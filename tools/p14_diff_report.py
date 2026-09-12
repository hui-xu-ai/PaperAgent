#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: tools/p14_diff_report.py
功能: P14 检查工具——本地段落骨架（拼接标志） vs mineru md 导出段落的差异报告：
      - 本地骨架 = build_local_skeleton（首行缩进/封闭配对等拼接标志产出的段落边界）
      - mineru md = 官方 full.md 段落（文本基底）
      - 双向对齐统计差异方向：
        * 一致       : md 段 ↔ 本地段 1:1
        * mineru拆   : 1 本地段 ↔ N md 段（mineru 把一段拆开 = 拼接拆分问题）
        * mineru并   : 1 md 段 ↔ N 本地段（本地拆得更细 / mineru 漏拆）
        * md 未对齐  : md 段在本地骨架无对应
        * 本地未覆盖 : 本地段无任何 md 段对应
      输出: 摘要(控制台) + work/p14_diff_report/<md名>/report.{json,txt}
用法: python tools/p14_diff_report.py <pdf> <md> [--out DIR]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from paperparse.core.skeleton_local import build_local_skeleton
from paperparse.core.md_align import align_md_to_skeleton, norm_text, _dice


def _line_num(line_id: str) -> int:
    try:
        return int(re.sub(r"\D", "", line_id))
    except (ValueError, IndexError):
        return 0


def _snip(text: str, n: int = 80) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    return t[:n] + ("…" if len(t) > n else "")


def build_report(pdf: str, md_text: str) -> dict:
    skeleton = build_local_skeleton(pdf)
    res = align_md_to_skeleton(md_text, skeleton)

    md_body = [p for p in res.md_paras if p.kind == "body"]
    local_bodies = [p for p in getattr(skeleton, "paragraphs", []) or []
                    if getattr(p, "kind", "") == "body"]

    # md 段 idx → 行区间 → 覆盖的本地段（行区间重叠，同 repair_paragraphs 逻辑）
    local_of_md: dict[int, list] = {}
    for p in md_body:
        rng = res.line_map.get(p.idx)
        if not rng:
            continue
        s, e = _line_num(rng[0]), _line_num(rng[1])
        for lp in local_bodies:
            ls = _line_num(lp.start_line.line_id)
            le = _line_num(lp.end_line.line_id)
            if s <= le and ls <= e:
                local_of_md.setdefault(p.idx, []).append(lp.para_id)

    md_of_local: dict[str, list] = {}
    for p in md_body:
        for pid in local_of_md.get(p.idx, []):
            md_of_local.setdefault(pid, []).append(p.idx)

    local_norm = {p.para_id: norm_text(p.text) for p in local_bodies}

    items: list[dict] = []
    counts = {"one2one": 0, "mineru_split": 0, "mineru_merge": 0,
              "md_unaligned": 0, "local_uncovered": 0}

    # 逐 md 段分类
    for p in md_body:
        lids = local_of_md.get(p.idx, [])
        if not lids:
            counts["md_unaligned"] += 1
            items.append({"type": "md_unaligned", "md_idx": p.idx,
                          "md_text": _snip(p.text),
                          "section": p.section})
            continue
        if len(lids) >= 2:
            counts["mineru_merge"] += 1
            items.append({"type": "mineru_merge", "md_idx": p.idx,
                          "md_text": _snip(p.text), "section": p.section,
                          "local_paras": lids,
                          "dice": round(_dice(p.norm, " ".join(
                              local_norm.get(x, "") for x in lids)), 3)})
            continue
        # 1 个本地段：看反向是否多对一
        pid = lids[0]
        mids = md_of_local.get(pid, [])
        if len(mids) >= 2:
            counts["mineru_split"] += 1
            lp = next(x for x in local_bodies if x.para_id == pid)
            items.append({"type": "mineru_split", "md_idx": mids,
                          "local_para": pid,
                          "pages": lp.pages, "md_texts": [_snip(
                              next(q.text for q in md_body if q.idx == i))
                              for i in mids],
                          "dice": round(_dice(p.norm, local_norm.get(pid, "")), 3)})
            continue
        counts["one2one"] += 1
        lp = next(x for x in local_bodies if x.para_id == pid)
        items.append({"type": "one2one", "md_idx": p.idx, "local_para": pid,
                      "pages": lp.pages, "words": lp.words,
                      "dice": round(_dice(p.norm, local_norm.get(pid, "")), 3),
                      "md_text": _snip(p.text)})

    # 本地段未被任何 md 段覆盖
    covered_local = set(md_of_local)
    for lp in local_bodies:
        if lp.para_id not in covered_local:
            counts["local_uncovered"] += 1
            items.append({"type": "local_uncovered", "local_para": lp.para_id,
                          "pages": lp.pages, "words": lp.words,
                          "text": _snip(lp.text)})

    return {
        "pdf": str(pdf),
        "stats": {
            "md_paras_total": len(res.md_paras),
            "md_body": len(md_body),
            "local_paras_body": len(local_bodies),
            "md_aligned": len(res.line_map),
            **counts,
        },
        "local_stats": skeleton.stats,
        "items": items,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("md")
    ap.add_argument("--out", default="work/p14_diff_report")
    args = ap.parse_args()

    md_text = Path(args.md).read_text(encoding="utf-8")
    report = build_report(args.pdf, md_text)

    out_dir = Path(args.out) / Path(args.md).stem
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    s = report["stats"]
    print("=" * 70)
    print("本地段落骨架 vs mineru md 导出段落 —— 差异报告")
    print(f"PDF: {Path(args.pdf).name}")
    print(f"md : {Path(args.md).name}")
    print("=" * 70)
    print(f"md 总段数={s['md_paras_total']}  正文段={s['md_body']}  "
          f"本地正文段={s['local_paras_body']}  对齐md段={s['md_aligned']}")
    print("-" * 70)
    print(f"① 一致(1:1)        : {s['one2one']}")
    print(f"② mineru拆(本地1段=md多段): {s['mineru_split']}  <- 拼接拆分问题")
    print(f"③ mineru并(md1段=本地多段): {s['mineru_merge']}  <- 本地拆得更细")
    print(f"④ md未对齐          : {s['md_unaligned']}")
    print(f"⑤ 本地未覆盖        : {s['local_uncovered']}")
    print("-" * 70)
    for it in report["items"]:
        if it["type"] in ("one2one",):
            continue
        print(f"[{it['type']:>14}] {json.dumps(it, ensure_ascii=False)[:220]}")
    print(f"\n详细报告: {out_dir / 'report.json'}")


if __name__ == "__main__":
    main()
