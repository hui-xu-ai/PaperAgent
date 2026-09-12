#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P11 重挖规则（AI 裁决定向）：对每篇已有 dual_report.json + review.json（AI verdicts）
→ 重建 DiffItems → mine_clean_rules(verdicts) → 覆盖 mined_rules.json
（不重新解析、不消耗云端配额）"""
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paperparse.core.dual_align import DualReport, DiffItem  # noqa: E402
from paperparse.core.rule_mining import mine_clean_rules, validate_candidates  # noqa: E402
from paperparse.middleware.schema import ParserBlocks  # noqa: E402

ROOT = Path("work/dual")
SKIP = {"adma_mineru"}


def _report_from_json(d: Path) -> DualReport:
    data = json.loads((d / "dual_report.json").read_text(encoding="utf-8"))
    rep = DualReport(pdf=data.get("pdf", ""), pages=data.get("pages", 0),
                     stats=data.get("stats", {}))
    rep.items = [DiffItem(diff_type=it["type"], page=it.get("page", 0),
                          mineru=it.get("mineru", {}), paddleocr=it.get("paddleocr", {}),
                          evidence=it.get("evidence", {})) for it in data.get("items", [])]
    return rep


def _verdicts_from_review(d: Path) -> dict[int, str]:
    """review.json 中带 ai 字段的条目 → {report_idx: verdict}（fuse 已记录 report_idx）"""
    v = {}
    p = d / "review.json"
    if not p.exists():
        return v
    try:
        review = json.loads(p.read_text(encoding="utf-8")).get("items", [])
    except Exception:
        return v
    for it in review:
        ai = it.get("ai")
        ridx = it.get("report_idx")
        if ai and isinstance(ridx, int) and ai.get("verdict") in ("mineru", "paddleocr"):
            v[ridx] = ai["verdict"]
    return v


def main():
    for d in sorted(ROOT.iterdir()):
        if not d.is_dir() or d.name in SKIP:
            continue
        if not (d / "dual_report.json").exists():
            continue
        rep = _report_from_json(d)
        blocks = {"m": None, "p": None}
        m_path, p_path = d / "mineru_blocks.json", d / "paddleocr_blocks.json"
        if not m_path.exists() or not p_path.exists():
            print("%s: 缺 blocks，跳过" % d.name)
            continue
        m_blocks = json.loads(m_path.read_text(encoding="utf-8"))
        p_blocks = json.loads(p_path.read_text(encoding="utf-8"))
        mn = ParserBlocks(source="mineru", pages=rep.pages, blocks=m_blocks)
        pn = ParserBlocks(source="paddleocr", pages=rep.pages, blocks=p_blocks)
        verdicts = _verdicts_from_review(d)
        cands = mine_clean_rules(rep, mn.blocks, pn.blocks, paper=d.name,
                                 verdicts=verdicts)
        cands = validate_candidates(cands, mn.blocks, pn.blocks)
        (d / "mined_rules.json").write_text(
            json.dumps([c.to_rule() for c in cands], ensure_ascii=False, indent=2),
            encoding="utf-8")
        print("%-42s verdicts=%d rules=%d" % (d.name, len(verdicts), len(cands)))
        for c in cands:
            print("    [%s/%s] conf=%.2f %s -> %s" % (c.category, c.pattern_type,
                                                      c.confidence, c.pattern[:35],
                                                      c.replacement[:20]))


if __name__ == "__main__":
    main()
