#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P11 跨论文规则聚合：合并 9 篇 mined_rules.json → 按 (category, pattern_type, pattern)
合并证据 → 重算置信度（证据量缩放）→ 高置信（≥0.85）可选入库 learned 层
用法: python tools/aggregate_dual_rules.py [--persist] [--rules-dir <dir>]
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path("work/dual")
SKIP = {"adma_mineru"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--persist", action="store_true", help="高置信规则入库 learned 层")
    ap.add_argument("--rules-dir", default=None)
    args = ap.parse_args()

    merged: dict[tuple, dict] = {}
    for d in sorted(ROOT.iterdir()):
        if not d.is_dir() or d.name in SKIP:
            continue
        p = d / "mined_rules.json"
        if not p.exists():
            continue
        for r in json.loads(p.read_text(encoding="utf-8")):
            key = (r["category"], r["pattern_type"], r["pattern"])
            m = merged.setdefault(key, {
                "category": r["category"], "pattern_type": r["pattern_type"],
                "pattern": r["pattern"], "replacement": r.get("replacement", ""),
                "papers": [], "evidence": [], "confidences": []})
            m["papers"].append(d.name)
            m["confidences"].append(r.get("confidence", 0))
            for ev in r.get("evidence", []):
                ev = dict(ev)
                ev["paper"] = d.name
                m["evidence"].append(ev)

    rows = []
    for key, m in merged.items():
        n_ev = len(m["evidence"])
        n_papers = len(set(m["papers"]))
        # dict 规则方向裁决：evidence 的 before/after 含每样本方向（before=脏, after=干净）
        # 主导方向占比 < 0.75 → 方向歧义（如 PNAS↔BNAS 两通道互相出错）→ 跳过；
        # 反向主导 → 翻转规则方向（防反向 auto 规则损坏文本）
        direction_ratio = 1.0
        if m["pattern_type"] == "dict":
            try:
                pat_map = json.loads(m["pattern"])
                dirty, clean = next(iter(pat_map.items()))
            except Exception:
                dirty = clean = None
            if not dirty or not clean:
                continue
            fwd = [e for e in m["evidence"] if e.get("before") == dirty and e.get("after") == clean]
            rev = [e for e in m["evidence"] if e.get("before") == clean and e.get("after") == dirty]
            total = len(fwd) + len(rev)
            if total == 0:
                continue
            ratio = max(len(fwd), len(rev)) / total
            if ratio < 0.75:
                continue          # 方向歧义 → 不产规则
            direction_ratio = ratio
            if len(rev) > len(fwd):
                m["pattern"] = json.dumps({clean: dirty}, ensure_ascii=False)
                m["evidence"] = [dict(e, before=e["after"], after=e["before"]) for e in rev]
            else:
                m["evidence"] = [dict(e) for e in fwd]
            n_ev = len(m["evidence"])
        # 置信度重算：regex 证据≥3→0.85；dict 随证据量缩放（上限 0.9，方向占比加权）
        if m["pattern_type"] == "regex":
            conf = 0.85 if n_ev >= 3 else (0.6 if n_ev >= 1 else 0)
        else:
            conf = round(min(0.9, 0.5 * n_ev / 3.0 * direction_ratio), 2)
        rows.append((key, m, n_ev, n_papers, conf))

    rows.sort(key=lambda x: -x[3])   # 按论文数降序
    print("合并后规则模式: %d 条（跨论文优先）" % len(rows))
    out_rules = []
    for key, m, n_ev, n_papers, conf in rows:
        print("  %-6s %-6s %-45s conf=%.2f 论文=%d 证据=%d" % (
            m["category"], m["pattern_type"], m["pattern"][:45], conf, n_papers, n_ev))
        if conf >= 0.85:
            repl = m["replacement"]
            if m["pattern_type"] == "dict" and not repl:
                repl = m["pattern"]   # dict 规则 replacement=pattern（schema 兼容）
            out_rules.append({
                "category": m["category"], "pattern_type": m["pattern_type"],
                "pattern": m["pattern"], "replacement": repl,
                "confidence": conf, "source": "mining",
                "scope": "global", "evidence": m["evidence"][:20],
                "description": "双通道跨论文聚合（%d 篇 %d 证据）" % (n_papers, n_ev)})

    out = ROOT / "merged_rules.json"
    out.write_text(json.dumps(out_rules, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n高置信（≥0.85）: %d 条 → %s" % (len(out_rules), out))

    # 完整候选清单（含低置信，供人工复核）：JSON + 人读 MD
    _export_all(rows, out_rules)

    if args.persist and out_rules:
        from paperparse.core.rule_library import add_rule, default_rules_dir
        d = Path(args.rules_dir) if args.rules_dir else default_rules_dir()
        added = []
        for r in out_rules:
            try:
                added.append(add_rule(d, r, level="learned"))
            except ValueError as e:
                if "已存在" in str(e):
                    continue
                raise
        print("入库 learned: %d 条" % len(added))


def _export_all(rows, high_rules):
    """[局部] 全部候选（含低置信）落盘：rule_candidates_all.json + rule_candidates_review.md"""
    import datetime
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    all_data = []
    for key, m, n_ev, n_papers, conf in rows:
        direction = ""
        if m["pattern_type"] == "dict":
            try:
                pat_map = json.loads(m["pattern"])
                dirty, clean = next(iter(pat_map.items()))
                direction = "%s → %s" % (dirty, clean)
            except Exception:
                direction = ""
        all_data.append({
            "category": m["category"], "pattern_type": m["pattern_type"],
            "pattern": m["pattern"], "replacement": m.get("replacement", ""),
            "direction": direction, "confidence": conf,
            "papers": sorted(set(m["papers"])), "evidence_count": n_ev,
            "evidence": m["evidence"][:10],
            "persisted": any(r.get("pattern") == m["pattern"] for r in high_rules),
        })
    all_data.sort(key=lambda r: -r["confidence"])
    (ROOT / "rule_candidates_all.json").write_text(
        json.dumps(all_data, ensure_ascii=False, indent=2), encoding="utf-8")

    # 人读版 MD
    lines = [
        "# 双通道清洗规则候选汇总（P11，9 篇语料）",
        "",
        "生成时间：%s　　合计 %d 条候选（高置信 ≥0.85：%d 条）" % (now, len(all_data), len(high_rules)),
        "",
        "> 已入库 learned：`diferent→different`（conf 0.9 auto）、`of(15)→of 15`（conf 0.85 待确认）等。",
        "> 低置信候选未经人工复核**不会自动执行**（规则引擎置信度门控）；复核后可用",
        "> `aggregate_dual_rules.py --persist` 或规则库工具转正。",
        "",
        "## 高置信（≥0.85）",
        "",
        "| 规则 | 类型 | 置信 | 证据 | 论文 |",
        "|------|------|------|------|------|",
    ]
    for r in all_data:
        if r["confidence"] < 0.85:
            continue
        lines.append("| `%s` → `%s` | %s | %.2f | %d | %s |" % (
            _pat(r["pattern"]), _repl(r), r["pattern_type"],
            r["confidence"], r["evidence_count"], ", ".join(p[-6:] for p in r["papers"])))
    lines += ["", "## 低置信候选（待人工复核，按置信度降序）", "",
              "| 规则 | 类型 | 置信 | 证据 | 论文 | AI/方向 | 证据示例 |",
              "|------|------|------|------|------|---------|----------|"]
    for r in all_data:
        if r["confidence"] >= 0.85:
            continue
        ev = r["evidence"][0] if r["evidence"] else {}
        sample = "`%s`→`%s` (p%s)" % (
            str(ev.get("before", ""))[:18], str(ev.get("after", ""))[:18], ev.get("page", "?"))
        lines.append("| `%s` → `%s` | %s | %.2f | %d | %s | %s | %s |" % (
            _pat(r["pattern"]), _repl(r), r["pattern_type"],
            r["confidence"], r["evidence_count"], ", ".join(p[-6:] for p in r["papers"]),
            r["direction"] or "—", sample))
    (ROOT / "rule_candidates_review.md").write_text("\n".join(lines), encoding="utf-8")
    print("完整候选清单: %s / %s" % (ROOT / "rule_candidates_all.json",
                                  ROOT / "rule_candidates_review.md"))


def _pat(pattern: str) -> str:
    """[局部] 规则 pattern 显示：dict JSON 取键，regex 原样截断"""
    try:
        d = json.loads(pattern)
        if isinstance(d, dict) and d:
            return next(iter(d))
    except Exception:
        pass
    return pattern[:40]


def _repl(r: dict) -> str:
    """[局部] 规则替换方向显示：dict 取值（r.direction 已含完整方向）"""
    if r["pattern_type"] == "dict":
        return r.get("direction", "").split("→")[-1].strip() or "（dict）"
    return r.get("replacement") or "（空）"


if __name__ == "__main__":
    main()
