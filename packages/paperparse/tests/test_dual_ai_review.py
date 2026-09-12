#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: tests/test_dual_ai_review.py
功能: P11 AI 仲裁单测（provider 打桩）
对外接口: 无（测试）
版本: v1.0.0 (2026-08-23)
版本历史:
  v1.0.0 初始版本
"""
import pytest

from paperparse.core.dual_ai_review import (Arbitration, SiliconFlowProvider,
                                            _parse_verdicts, arbitrate,
                                            build_arbitration)


def _items():
    return [
        {"type": "text_conflict", "page": 1, "evidence": {"dice": 0.86},
         "mineru": {"text": "cost-efective procedures"},
         "paddleocr": {"text": "cost-effective procedures"}},
        {"type": "format_diff", "page": 8, "evidence": {"dice": 0.9, "latex_valid": True},
         "mineru": {"text": r"$3.27 \mathrm{S}$"}, "paddleocr": {"text": "3.27 S"}},
        {"type": "format_diff", "page": 9, "evidence": {"dice": 0.88, "latex_valid": False},
         "mineru": {"text": r"\frac{a}{"}, "paddleocr": {"text": "a/b"}},
        {"type": "missing_mineru", "page": 6,
         "mineru": {}, "paddleocr": {"text": "extra paragraph"}},
    ]


class FakeProvider:
    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    def available(self):
        return True

    def complete(self, messages, **kw):
        self.calls.append(messages)
        return self._resp


def test_parse_verdicts():
    assert _parse_verdicts('[{"id":0,"verdict":"mineru","reason":"x","confidence":0.9}]') == [
        {"id": 0, "verdict": "mineru", "reason": "x", "confidence": 0.9}]
    # 代码围栏 + 多余文本
    assert len(_parse_verdicts("```json\n[{\"id\":1,\"verdict\":\"both\",\"reason\":\"y\"}]\n```")) == 1
    assert _parse_verdicts("not json") == []


def test_arbitrate_targets_only_conflicts():
    """只仲裁 text_conflict + latex_valid=False 的 format_diff"""
    fp = FakeProvider('[{"id":0,"verdict":"paddleocr","reason":"拼写","confidence":0.95},'
                      '{"id":2,"verdict":"both","reason":"格式差异","confidence":0.9}]')
    out = arbitrate(_items(), provider=fp)
    assert len(out) == 2
    assert out[0].id == 0 and out[0].verdict == "paddleocr"
    assert out[1].id == 2 and out[1].verdict == "both"


def test_arbitrate_unresolved_without_key(monkeypatch):
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    out = arbitrate(_items(), provider=SiliconFlowProvider(api_key=""))
    assert all(a.verdict == "unresolved" for a in out)


def test_build_arbitration_merges():
    items = [{"report_idx": 3, "page": 1, "dice": 0.86,
              "mineru": {"text": "a"}, "paddleocr": {"text": "b"}},
             {"report_idx": 7, "page": 2, "dice": 0.9,
              "mineru": {"text": "c"}, "paddleocr": {"text": "d"}}]
    arb = [Arbitration(id=3, diff_type="text_conflict", page=1, verdict="mineru",
                       reason="r", confidence=0.8),
           Arbitration(id=7, diff_type="format_diff", page=2, verdict="both",
                       reason="fmt", confidence=0.7)]
    merged = build_arbitration(items, arb)
    assert merged[0]["ai"]["verdict"] == "mineru"
    assert merged[1]["ai"]["verdict"] == "both"
    assert len(merged) == 2
