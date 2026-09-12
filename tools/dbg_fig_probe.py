#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图片提取 A/B 取证探针（只读 PDF，不调任何 API）。

对 用户提供的文献/PDF文献/*.pdf 逐篇跑两套算法并对比：
  v1 = git HEAD 的 image_extract（旧：位图门槛 25% 页宽 + 整栏渲染兜底）
  v2 = 当前工作区的 image_extract（内容聚类 + 文本避让）
指标（对每个产出图）：
  wide  = 面积 ≥50% 落入裁剪框的**宽文本行**（宽度 ≥ 该页栏宽 45% 的正文/标题行）
          → "混入正文"的判据
  narrow= 同口径的窄文本行（图内坐标轴/分图号等图内文字，信息性计数）

用法: python tools/dbg_fig_probe.py [--out work/scratch/fig-probe-v2]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages" / "paperparse"))

import pymupdf  # noqa: E402

from paperparse.core import image_extract as v2mod  # noqa: E402
from paperparse.core.skeleton_local import build_local_skeleton  # noqa: E402

TEXT_KINDS = {"body", "heading", "meta", "other"}
_W_FRAC = 0.45


def load_v1():
    """从 work/scratch/image_extract_v1.py 载入旧版模块（git HEAD 快照）"""
    path = ROOT / "work" / "scratch" / "image_extract_v1.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location("image_extract_v1", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _inter(a, b) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def _obstacles(sk, doc) -> dict:
    """每页宽文本行 bbox（与 image_extract v2 同口径）"""
    by_page: dict[int, list] = {}
    for ln in sk.lines:
        if getattr(ln, "bbox", None):
            by_page.setdefault(ln.page, []).append(ln)
    out = {}
    for pno, lns in by_page.items():
        widths = [ln.bbox[2] - ln.bbox[0] for ln in lns
                  if ln.kind in ("body", "heading") and (ln.bbox[2] - ln.bbox[0]) > 10]
        col_w = max(widths) if widths else doc[pno - 1].rect.width * 0.8
        out[pno] = [tuple(float(v) for v in ln.bbox) for ln in lns
                    if (ln.bbox[2] - ln.bbox[0]) >= col_w * _W_FRAC
                    and (ln.bbox[3] - ln.bbox[1]) > 2]
    return out


def _cover(box, prims, step: float = 4.0) -> float:
    """裁剪框内"图形图元覆盖比例"（紧致度）：1.0=完全贴图，低=含大量空白/文字"""
    import numpy as np
    w = max(1, int((box[2] - box[0]) / step))
    h = max(1, int((box[3] - box[1]) / step))
    mask = np.zeros((h, w), dtype=bool)
    for p in prims:
        x0 = int((p[0] - box[0]) / step)
        x1 = int((p[2] - box[0]) / step)
        y0 = int((p[1] - box[1]) / step)
        y1 = int((p[3] - box[1]) / step)
        x0, x1 = max(0, x0), min(w, x1)
        y0, y1 = max(0, y0), min(h, y1)
        if x1 > x0 and y1 > y0:
            mask[y0:y1, x0:x1] = True
    return float(mask.mean())


def score(figs, obstacles_by_page, prims_by_page):
    """统计每个图的 wide/narrow 文本行命中"""
    recs = []
    for f in figs:
        box = tuple(f.bbox)
        area = max(1e-6, (box[2] - box[0]) * (box[3] - box[1]))
        wide = narrow = 0
        wide_area = 0.0
        for o in obstacles_by_page.get(f.page, []):
            ov = _inter(o, box)
            if ov <= 0:
                continue
            a = max(1e-6, (o[2] - o[0]) * (o[3] - o[1]))
            if ov / a >= 0.5:
                wide += 1
                wide_area += a
            elif ov / a >= 0.3:
                narrow += 1
        recs.append({"fig": f.fig_id, "page": f.page,
                     "bbox": [round(v, 1) for v in box],
                     "w": round(box[2] - box[0], 1), "h": round(box[3] - box[1], 1),
                     "caption": (f.caption or "")[:60],
                     "wide": wide, "narrow": narrow,
                     "cover": round(_cover(box, prims_by_page.get(f.page, [])), 3),
                     "wide_area_frac": round(wide_area / area, 3)})
    return recs


def analyze(pdf: Path, out_root: Path, v1) -> dict:
    sk = build_local_skeleton(str(pdf))
    doc = pymupdf.open(str(pdf))
    try:
        obs = _obstacles(sk, doc)
        prims_by_page = {i + 1: v2mod._collect_graphics(doc[i])
                         for i in range(doc.page_count)}
    finally:
        doc.close()
    diag: dict = {}
    figs2 = v2mod.extract_figures_caption_driven(
        str(pdf), sk, out_root / pdf.stem / "images", diag=diag)
    res = {"pdf": pdf.name, "figures_v2": len(figs2),
           "recs_v2": score(figs2, obs, prims_by_page),
           "anchor_counts": diag.get("counts", {}),
           "anchors": diag.get("anchors", [])}
    if v1 is not None:
        figs1 = v1.extract_figures_caption_driven(
            str(pdf), sk, out_root / (pdf.stem + "_v1") / "images")
        res["figures_v1"] = len(figs1)
        res["recs_v1"] = score(figs1, obs, prims_by_page)
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="work/scratch/fig-probe-v2")
    ap.add_argument("--pdf-dir", default="用户提供的文献/PDF文献")
    args = ap.parse_args()

    out_root = ROOT / args.out
    out_root.mkdir(parents=True, exist_ok=True)
    v1 = load_v1()
    pdfs = sorted((ROOT / args.pdf_dir).glob("*.pdf"))
    report = []
    tot = {"f1": 0, "f2": 0, "bad1": 0, "bad2": 0, "cov1": 0.0, "cov2": 0.0}
    print(f"{'论文':38s} {'v1图':>4s} {'v1污染':>6s} {'v2图':>4s} {'v2污染':>6s} "
          f"{'v1覆盖':>7s} {'v2覆盖':>7s}")
    for pdf in pdfs:
        try:
            r = analyze(pdf, out_root, v1)
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {pdf.name}: {type(exc).__name__}: {exc}")
            continue
        report.append(r)
        b1 = [x for x in r.get("recs_v1", []) if x["wide"] > 0]
        b2 = [x for x in r["recs_v2"] if x["wide"] > 0]
        c1 = [x["cover"] for x in r.get("recs_v1", [])]
        c2 = [x["cover"] for x in r["recs_v2"]]
        m1 = sum(c1) / len(c1) if c1 else 0.0
        m2 = sum(c2) / len(c2) if c2 else 0.0
        tot["f1"] += r.get("figures_v1", 0)
        tot["f2"] += r["figures_v2"]
        tot["bad1"] += len(b1)
        tot["bad2"] += len(b2)
        tot["cov1"] += sum(c1)
        tot["cov2"] += sum(c2)
        print(f"{pdf.stem[:38]:38s} {r.get('figures_v1', 0):4d} {len(b1):6d} "
              f"{r['figures_v2']:4d} {len(b2):6d} {m1:7.3f} {m2:7.3f}  {r['anchor_counts']}")
        for x in b2:
            print(f"    !{x['fig']} p{x['page']} {x['bbox']} wide={x['wide']} "
                  f"frac={x['wide_area_frac']} | {x['caption'][:40]}")
    (out_root / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n合计 {len(report)} 篇：v1 图 {tot['f1']}（混正文 {tot['bad1']}，覆盖 "
          f"{tot['cov1'] / max(1, tot['f1']):.3f}） → v2 图 {tot['f2']}（混正文 "
          f"{tot['bad2']}，覆盖 {tot['cov2'] / max(1, tot['f2']):.3f}）")
    print("覆盖 = 裁剪框内图形图元占比（1.0 最紧致；含正文/大片空白的框会偏低）")
    print(f"报告: {out_root / 'report.json'}")
    for r in report:
        print(f"  {r['pdf'][:40]:40s} anchors={r['anchor_counts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
