# -*- coding: utf-8 -*-
"""rule_engine 单测（P-ENHANCE R04 / S6.5 规则引擎）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "paperparse"))

from paperparse.core import rule_engine as re_mod  # noqa: E402
from paperparse.core.rule_library import add_rule  # noqa: E402
from paperparse.middleware.schema import ArticleDocument, Paragraph  # noqa: E402


def _para(pid, text, is_heading=False, pages=None, is_caption=False):
    return Paragraph(para_id=pid, order=0, section="", text_en=text,
                     is_heading=is_heading, is_caption=is_caption,
                     coords={"pages": pages or [1]})


def _doc(paras, authors=None):
    from paperparse.middleware.schema import ArticleMetadata
    meta = ArticleMetadata(title="t", authors=authors or [])
    return ArticleDocument(metadata=meta, paragraphs=paras)


def _add(tmp_path, cat, pat, repl, conf=1.0, auto=True):
    add_rule(tmp_path, {"category": cat, "pattern_type": "regex", "pattern": pat,
                        "replacement": repl, "confidence": conf,
                        "auto_apply": auto, "source": "mining"}, level="learned")


def test_of_join_auto_fix(tmp_path):
    _add(tmp_path, "spacing", r"(?<![a-z])(of)([a-z]{7,})(?![a-z])", r"\1 \2")
    doc = _doc([_para("P001", "the ofconductivity value ofmultimodal x")])
    rep = re_mod.apply_rules(doc, tmp_path)
    assert "of conductivity" in doc.paragraphs[0].text_en
    assert "of multimodal" in doc.paragraphs[0].text_en
    assert rep.to_dict()["fixed"] == 1          # 一段两词（计数按段落）
    assert rep.applied[0]["rule_id"] and rep.applied[0]["category"] == "spacing"


def test_low_confidence_goes_pending(tmp_path):
    # 用不撞内置 of-规则的自定义模式（R09：apply_rules 始终合并内嵌 builtin）
    _add(tmp_path, "spacing", r"\b(xofy)\b", r"x of y", conf=0.7, auto=False)
    doc = _doc([_para("P001", "the xofy value")])
    rep = re_mod.apply_rules(doc, tmp_path)
    assert doc.paragraphs[0].text_en == "the xofy value"   # 未自动修
    assert rep.to_dict()["pending_count"] == 1
    assert rep.pending[0]["confidence"] == 0.7


def test_formula_rule_literal_replacement(tmp_path):
    # 无捕获组 → 字面量替换（反斜杠安全，KNOWN_FIXES 语义）
    _add(tmp_path, "formula", r"\\bar\s*\{\s*2\s*\}", r"^2")
    doc = _doc([_para("P001", "A m^{\\bar{2}} kg")])
    rep = re_mod.apply_rules(doc, tmp_path)
    assert "A m^{^2} kg" in doc.paragraphs[0].text_en     # \bar{2} → ^2
    assert rep.to_dict()["fixed"] == 1


def test_metadata_authors_clean(tmp_path):
    doc = _doc([_para("P001", "body")], authors=["Alice", "and Bob", "Carol*"])
    rep = re_mod.apply_rules(doc, tmp_path)
    assert doc.metadata.authors == ["Alice", "Bob", "Carol"]
    assert any(e["rule_id"] == "META-authors-and" for e in rep.applied)


def test_latex_syntax_to_pending(tmp_path):
    doc = _doc([_para("P001", "value is $x = 5 nm")])     # $ 未配对
    rep = re_mod.apply_rules(doc, tmp_path)
    assert any(e["category"] == "latex" for e in rep.pending)


def test_mid_join_detected_pending(tmp_path, monkeypatch):
    # 词中粘连检测独立可用（apply_rules 默认不触发；R05 AI 审查按需启用）
    doc = _doc([_para("P001", "electrodeandconductor layers", pages=[1])])
    rep = re_mod.RuleReport()
    re_mod.apply_spacing_local(doc, "nonexistent.pdf", rep)
    assert any(e["kind"] == "mid_join" for e in rep.pending)
    assert rep.pending[0]["evidence_hint"] in ("本地无空格形式", "本地含空格形式")


def test_empty_rules_no_crash(tmp_path):
    doc = _doc([_para("P001", "plain text $x$ ok")])
    rep = re_mod.apply_rules(doc, tmp_path)
    assert rep.to_dict()["fixed"] == 0
    # latex 检测仍会报（无规则也检测语法）
    assert len(rep.pending) == 0 or rep.pending[0]["category"] == "latex"


def test_category_order_heading_included(tmp_path):
    _add(tmp_path, "layout", r"\bofInterest\b", "of Interest")
    doc = _doc([_para("P001", "Conflict ofInterest", is_heading=True)])
    rep = re_mod.apply_rules(doc, tmp_path)
    assert doc.paragraphs[0].text_en == "Conflict of Interest"
    assert rep.to_dict()["fixed"] == 1


def test_save_report(tmp_path):
    _add(tmp_path, "spacing", r"^(of)([a-z]{7,})$", r"\1 \2")
    doc = _doc([_para("P001", "ofconductivity")])
    rep = re_mod.apply_rules(doc, tmp_path)
    out = re_mod.save_report(rep, tmp_path / "rule_report.json")
    import json
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["fixed"] == 1
    assert data["applied"][0]["before"] == "ofconductivity"
