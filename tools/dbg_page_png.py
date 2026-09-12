#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""渲染指定 PDF 页为 PNG（只读取证用）。

用法: python tools/dbg_page_png.py <pdf> <pages e.g. 13,15> [--dpi 90]
输出: work/scratch/pagepng/<stem>-p<N>.png
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages" / "paperparse"))

import pymupdf  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("pages")
    ap.add_argument("--dpi", type=int, default=90)
    args = ap.parse_args()
    pdf = Path(args.pdf)
    if not pdf.is_absolute():
        pdf = ROOT / pdf
    out = ROOT / "work" / "scratch" / "pagepng"
    out.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(str(pdf))
    for spec in args.pages.split(","):
        pno = int(spec) - 1
        pix = doc[pno].get_pixmap(dpi=args.dpi)
        p = out / f"{pdf.stem}-p{pno + 1}.png"
        pix.save(str(p))
        print(p)
    doc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
