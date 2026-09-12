#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""共享全文前缀的**噪声取证**：References / ACKNOWLEDGMENTS / Conflict of Interest 等
是否漏进了 `shared_ctx`（送 AI 的原文），以及浪费了多少字符/token。

用法: python tools/dbg_ctx_noise.py [--rid <资源目录名> ...]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages" / "paperkb"))

from paperkb.context import (context_paragraphs, shared_ctx, tail_cut_index,  # noqa: E402
                             _is_ref_section)
from paperkb.doc import read_document  # noqa: E402

# 不该进上下文的"非正文"段（用户 2026-09-12 指出）
NOISE_PAT = re.compile(
    r"^\s*(references|bibliography|acknowledg(e)?ments?|conflict of interest|"
    r"declaration of competing interest|competing interests?|author contributions?|"
    r"data availability|supporting information|supplementary (material|information)|"
    r"associated content|credit authorship|funding|notes\b|"
    r"this article references|additional information)", re.IGNORECASE)
# 参考文献条目形态：编号/年份/期刊缩写开头
REF_ENTRY_PAT = re.compile(r"^\s*(\[\d+\]|\(\d+\)|\d+\.\s+[A-Z])")
YEAR_PAT = re.compile(r"\b(19|20)\d{2}\b")


def suspect(para) -> str:
    t = (para.text_en or "").strip()
    if not t:
        return ""
    if NOISE_PAT.match(t):
        return "标题式噪声"
    if REF_ENTRY_PAT.match(t) and YEAR_PAT.search(t) and len(t) > 60:
        return "疑似参考文献条目"
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rid", nargs="*", default=None)
    ap.add_argument("--out", default="work/scratch/ctx-noise.json")
    args = ap.parse_args()

    rids = args.rid or [d.name for d in sorted((ROOT / "library").iterdir()) if d.is_dir()]
    report = []
    for rid in rids:
        doc_json = None
        for base in ("knowledge_base", "library"):
            cand = ROOT / base / rid / "document.json"
            if cand.exists():
                doc_json = cand
                break
        if doc_json is None:
            continue
        doc = read_document(doc_json)
        ctx = shared_ctx(doc)
        cut = tail_cut_index(doc)
        kept = context_paragraphs(doc)
        dropped = len(doc.paragraphs) - len(kept)
        hits = []
        for p in doc.paragraphs:
            why = suspect(p)
            if not why:
                continue
            in_ctx = ("[%s]" % p.para_id) in ctx
            hits.append({"para_id": p.para_id, "section": p.section,
                         "is_heading": bool(p.is_heading), "why": why,
                         "chars": len(p.text_en or ""),
                         "in_ctx": in_ctx,
                         "ref_section": _is_ref_section(p.section),
                         "head": (p.text_en or "").strip()[:70]})
        bad = [h for h in hits if h["in_ctx"]]
        wasted = sum(h["chars"] for h in bad)
        report.append({"rid": rid, "doc": str(doc_json.relative_to(ROOT)),
                       "ctx_chars": len(ctx), "paras": len(doc.paragraphs),
                       "tail_cut_idx": cut, "dropped_paras": dropped,
                       "suspect_total": len(hits), "leaked": len(bad),
                       "leaked_chars": wasted,
                       "leaked_tokens_est": int(wasted / 3.85),
                       "leaked_pct": round(100.0 * wasted / max(1, len(ctx)), 1),
                       "items": bad})
        edge = ""
        if cut < len(doc.paragraphs):
            prev = doc.paragraphs[cut - 1].text_en if cut else ""
            edge = ("\n      截断点: idx=%d/%d 触发段=%r\n      上一段(保留)=%r"
                    % (cut, len(doc.paragraphs),
                       (doc.paragraphs[cut].text_en or "")[:60], (prev or "")[:60]))
        print(f"[{rid[:34]:34s}] 段={len(doc.paragraphs):4d} ctx={len(ctx):6d}字符 "
              f"嫌疑={len(hits):3d} **漏进前缀={len(bad):3d}（{wasted} 字符 ≈{int(wasted / 3.85)} token，"
              f"占 {round(100.0 * wasted / max(1, len(ctx)), 1)}%）{edge}")
        for h in bad[:8]:
            print(f"    {h['para_id']} sec={h['section'][:18]!r:20s} {h['why']} "
                  f"{h['chars']:5d}字 | {h['head'][:56]}")
    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    tot = sum(r["leaked_chars"] for r in report)
    print(f"\n合计漏进前缀 {tot} 字符 ≈{int(tot / 3.85)} token；报告 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
