#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: tools/p14_golden_check.py
功能: P14 验收——修复后段落流 vs 用户 golden（网页下载版 md）逐段对照：
      - 分别解析两 md 的正文段（parse_md_paragraphs）
      - 顺序敏感前缀锚对齐（单调不回退）+ 整段 Dice
      - 输出 一致(Dice≥0.6)/文本差异/未对齐 三类 + 数量
用法: python tools/p14_golden_check.py <repaired_md> <golden_md> [--out DIR]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from paperparse.core.md_align import parse_md_paragraphs, norm_text, _dice


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("repaired")
    ap.add_argument("golden")
    ap.add_argument("--out", default="work/p14_golden")
    args = ap.parse_args()

    rep_all = parse_md_paragraphs(Path(args.repaired).read_text(encoding="utf-8"))
    gold_all = parse_md_paragraphs(Path(args.golden).read_text(encoding="utf-8"))

    # References 区（"## References" 之后）不参与正文对照：修复侧已表格化
    # （| 编号 | 文献 |），golden 侧是逐条 [N] 段落——格式差异非段落修复
    def _before_refs(paras):
        out, in_refs = [], False
        for p in paras:
            if p.kind == "heading" and re.match(r"^##?\s*references$",
                                                p.text.strip(), re.I):
                in_refs = True
            if not in_refs:
                out.append(p)
        return out

    rep = [p for p in _before_refs(rep_all) if p.kind == "body"]
    gold = [p for p in _before_refs(gold_all) if p.kind == "body"]

    # 顺序敏感整段 Dice 贪心对齐（前缀↔整段会掉进 Dice 分母陷阱：
    # 18 词前缀 vs 200 词整段最大 0.165 < 0.3 → 全不匹配）
    matched: list[dict] = []
    search_start = 0
    for g in gold:
        if not g.norm:
            continue
        best_i, best_d = -1, 0.0
        for i in range(search_start, len(rep)):
            d = _dice(g.norm, rep[i].norm)
            if d > best_d:
                best_d, best_i = d, i
        if best_i < 0 or best_d < 0.4:
            matched.append({"type": "gold_unmatched", "dice": round(best_d, 3),
                            "gold": g.text[:90]})
            continue
        if best_d >= 0.6:
            matched.append({"type": "ok", "dice": round(best_d, 3),
                            "gold": g.text[:60]})
        else:
            matched.append({"type": "text_diff", "dice": round(best_d, 3),
                            "gold": g.text[:90],
                            "repaired": rep[best_i].text[:90]})
        search_start = best_i + 1
    for i in range(search_start, len(rep)):
        matched.append({"type": "repair_extra", "repaired": rep[i].text[:90]})

    stats = {"golden": len(gold), "repaired": len(rep),
             "ok": sum(1 for m in matched if m["type"] == "ok"),
             "text_diff": sum(1 for m in matched if m["type"] == "text_diff"),
             "gold_unmatched": sum(1 for m in matched if m["type"] == "gold_unmatched"),
             "repair_extra": sum(1 for m in matched if m["type"] == "repair_extra")}
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "golden_report.json").write_text(
        json.dumps({"stats": stats, "items": matched},
                   ensure_ascii=False, indent=1), encoding="utf-8")

    print("修复后段落流 vs golden 对照")
    print(f"golden 正文段={stats['golden']}  修复后正文段={stats['repaired']}")
    print(f"一致(Dice≥0.6)={stats['ok']}  文本差异={stats['text_diff']}  "
          f"golden 未匹配={stats['gold_unmatched']}  修复侧多出={stats['repair_extra']}")
    for m in matched:
        if m["type"] != "ok":
            print(f"[{m['type']}] {json.dumps(m, ensure_ascii=False)[:160]}")
    print(f"详细: {out_dir / 'golden_report.json'}")


if __name__ == "__main__":
    main()
