#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""consensus_scan 单测：C 层 AI 共识扫描（学习闭环样本源）

覆盖：
  1. find_boundary_candidates：词典外带电荷 token 提取（词典内/无电荷跳过）
  2. _validate_suggestion：元素表校验 AI 建议（非法/幻觉拒绝）
  3. ai_scan：fake provider 解析、AI 不可用降级
  4. build_scan_review_items：review.json item 结构 + evidence
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paperparse.core.consensus_fix import (  # noqa: E402
    find_boundary_candidates, reset_cache)
from paperparse.core.consensus_scan import (  # noqa: E402
    _parse_json_list, _validate_suggestion, ai_scan, build_scan_review_items)


@pytest.fixture(autouse=True)
def _clean():
    reset_cache()
    yield
    reset_cache()


class FakeProvider:
    """fake OpenAI 兼容 provider：返回固定 JSON"""

    def __init__(self, text: str, available=True):
        self._text = text
        self._available = available

    def available(self):
        return self._available

    def complete(self, messages, **kw):
        if not self._available:
            raise RuntimeError("unavailable")
        return self._text


class TestBoundaryCandidates:
    def test_out_of_dict_extracted(self):
        # LaFeO3−（钙钛矿，不在词典）→ 边界候选
        cands = find_boundary_candidates("研究了 LaFeO3− 的结构")
        assert any(c["token"] == "LaFeO3−" for c in cands)

    def test_in_dict_skipped(self):
        # BF− 词典命中（review 档由确定性层管）→ 不送 AI
        cands = find_boundary_candidates("BF− 离子")
        assert cands == []

    def test_complete_form_skipped(self):
        # BF₄⁻ 完整正确形态也在词典覆盖 → 不送 AI
        cands = find_boundary_candidates("BF₄⁻ 离子")
        assert cands == []

    def test_no_charge_skipped(self):
        cands = find_boundary_candidates("BF 与 LIG")
        assert cands == []

    def test_protected_skipped(self):
        cands = find_boundary_candidates("Nafion+ 溶液")   # 专名保护 → 不提取
        assert cands == []


class TestValidateSuggestion:
    def test_valid(self):
        chk = _validate_suggestion("BF4-")
        assert chk and chk["elements"] == {"B": 1, "F": 4} and chk["charge"] == -1

    def test_multi_charge(self):
        chk = _validate_suggestion("SO4-2")
        assert chk and chk["charge"] == -2

    def test_invalid_rejected(self):
        assert _validate_suggestion("BF4-(") is None
        assert _validate_suggestion("") is None
        assert _validate_suggestion("Nu2-") is None      # 希腊污染/非法元素


class TestAiScan:
    _C = {"token": "BF−", "elements": {"B": 1, "F": 1}, "charge": -1}

    def test_unavailable_fallback(self):
        out = ai_scan([self._C], provider=FakeProvider("", False))
        assert len(out) == 1 and not out[0]["valid"]

    def test_parse_valid(self):
        fake = FakeProvider('[{"id":0,"suggestion":"BF4-","unicode":"BF₄⁻",'
                            '"reason":"缺下标4","confidence":0.9}]')
        out = ai_scan([self._C], provider=fake)
        assert len(out) == 1
        assert out[0]["valid"] and out[0]["suggestion"] == "BF4-"

    def test_hallucination_rejected(self):
        # AI 输出非法式 → valid=False
        fake = FakeProvider('[{"id":0,"suggestion":"XxYy-","confidence":0.9}]')
        out = ai_scan([self._C], provider=fake)
        assert not out[0]["valid"]

    def test_parse_json_list(self):
        assert _parse_json_list("```json\n[{\"a\":1}]\n```") == [{"a": 1}]
        assert _parse_json_list("无 JSON") == []
        assert _parse_json_list('[{"a":1},{"b":2}]') == [{"a": 1}, {"b": 2}]


class TestBuildReviewItems:
    def _para(self, pid="p1", text="含 BF− 的段落", kind="body", md_idx=(3,)):
        from types import SimpleNamespace
        return SimpleNamespace(para_id=pid, kind=kind, text=text, md_idx=list(md_idx))

    def test_item_shape(self):
        paras = [self._para()]
        by_token = {"p1": [{"token": "BF−"}]}
        scans = [{"token": "BF−", "suggestion": "BF4-", "unicode": "BF₄⁻",
                  "reason": "缺下标4", "confidence": 0.9, "valid": True}]
        items = build_scan_review_items(paras, scans, by_token)
        assert len(items) == 1
        it = items[0]
        assert it["evidence"]["kind"] == "domain_ai"
        assert it["evidence"]["suggestion"] == "BF4-"
        assert it["ai"]["verdict"] == "paddleocr"
        assert it["ai"]["applied"] is False
        assert "BF₄⁻" in it["paddleocr"]["text"]      # 建议全文含校正
        assert it["paddleocr"]["block_id"] == "paddle-p1"
