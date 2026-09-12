# -*- coding: utf-8 -*-
"""把 app.js 里的 confirm(...) 同步弹窗改成页内确认 askConfirm(...)。

用法（项目根）：python tools/migrate_confirm.py [--apply]
默认 dry-run 只打印将要修改的行；--apply 才写盘。
规则：
  - `X !confirm(ARGS) Y`   → `X !(await askConfirm(ARGS)) Y`
  - `X confirm(ARGS) Y`    → `X (await askConfirm(ARGS)) Y`
  - 不动 `window.confirm`（app.js 里的兜底）与本脚本改后的 `await askConfirm(`。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

APP = Path("frontend/app.js")
CALL = re.compile(r"(?<![.\w])(!?)confirm\(")


def _match_paren(text: str, open_idx: int) -> int:
    """返回与 text[open_idx]=='(' 配对的 ')' 下标；不处理字符串里的括号（本文件够用）。"""
    depth = 0
    i = open_idx
    in_s = None
    while i < len(text):
        ch = text[i]
        if in_s:
            if ch == "\\":
                i += 2
                continue
            if ch == in_s:
                in_s = None
        elif ch in "\"'`":
            in_s = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError("括号不配对")


def transform(src: str) -> tuple[str, list[tuple[int, str]]]:
    out = []
    pos = 0
    changes: list[tuple[int, str]] = []
    for m in CALL.finditer(src):
        start = m.start()
        open_idx = m.end() - 1                      # '(' 位置
        line_no = src.count("\n", 0, start) + 1
        # 跳过已经是 await askConfirm( 的（防御性；正则本就不匹配 askConfirm）
        close_idx = _match_paren(src, open_idx)
        neg = m.group(1)
        inner = src[open_idx + 1:close_idx]
        new = f"{'!(' if neg else '('}await askConfirm({inner}){')' if neg else ')'}"
        out.append(src[pos:start])
        out.append(new)
        changes.append((line_no, f"confirm({inner[:48]}…)  →  {'!(' if neg else '('}await askConfirm(…)…"))
        pos = close_idx + 1
    out.append(src[pos:])
    return "".join(out), changes


def main() -> None:
    apply = "--apply" in sys.argv
    src = APP.read_text(encoding="utf-8")
    new, changes = transform(src)
    print(f"将替换 {len(changes)} 处 confirm：")
    for ln, desc in changes:
        print(f"  L{ln}: {desc}")
    if not apply:
        print("\n[dry-run] 加 --apply 写盘")
        return
    APP.write_text(new, encoding="utf-8")
    print(f"\n已写盘：{APP}")


if __name__ == "__main__":
    main()
