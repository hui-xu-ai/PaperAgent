# -*- coding: utf-8 -*-
r"""反馈草稿 → 主文件（`docs/AGENT_FEEDBACK.md`）**原子追加**。

为什么需要（pydev-protocol §7）：多会话可能同时写反馈，`read→edit` 主文件会互相覆盖。
本脚本只做 append（带文件锁 + 重试 + 幂等判重），**从不读回主文件正文进上下文**。

用法：
    python tools\feedback_append.py feedback\2026-09-12-V18-xxx.md
    python tools\feedback_append.py <draft> --target docs\AGENT_FEEDBACK.md --dry-run
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

DEFAULT_TARGET = Path("docs/AGENT_FEEDBACK.md")
LOCK = Path("docs/.AGENT_FEEDBACK.lock")
STALE_SEC = 60.0


def _marker(text: str) -> str:
    """判重标记：草稿的首个标题行（如 `# AGENT_FEEDBACK V18（…）`）。"""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            return s
    return text.strip().splitlines()[0][:80] if text.strip() else ""


def _acquire(timeout: float = 20.0) -> None:
    t0 = time.time()
    while True:
        try:
            fd = LOCK.open("x", encoding="utf-8")
            fd.write(f"{Path.cwd()} pid={__import__('os').getpid()}\n")
            fd.close()
            return
        except FileExistsError:
            try:
                if time.time() - LOCK.stat().st_mtime > STALE_SEC:
                    LOCK.unlink(missing_ok=True)   # 陈旧锁（进程被杀）自动回收
                    continue
            except OSError:
                pass
            if time.time() - t0 > timeout:
                raise SystemExit(f"获取反馈文件锁超时（{LOCK}）；如确认无其它会话在写，可手动删除该锁文件")
            time.sleep(0.5)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("draft", help="草稿 md 路径")
    ap.add_argument("--target", default=str(DEFAULT_TARGET))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    draft = Path(args.draft)
    target = Path(args.target)
    if not draft.is_file():
        raise SystemExit(f"草稿不存在: {draft}")
    text = draft.read_text(encoding="utf-8").rstrip() + "\n"
    marker = _marker(text)
    if not marker:
        raise SystemExit("草稿为空")

    if target.is_file() and marker in target.read_text(encoding="utf-8", errors="replace"):
        print(f"已存在同标题段落，跳过追加（幂等）：{marker[:60]}")
        return 0
    if args.dry_run:
        print(f"[dry-run] 将把 {draft}（{len(text)} 字符）追加到 {target}")
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    _acquire()
    try:
        with target.open("a", encoding="utf-8") as f:
            f.write("\n---\n\n")
            f.write(text)
    finally:
        LOCK.unlink(missing_ok=True)
    print(f"已追加：{draft} → {target}（+{len(text)} 字符；标记「{marker[:50]}」）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
