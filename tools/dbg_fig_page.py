#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单页图元取证：位图 bbox / 矢量图元聚类 / 文本行，用于图片裁剪算法设计。

用法: python tools/dbg_fig_page.py <pdf> <page> [--json work/scratch/page.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages" / "paperparse"))

import pymupdf  # noqa: E402

from paperparse.core.skeleton_local import build_local_skeleton  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("page", type=int)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    pdf = Path(args.pdf)
    if not pdf.is_absolute():
        pdf = ROOT / pdf
    doc = pymupdf.open(str(pdf))
    pno = args.page - 1
    page = doc[pno]
    pw, ph = page.rect.width, page.rect.height
    out = {"pdf": pdf.name, "page": args.page, "pw": pw, "ph": ph}

    imgs = []
    for info in page.get_image_info(xrefs=True):
        b = tuple(float(v) for v in info["bbox"])
        imgs.append({"bbox": [round(v, 1) for v in b],
                     "wh": [round(b[2] - b[0], 1), round(b[3] - b[1], 1)],
                     "xref": info.get("xref")})
    out["bitmaps"] = imgs

    draws = page.get_drawings()
    dr = []
    for d in draws:
        r = d["rect"]
        dr.append({"rect": [round(r.x0, 1), round(r.y0, 1), round(r.x1, 1), round(r.y1, 1)],
                   "w": round(r.width, 1), "h": round(r.height, 1),
                   "items": len(d["items"]), "type": d.get("type")})
    out["n_drawings"] = len(dr)
    out["drawings_big"] = [x for x in dr if x["w"] > 20 and x["h"] > 20][:60]

    sk = build_local_skeleton(str(pdf))
    lines = [{"y": [round(ln.bbox[1], 1), round(ln.bbox[3], 1)],
              "x": [round(ln.bbox[0], 1), round(ln.bbox[2], 1)],
              "kind": ln.kind, "col": ln.column, "text": ln.text[:60]}
             for ln in sk.lines if ln.page == args.page]
    out["lines"] = lines
    out["captions"] = [{"y0": round(p.start_line.bbox[1], 1), "kind": p.kind,
                        "text": p.text[:80]}
                       for p in sk.paragraphs
                       if p.start_line and p.start_line.page == args.page
                       and p.kind in ("caption", "body", "heading")]
    doc.close()
    text = json.dumps(out, ensure_ascii=False, indent=1)
    if args.json:
        Path(args.json).write_text(text, encoding="utf-8")
        print(f"written {args.json}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
