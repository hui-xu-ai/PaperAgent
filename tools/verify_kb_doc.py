# -*- coding: utf-8 -*-
"""一致性校验工具（KB-DESIGN D17）：en.md ↔ document.json 对齐检查。

用法：
    python tools/verify_kb_doc.py --doi 10.1016/j.cej.2025.167798 [--base kb|library] [--root D:/Python/DeepSeek/PaperAgent]

退出码：0=一致；1=不一致/缺文件。供导入/重新同步原文后人工/脚本校验。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "paperkb"))

from paperkb.config import Roots  # noqa: E402
from paperkb.imports import verify_kb_doc  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="en.md ↔ document.json 一致性校验")
    ap.add_argument("--doi", required=True, help="DOI（或 kb/library 目录名）")
    ap.add_argument("--base", default="kb", choices=("kb", "library"),
                    help="校验哪一侧（默认 kb）")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1]),
                    help="项目根（默认自动推断）")
    args = ap.parse_args()

    root = Path(args.root)
    roots = Roots(data_dir=root / "data", library_dir=root / "library",
                  kb_dir=root / "knowledge_base")
    v = verify_kb_doc(args.doi, roots, base=args.base)
    print(f"base={args.base} doi={v.get('doi', args.doi)}")
    if "error" in v:
        print(f"ERROR: {v['error']}")
        return 1
    print(f"document.json 段落={v['expected_count']}  en.md 段落={v['found_count']}  "
          f"一致={v['ok']}")
    for m in v["mismatches"][:10]:
        print(f"  [{m['index']}] {m['para_id']}: 期望 {m['expected']!r} 实际 {m['found']!r}")
    if not v["ok"]:
        print(f"不一致项 {len(v['mismatches'])} 处（显示前 10）")
    return 0 if v["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
