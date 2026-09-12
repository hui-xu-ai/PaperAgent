#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: tests/test_rule_mining.py
功能: P11-4 清洗规则挖掘单元测试
对外接口: 无（测试）
版本: v1.0.0 (2026-08-22)
版本历史:
  v1.0.0 初始版本
"""
import json
from types import SimpleNamespace

from paperparse.core.rule_mining import (mine_clean_rules, mine_char_rules,
                                         validate_candidates)
from paperparse.middleware.schema import TextBlock


def _m(bid, page, text, kind="body"):
    return TextBlock(block_id=bid, page=page, bbox=(0, 0, 10, 10),
                     text=text, kind=kind, source="mineru")


def _p(bid, page, text, kind="body"):
    return TextBlock(block_id=bid, page=page, bbox=(0, 0, 10, 10),
                     text=text, kind=kind, source="paddleocr")


def _mine(mineru, po, verdicts=None):
    """2026-08-26：align_dual 已随 dual_align 归档——测试内简化对齐
    （等长成对块直接构造 text_conflict items；rule_mining 只读 items 字段）。"""
    items = []
    for mi, pi in zip(mineru, po):
        items.append(SimpleNamespace(
            diff_type="text_conflict", page=1,
            mineru={"block_id": mi.block_id, "text": mi.text, "kind": mi.kind},
            paddleocr={"block_id": pi.block_id, "text": pi.text, "kind": pi.kind},
            evidence={}))
    rep = SimpleNamespace(items=items)
    return mine_clean_rules(rep, mineru, po, paper="test-paper",
                            verdicts=verdicts or {})


def test_spacing_rule_mined():
    """"1 of15" → "1 of 15" 空格粘连 → spacing 正则规则"""
    mineru = [_m("M%d" % i, 1, "2407106 (1 of15)") for i in range(3)]
    po = [_p("P%d" % i, 1, "2407106 (1 of 15)") for i in range(3)]
    rules = _mine(mineru, po)
    spacing = [r for r in rules if r.pattern_type == "regex"]
    assert len(spacing) >= 1
    r = spacing[0]
    assert r.category == "spacing"
    assert r.pattern == "of(15)" and r.replacement == "of 15"
    assert r.confidence >= 0.6 and len(r.evidence) >= 1


def test_word_diff_rule_mined():
    """"Eficient" ↔ "Efficient" 词差异 → AI 裁决 verdict=paddleocr（mineru 脏）→ dict 规则"""
    mineru = [_m("M1", 1, "Eficient ion transport is important for performance.")]
    po = [_p("P1", 1, "Efficient ion transport is important for performance.")]
    rules = _mine(mineru, po, verdicts={0: "paddleocr"})   # mineru 侧拼写错误
    dict_rules = [r for r in rules if r.pattern_type == "dict"]
    assert len(dict_rules) == 1
    mapping = json.loads(dict_rules[0].pattern)
    assert mapping == {"Eficient": "Efficient"}


def test_word_diff_without_verdict_no_rule():
    """无 AI 裁决（方向未知）→ 不产 dict 规则"""
    mineru = [_m("M1", 1, "Eficient ion transport is important for performance.")]
    po = [_p("P1", 1, "Efficient ion transport is important for performance.")]
    rules = _mine(mineru, po)
    assert not [r for r in rules if r.pattern_type == "dict"]


def test_validation_filters_clean_side_hits():
    """规则若会在干净侧误伤（如过宽正则）→ 丢弃或降置信"""
    # 干净侧（paddleocr）已含目标空格 → 过宽规则 "of(15)" 不命中（有空格）→ 保留
    mineru = [_m("M1", 1, "page 1 of15") for _ in range(2)]
    po = [_p("P1", 1, "page 1 of 15") for _ in range(2)]
    rules = _mine(mineru, po)
    rules = validate_candidates(rules, mineru, po)
    assert any(r.pattern_type == "regex" for r in rules)
    # 构造误伤：干净侧文本本身含 "of15"（错误也出现在干净侧）→ 规则被丢弃
    mineru2 = [_m("M1", 1, "see of15 here"), _m("M2", 1, "a of15 b")]
    po2 = [_p("P1", 1, "of15 also appears in clean side! of15")]
    rules2 = _mine(mineru2, po2)
    rules2 = validate_candidates(rules2, mineru2, po2)
    assert not any(r.pattern_type == "regex" and r.pattern == "of(15)" for r in rules2)


def test_no_rule_without_conflicts():
    mineru = [_m("M1", 1, "Identical text in both channels.")]
    po = [_p("P1", 1, "Identical text in both channels.")]
    assert _mine(mineru, po) == []


# ---------- P15 字符级规则挖掘（mine_char_rules） ----------


def _arb(verdict, conf=0.95):
    from paperparse.core.dual_ai_review import Arbitration
    return Arbitration(id=0, diff_type="text_conflict", page=1,
                       verdict=verdict, reason="", confidence=conf)


def test_mine_char_rules_directed_pair():
    """verdict=paddleocr → mineru 脏（Eficient→Efficient）→ dict 候选，方向正确"""
    cfl = [{"type": "text_conflict", "page": 1,
            "mineru": {"text": "Eficient"},
            "paddleocr": {"text": "Efficient"}}]
    rules = mine_char_rules(cfl, [_arb("paddleocr")], paper="p1")
    assert len(rules) == 1
    r = rules[0]
    assert r.pattern_type == "dict"
    assert r.direction == "mineru->paddleocr"
    assert r.clean_channel == "paddleocr"
    assert json.loads(r.pattern) == {"Eficient": "Efficient"}
    assert r.confidence < 0.9        # 单篇低证据 → 保守（不 auto）


def test_mine_char_rules_single_letter_skipped():
    """单字母 insert（''→'f' 断词修复）→ 不产规则（词边界执行器误伤风险）"""
    cfl = [{"type": "text_conflict", "page": 1,
            "mineru": {"text": ""},
            "paddleocr": {"text": "f"}}]
    assert mine_char_rules(cfl, [_arb("paddleocr")], paper="p1") == []


def test_mine_char_rules_undirected_skipped():
    """both/neither/unresolved → 方向未知 → 不产规则"""
    cfl = [{"type": "text_conflict", "page": 1,
            "mineru": {"text": "Eficient"},
            "paddleocr": {"text": "Efficient"}}]
    for v in ("both", "neither", "unresolved"):
        assert mine_char_rules(cfl, [_arb(v)], paper="p1") == []


def test_mine_char_rules_reverse_direction():
    """verdict=mineru → paddleocr 脏（方向反向）"""
    cfl = [{"type": "text_conflict", "page": 1,
            "mineru": {"text": "Efficient"},
            "paddleocr": {"text": "Eficient"}}]
    rules = mine_char_rules(cfl, [_arb("mineru")], paper="p1")
    assert len(rules) == 1
    assert rules[0].direction == "paddleocr->mineru"
    assert rules[0].clean_channel == "mineru"


def test_persist_rules(tmp_work):
    from paperparse.core.rule_mining import persist_rules
    mineru = [_m("M1", 1, "2407106 (1 of15)")]
    po = [_p("P1", 1, "2407106 (1 of 15)")]
    rules = _mine(mineru, po)
    added = persist_rules(rules, tmp_work / "rules")
    # 只入库 regex 规则（dict 词差异候选不全局入库）
    assert len(added) == len([r for r in rules if r.pattern_type == "regex"])
    assert added and all(r["pattern_type"] == "regex" for r in added)
    from paperparse.core.rule_library import load_rules
    loaded = load_rules(tmp_work / "rules", level="learned")
    assert any(r["pattern"] == "of(15)" for r in loaded)
    assert all(r["source"] == "mining" for r in loaded)
