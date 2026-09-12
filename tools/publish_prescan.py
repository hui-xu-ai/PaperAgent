# -*- coding: utf-8 -*-
"""发布前扫描（公开仓库专用闸门）：确认**没有真实密钥/本机私有路径/用户数据**入库。

用法：
  python tools\\publish_prescan.py            # 只扫当前工作树（快）
  python tools\\publish_prescan.py --history   # 连 git 历史一起扫（慢，发布前必跑一次）

退出码：0 = 通过；1 = 有需要人工确认的命中（按输出逐条处理，别直接发布）。

来源（2026-09-12 实测）：本仓库曾把 ① 真实 DeepSeek/SiliconFlow/智谱 Key 拷进
`tests/fixtures/data-v2/data/system/app.db`；② 整个 `.edge-debug/` 浏览器 profile
（含 Cookies/History/Cache，其中一份缓存里还有真 Key）提交进历史。
两者都只能靠**扫描 + 历史重写**解决，所以固化成闸门脚本。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parents[1]

# 真密钥形态（假值/占位不匹配）
RX_SECRET = re.compile(
    rb"(sk-[A-Za-z0-9]{20,64}"                       # OpenAI/DeepSeek/SiliconFlow 形态
    rb"|[0-9a-f]{32}\.[A-Za-z0-9]{12,})")            # 智谱形态 id.secret
RX_LOCALPATH = re.compile(rb"[A-Za-z]:\\\\(?:Python|Users)\\\\|/Users/[a-z0-9_.\-]+/|/home/[a-z0-9_.\-]+/")
# 允许（测试用假值 / 文档占位）
ALLOWLIST = (b"sk-REDACTED-EXAMPLE", b"sk-test-", b"sk-from-frontend", b"sk-top-level",
             b"sk-snapshot", b"sk-real-1234567890abcdef", b"sk-brand-new-key", b"sk-mineru-new",
             b"sk-silicon-new", b"sk-real-key-1234567890", b"sk-managementMinerU")
# 这些路径**永远不该进版本库**（`.env.example` 是模板，例外放行）
FORBIDDEN_PATH_RX = re.compile(
    r"(^|/)(\.edge-debug|\.edge-test|\.edge-verify|\.env$|\.env\.(?!example$)[^/]+$|data/|"
    r"library/|knowledge_base/|work/|logs/|release/|dist/|\.pytest_cache|__pycache__|\.idea|"
    r"\.gh-pat[^/]*|\.gh-token|gh-token\.txt|\.github-token|\.pat)/?")


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True).stdout


def scan_text(name: str, data: bytes, hits: list) -> None:
    for m in RX_SECRET.finditer(data):
        val = m.group(0)
        if any(a in val for a in ALLOWLIST):
            continue
        hits.append({"where": name, "kind": "secret", "value": val[:40].decode("ascii", "replace")})
    for m in RX_LOCALPATH.finditer(data):
        hits.append({"where": name, "kind": "local_path",
                     "value": m.group(0)[:60].decode("utf-8", "replace")})


def scan_worktree() -> list:
    hits: list = []
    for rel in git("ls-files").decode("utf-8", "replace").splitlines():
        rel = rel.strip()
        if not rel:
            continue
        if FORBIDDEN_PATH_RX.search(rel) and not rel.startswith("tests/fixtures/"):
            hits.append({"where": rel, "kind": "tracked_forbidden_path", "value": rel})
        p = ROOT / rel
        if p.is_file() and p.stat().st_size < 8_000_000:
            scan_text(rel, p.read_bytes(), hits)
    return hits


def scan_history() -> list:
    hits: list = []
    objs = git("rev-list", "--objects", "--all").decode("utf-8", "replace").splitlines()
    shas = []
    for line in objs:
        parts = line.split(" ", 1)
        if parts and re.fullmatch(r"[0-9a-f]{40}", parts[0]):
            shas.append((parts[0], parts[1] if len(parts) > 1 else ""))
    for i in range(0, len(shas), 400):
        chunk = shas[i:i + 400]
        meta = {}
        pr = subprocess.run(["git", "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
                            cwd=ROOT, input="\n".join(s for s, _ in chunk).encode(),
                            capture_output=True)
        for line in pr.stdout.decode("utf-8", "replace").splitlines():
            f = line.split()
            if len(f) == 3 and f[1] == "blob" and int(f[2]) < 2_000_000:
                meta[f[0]] = int(f[2])
        if not meta:
            continue
        pr2 = subprocess.run(["git", "cat-file", "--batch"], cwd=ROOT,
                             input="\n".join(meta).encode(), capture_output=True)
        raw, pos = pr2.stdout, 0
        while pos < len(raw):
            nl = raw.find(b"\n", pos)
            if nl < 0:
                break
            head = raw[pos:nl].decode("utf-8", "replace").split()
            if len(head) < 3:
                break
            size = int(head[2])
            body = raw[nl + 1:nl + 1 + size]
            pos = nl + 1 + size + 1
            scan_text("blob " + head[0][:8], body, hits)
            path = dict(chunk).get(head[0], "")
            if path and FORBIDDEN_PATH_RX.search(path):
                hits.append({"where": f"history:{head[0][:8]}", "kind": "history_forbidden_path",
                             "value": path})
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", action="store_true", help="连 git 历史一起扫（发布前必跑）")
    ap.add_argument("--json", default="", help="命中落盘路径")
    args = ap.parse_args()

    hits = scan_worktree()
    if args.history:
        hits += scan_history()
    secrets = [h for h in hits if h["kind"] == "secret"]
    paths = [h for h in hits if "path" in h["kind"]]
    print(f"工作树跟踪文件：{len(git('ls-files').decode().splitlines())} 个"
          f"{'，含历史 blob 扫描' if args.history else ''}")
    print(f"命中：密钥 {len(secrets)} · 违禁路径 {len(paths)} · 本机路径 "
          f"{len([h for h in hits if h['kind'] == 'local_path'])}")
    for h in (secrets + paths)[:40]:
        print(f"  [{h['kind']}] {h['where']}: {h['value']}")
    if args.json:
        Path(args.json).write_text(json.dumps(hits, ensure_ascii=False, indent=1), encoding="utf-8")
    if secrets or paths:
        print("\n[FAIL] 有真实密钥或违禁路径 ⇒ **不要发布**；先脱敏（tools\\sanitize_fixtures.py）"
              "并按需重写历史（见 HANDOFF）。")
        return 1
    print("\n[PASS] 未发现真实密钥 / 违禁路径入库。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
