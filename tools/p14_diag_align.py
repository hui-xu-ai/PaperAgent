#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: tools/p14_diag_align.py
功能: P14 诊断——查清 p14_diff_report 中异常项的根因：
      1) md 段"未对齐"但文本与本地段几乎相同（对齐算法失败？）
      2) "mineru拆/并" 是真实断段还是行区间重叠误判（链式错位）
      3) 本地段"未覆盖"的分类（图注续行/meta 等）
用法: python tools/p14_diag_align.py <pdf> <md>
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from paperparse.core.skeleton_local import build_local_skeleton
from paperparse.core.md_align import (align_md_to_skeleton, norm_text, _dice,
                                      _tokens)


def _snip(t, n=90):
    t = re.sub(r"\s+", " ", t or "").strip()
    return t[:n] + ("…" if len(t) > n else "")


def main() -> None:
    pdf, md = sys.argv[1], sys.argv[2]
    skeleton = build_local_skeleton(pdf)
    md_text = Path(md).read_text(encoding="utf-8")
    res = align_md_to_skeleton(md_text, skeleton)

    md_body = [p for p in res.md_paras if p.kind == "body"]
    local_bodies = [p for p in skeleton.paragraphs
                    if getattr(p, "kind", "") == "body"]
    local_norm = {p.para_id: norm_text(p.text) for p in local_bodies}

    print("=" * 72)
    print("【1】md 段未对齐 → 全量扫描最近本地段（判断:文本存在但锚失败?）")
    print("=" * 72)
    for p in md_body:
        if p.idx in res.line_map:
            continue
        # 全量扫描本地段
        best = max(local_bodies, key=lambda lp: _dice(p.norm, local_norm[lp.para_id]))
        d = _dice(p.norm, local_norm[best.para_id])
        # 单词交集数
        inter = len(_tokens(p.norm) & _tokens(local_norm[best.para_id]))
        print(f"md#{p.idx:>3} [{p.section[:24]:<24}] dice={d:.2f} "
              f"交集词={inter:>3} → 最近本地 {best.para_id} "
              f"(words={best.words}, closed={best.closed})")
        print(f"      md  : {_snip(p.text)}")
        print(f"      local: {_snip(best.text)}")

    print()
    print("=" * 72)
    print("【2】本地段未被 md 覆盖 → 分类检查")
    print("=" * 72)
    covered_local = set()
    for p in md_body:
        for pid in _md_local_map(res, md_body, local_bodies).get(p.idx, []):
            covered_local.add(pid)
    for lp in local_bodies:
        if lp.para_id in covered_local:
            continue
        print(f"{lp.para_id:>6} pages={lp.pages} words={lp.words:>3} "
              f"closed={lp.closed} kind={lp.kind}")
        print(f"      首行: {_snip(lp.start_line.text)}  (kind={lp.start_line.kind}, "
              f"region={lp.start_line.region}, font={lp.start_line.font_size})")
        print(f"      文本: {_snip(lp.text, 110)}")

    print()
    print("=" * 72)
    print("【3】mineru拆 的真实性核验（本地1段 = md多段）")
    print("=" * 72)
    local_of_md = _md_local_map(res, md_body, local_bodies)
    md_of_local: dict[str, list] = {}
    for p in md_body:
        for pid in local_of_md.get(p.idx, []):
            md_of_local.setdefault(pid, []).append(p.idx)
    for pid, mids in md_of_local.items():
        if len(mids) < 2:
            continue
        lp = next(x for x in local_bodies if x.para_id == pid)
        print(f"{pid} pages={lp.pages} words={lp.words} ← md 段 {mids}")
        # 检查 md 各段首/尾词与本地段对应位置
        for i in mids:
            q = next(x for x in md_body if x.idx == i)
            toks = q.norm.split()
            print(f"   md#{i:>3} 首={_snip(q.text, 60)!r:>70}")
        print(f"   本地首={_snip(lp.start_line.text, 60)!r}")
        print(f"   本地尾={_snip(lp.end_line.text, 60)!r}")
        print()


def _md_local_map(res, md_body, local_bodies) -> dict:
    """md idx → [local para_id]（行区间重叠）"""
    out: dict[int, list] = {}

    def _num(lid: str) -> int:
        try:
            return int(re.sub(r"\D", "", lid))
        except ValueError:
            return 0

    for p in md_body:
        rng = res.line_map.get(p.idx)
        if not rng:
            continue
        s, e = _num(rng[0]), _num(rng[1])
        for lp in local_bodies:
            ls = _num(lp.start_line.line_id)
            le = _num(lp.end_line.line_id)
            if s <= le and ls <= e:
                out.setdefault(p.idx, []).append(lp.para_id)
    return out


if __name__ == "__main__":
    main()
