# -*- coding: utf-8 -*-
"""rule_library 双源加载测试（R09：内嵌 builtin + 外部 learned/user）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "paperparse"))

from paperparse.core import rule_library as rl  # noqa: E402
from paperparse.core.rule_library import add_rule  # noqa: E402


def test_builtin_rules_dir_resolves(tmp_path):
    # 内嵌根 = base/rules（builtin_base 参数覆盖；默认 asset_root()/rules）
    assert rl.builtin_rules_dir(tmp_path / "embedded") == tmp_path / "embedded" / "rules"


def test_load_all_rules_merges(tmp_path):
    # 内嵌 builtin（builtin_base=tmp_path/embedded）
    embedded = tmp_path / "embedded" / "rules"
    add_rule(embedded, {"category": "spacing", "pattern_type": "regex",
                        "pattern": "ofx", "replacement": "of x",
                        "source": "builtin", "confidence": 1.0}, level="builtin")
    # 外部 learned
    external = tmp_path / "external"
    add_rule(external, {"category": "formula", "pattern_type": "regex",
                        "pattern": "bad", "replacement": "good",
                        "source": "ai_review", "confidence": 0.7}, level="learned")
    rules = rl.load_all_rules(external=external, builtin_base=tmp_path / "embedded")
    assert len(rules) == 2
    levels = {r["level"] for r in rules}
    assert levels == {"builtin", "learned"}
    assert rules[0]["level"] == "builtin"      # builtin 在前


def test_load_all_rules_empty_ok(tmp_path):
    rules = rl.load_all_rules(external=tmp_path / "noexist",
                              builtin_base=tmp_path / "noexist2")
    assert rules == []
