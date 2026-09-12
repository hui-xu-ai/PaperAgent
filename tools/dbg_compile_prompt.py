#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""编译提示词取证：L1/L2/L3 各送了多少正文、kb 副本与 library 的前缀分叉点。

用法: python tools/dbg_compile_prompt.py <资源键或目录名> [--kb path] [--lib path]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages" / "paperkb"))
sys.path.insert(0, str(ROOT / "packages" / "paperparse"))

from paperkb.compile import _l2_sections, _prompt_l1, _prompt_l2, _prompt_l3  # noqa: E402
from paperkb.context import shared_ctx  # noqa: E402
from paperkb.doc import read_document  # noqa: E402


def tok(n_chars: int) -> int:
    """英文/中文混合的粗略 token 估算（与实测 deepseek 用量同量级）"""
    return int(n_chars / 3.85)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("rid")
    ap.add_argument("--kb", default=None)
    ap.add_argument("--lib", default=None)
    args = ap.parse_args()

    kb = Path(args.kb) if args.kb else ROOT / "knowledge_base" / args.rid / "document.json"
    lib = Path(args.lib) if args.lib else ROOT / "library" / args.rid / "document.json"

    docs = {}
    for tag, p in (("kb", kb), ("library", lib)):
        if p.exists():
            docs[tag] = read_document(str(p))
    if not docs:
        print("找不到 document.json")
        return 1

    meta = {}
    ref = docs.get("kb") or docs.get("library")
    md = ref.metadata if hasattr(ref, "metadata") else {}
    meta = {"title": getattr(md, "title", ""), "journal": getattr(md, "journal", ""),
            "year": getattr(md, "year", ""), "abstract": getattr(md, "abstract", "") or "",
            "keywords": getattr(md, "keywords", None) or []}

    for tag, doc in docs.items():
        sc = shared_ctx(doc)
        print(f"[{tag:7s}] {len(doc.paragraphs)} 段  shared_ctx={len(sc)} 字符 "
              f"≈{tok(len(sc))} token  path={doc.__class__.__name__}")

    if len(docs) == 2:
        a, b = shared_ctx(docs["kb"]), shared_ctx(docs["library"])
        n = min(len(a), len(b))
        i = 0
        while i < n and a[i] == b[i]:
            i += 1
        print(f"→ kb 与 library 的 shared_ctx 在**第 {i} 个字符**分叉"
              f"（公共前缀 ≈{tok(i)} token；长度 {len(a)} vs {len(b)}）")
        print(f"   kb  : ...{a[max(0, i - 40):i + 60]!r}")
        print(f"   lib : ...{b[max(0, i - 40):i + 60]!r}")

    doc = ref
    l1 = _prompt_l1(meta, doc, meta.get("journal", ""))
    l2 = _prompt_l2(meta, doc, "(L1 摘要占位)")
    l3 = _prompt_l3(meta, doc, "(L1 摘要占位)", "(L2 摘要占位)")
    print(f"\nL1 prompt: {len(l1)} 字符 ≈{tok(len(l1))} token（= shared_ctx + 指令，全文一次送入）")
    print(f"L2 prompt: {len(l2)} 字符 ≈{tok(len(l2))} token（**不含 shared_ctx**）")
    print(f"L3 prompt: {len(l3)} 字符 ≈{tok(len(l3))} token（= shared_ctx + 指令）")

    secs = _l2_sections(doc)
    kept, skipped = [], []
    for s in secs:
        (skipped if s.get("count", 0) > 60 else kept).append(s)
    print(f"\nL2 章节清单：共 {len(secs)} 个；>60 段被丢弃 {len(skipped)} 个"
          f"（丢弃段数 {sum(s['count'] for s in skipped)}）")
    for s in sorted(secs, key=lambda x: -x.get("count", 0))[:12]:
        mark = "丢弃" if s.get("count", 0) > 60 else "保留"
        print(f"   [{mark}] {s.get('count'):4d} 段  {str(s.get('section'))[:50] or '(未分节)'}")
    parts = l2.split("## 章节片段（原文 text_en）")
    print(f"   实际送入 L2 的章节片段 = {len(parts[1])} 字符" if len(parts) > 1 else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
