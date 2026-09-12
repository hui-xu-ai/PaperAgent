#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定点诊断：某页图注锚点的 cap_box / 图元 / 并排种子。

用法: python tools/dbg_fig_anchor.py <pdf> <page>
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages" / "paperparse"))

import pymupdf  # noqa: E402

from paperparse.core import image_extract as ie  # noqa: E402
from paperparse.core.skeleton_local import build_local_skeleton  # noqa: E402


def main() -> int:
    pdf, page = sys.argv[1], int(sys.argv[2])
    p = Path(pdf)
    if not p.is_absolute():
        p = ROOT / pdf
    sk = build_local_skeleton(str(p))
    doc = pymupdf.open(str(p))
    pw, ph = doc[page - 1].rect.width, doc[page - 1].rect.height
    print(f"page {page} pw={pw:.1f} ph={ph:.1f} seed_area>={ie._SEED_MIN_AREA_FRAC * pw * ph:.0f}")
    for para in sk.paragraphs:
        if para.kind != "caption" or not para.lines:
            continue
        if para.lines[0].page != page:
            continue
        box = (min(l.bbox[0] for l in para.lines), min(l.bbox[1] for l in para.lines),
               max(l.bbox[2] for l in para.lines), max(l.bbox[3] for l in para.lines))
        print(f"CAP num-lines={len(para.lines)} box={tuple(round(v,1) for v in box)} "
              f"text={para.text[:50]!r}")
        prims = ie._collect_graphics(doc[page - 1])
        print(f"  prims={len(prims)}")
        seed, idx = ie._beside_seeds(prims, box, pw, ph, set())
        print(f"  beside_seed={seed and tuple(round(v,1) for v in seed)} idx={len(idx)}")
        for i, pr in enumerate(prims):
            ov = min(box[3], pr[3]) - max(box[1], pr[1])
            hmin = min(box[3] - box[1], pr[3] - pr[1])
            gap = box[0] - pr[2] if pr[2] <= box[0] else pr[0] - box[2]
            if pr[3] > box[1] and pr[1] < box[3]:
                print(f"    p{i} {tuple(round(v,1) for v in pr)} "
                      f"yov={ov / hmin if hmin > 0 else 0:.2f} xgap={gap:.1f} "
                      f"area={(pr[2]-pr[0])*(pr[3]-pr[1]):.0f}")
    doc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
