#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P11 批量结果汇总：扫描 work/dual/<doi>/ 各篇报告 → 汇总表（stats/仲裁/规则）"""
import json, sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path("work/dual")
SKIP = {"adma_mineru"}


def main():
    rows = []
    for d in sorted(ROOT.iterdir()):
        if not d.is_dir() or d.name in SKIP:
            continue
        rep_path = d / "dual_report.json"
        if not rep_path.exists():
            continue
        try:
            rep = json.loads(rep_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        fused = {}
        if (d / "fused.json").exists():
            fused = json.loads((d / "fused.json").read_text(encoding="utf-8")).get("stats", {})
        review = {}
        if (d / "review.json").exists():
            review = json.loads((d / "review.json").read_text(encoding="utf-8")).get("ai", {})
        rules = []
        if (d / "mined_rules.json").exists():
            rules = json.loads((d / "mined_rules.json").read_text(encoding="utf-8"))
        rows.append({
            "doi": d.name,
            "stats": rep.get("stats", {}),
            "fuse": fused,
            "ai": review,
            "rules": len(rules),
            "rules_high": sum(1 for r in rules if r.get("confidence", 0) >= 0.8),
        })

    if not rows:
        print("无批量产物（work/dual/ 下无报告）")
        return
    print("%-42s %7s %5s %5s %5s %5s %5s %5s %5s %5s" % (
        "doi", "aligned", "conf", "fmt", "missM", "insrt", "arb", "unres", "rules", "r≥0.8"))
    for r in rows:
        s = r["stats"]
        print("%-42s %7d %5d %5d %5d %5d %5s %5s %5d %5d" % (
            r["doi"], s.get("aligned", 0), s.get("text_conflict", 0),
            s.get("format_diff", 0), s.get("missing_mineru", 0),
            r["fuse"].get("inserted", 0),
            r["ai"].get("arbitrated", "-"), r["ai"].get("unresolved", "-"),
            r["rules"], r["rules_high"]))
    # 汇总
    print("\n合计：%d 篇" % len(rows))
    print("  规则候选总数=%d（≥0.8 高置信=%d）" % (
        sum(r["rules"] for r in rows), sum(r["rules_high"] for r in rows)))
    print("  AI 仲裁总数=%d unresolved=%d" % (
        sum(r["ai"].get("arbitrated", 0) for r in rows),
        sum(r["ai"].get("unresolved", 0) for r in rows)))


if __name__ == "__main__":
    main()
