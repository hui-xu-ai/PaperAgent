# -*- coding: utf-8 -*-
"""rule_library 单测（P-ENHANCE R03：规则库存储/schema/导出导入/合并冲突）"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "paperparse"))

from paperparse.core import rule_library as rl  # noqa: E402


def _rule(**kw):
    r = {"rule_id": "R-20260822-001", "category": "spacing",
         "pattern_type": "regex", "pattern": "ofx",
         "replacement": "of x", "confidence": 0.9, "source": "mining"}
    r.update(kw)
    return r


# ---------- 校验 ----------

def test_validate_ok():
    assert rl.validate_rule(_rule()) == []


def test_validate_missing_field():
    errs = rl.validate_rule({})
    assert any("category" in e or "rule_id" in e for e in errs)


def test_validate_bad_values():
    assert any("category" in e for e in rl.validate_rule(_rule(category="bogus")))
    assert any("confidence" in e for e in rl.validate_rule(_rule(confidence=1.5)))
    assert any("scope" in e for e in rl.validate_rule(_rule(scope="weird")))
    assert any("pattern" in e for e in rl.validate_rule(_rule(pattern_type="regex",
                                                             pattern="[unclosed")))


def test_validate_evidence():
    r = _rule(evidence=[{"paper": "p1", "before": "a", "after": "b"}])
    assert rl.validate_rule(r) == []
    assert any("evidence" in e for e in rl.validate_rule(_rule(evidence=[{"paper": "x"}])))


# ---------- 增删改查 ----------

def test_add_and_load(tmp_path):
    r = _rule()
    out = rl.add_rule(tmp_path, r, level="learned")
    assert out["rule_id"].startswith("R-")
    assert out["level"] == "learned"
    assert out["auto_apply"] is True          # confidence 0.9
    loaded = rl.load_rules(tmp_path, level="learned")
    assert len(loaded) == 1
    assert loaded[0]["rule_id"] == out["rule_id"]


def test_add_duplicate_rejected(tmp_path):
    rl.add_rule(tmp_path, _rule(), level="learned")
    try:
        rl.add_rule(tmp_path, _rule(), level="learned")
        assert False, "应拒绝同 pattern"
    except ValueError:
        pass


def test_remove_and_disable(tmp_path):
    r = rl.add_rule(tmp_path, _rule(), level="learned")
    assert rl.set_enabled(tmp_path, r["rule_id"], False)
    assert rl.load_rules(tmp_path, level="learned")[0]["enabled"] is False
    assert rl.remove_rule(tmp_path, r["rule_id"])
    assert rl.load_rules(tmp_path, level="learned") == []


def test_builtin_readonly(tmp_path):
    r = rl.add_rule(tmp_path, _rule(), level="builtin")
    assert rl.remove_rule(tmp_path, r["rule_id"]) is False    # builtin 不可删


def test_load_by_level_and_stats(tmp_path):
    rl.add_rule(tmp_path, _rule(), level="learned")
    rl.add_rule(tmp_path, _rule(category="formula", pattern="x$", replacement="x"),
                level="user")
    by = rl.load_by_level(tmp_path)
    assert len(by["learned"]) == 1 and len(by["user"]) == 1
    st = rl.stats(tmp_path)
    assert st["total"] == 2
    assert st["by_category"]["spacing"] == 1
    assert st["auto_apply"] == 2


# ---------- 导出 / 导入 ----------

def test_export_import_roundtrip(tmp_path):
    r = rl.add_rule(tmp_path, _rule(), level="learned")
    bundle = tmp_path / "bundle.json"
    rl.export_bundle(tmp_path, bundle)
    data = json.loads(bundle.read_text(encoding="utf-8"))
    assert data["schema_version"] == rl.SCHEMA_VERSION
    assert len(data["rules"]) == 1

    # 导入到另一目录
    other = tmp_path / "other"
    res = rl.import_bundle(other, bundle, level="user")
    assert res["imported"] == 1
    loaded = rl.load_rules(other, level="user")
    assert loaded[0]["replacement"] == "of x"
    assert loaded[0]["level"] == "user"


def test_import_conflict_keeps_existing(tmp_path):
    rl.add_rule(tmp_path, _rule(), level="learned")
    bundle = tmp_path / "b.json"
    rl.export_bundle(tmp_path, bundle)
    res = rl.import_bundle(tmp_path, bundle, level="learned")   # 同 id 再导
    assert res["imported"] == 0
    assert len(res["conflicts"]) == 1


# ---------- 合并冲突裁决 ----------

def test_merge_user_overrides_learned():
    learned = [_rule(rule_id="R-20260822-001", level="learned", confidence=0.8,
                     replacement="of x")]
    user = [_rule(rule_id="R-20260822-002", level="user", confidence=0.9,
                  replacement="of the x")]
    res = rl.merge([learned, user])
    assert len(res["rules"]) == 1
    assert res["rules"][0]["replacement"] == "of the x"      # user 优先
    assert len(res["conflicts"]) == 1


def test_merge_confidence_tiebreak():
    low = [_rule(rule_id="R-20260822-001", level="learned", confidence=0.7,
                 replacement="a")]
    high = [_rule(rule_id="R-20260822-002", level="learned", confidence=0.95,
                  replacement="b")]
    res = rl.merge([low, high])
    assert res["rules"][0]["replacement"] == "b"


def test_merge_dedup_same_replacement():
    a = [_rule(rule_id="R-20260822-001", level="learned", hits=1,
               evidence=[{"paper": "p1", "before": "x", "after": "y", "review": "ai"}])]
    b = [_rule(rule_id="R-20260822-002", level="learned", hits=2,
               evidence=[{"paper": "p2", "before": "x", "after": "y", "review": "user"}])]
    res = rl.merge([a, b])
    assert len(res["rules"]) == 1
    assert res["rules"][0]["hits"] == 3
    assert len(res["rules"][0]["evidence"]) == 2
    assert res["conflicts"] == []


def test_default_rules_dir_env(monkeypatch, tmp_path):
    monkeypatch.setenv("RULES_DIR", str(tmp_path))
    assert rl.default_rules_dir() == tmp_path
