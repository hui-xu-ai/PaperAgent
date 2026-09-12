# -*- coding: utf-8 -*-
"""ai_review 单测（P-ENHANCE R05：AI 审查学习闭环）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "paperparse"))

from paperparse.core import ai_review as ar  # noqa: E402
from paperparse.core.rule_library import load_rules  # noqa: E402
from paperparse.llm.client import FakeAI  # noqa: E402
from paperparse.middleware.schema import ArticleDocument, ArticleMetadata, Paragraph  # noqa: E402


def _para(pid, text, is_heading=False, pages=None):
    return Paragraph(para_id=pid, order=0, section="", text_en=text,
                     is_heading=is_heading, coords={"pages": pages or [1]})


def _doc(paras, authors=None):
    return ArticleDocument(metadata=ArticleMetadata(title="t", authors=authors or []),
                           paragraphs=paras)


def test_build_review_items_mid_join():
    doc = _doc([_para("P001", "electrodeandconductor layers and maintain stability")])
    items = ar.build_review_items(doc, enable_mid_join=True)
    kinds = [i["kind"] for i in items]
    assert "mid_join" in kinds
    # maintain 是合法词（含 in 子串）也会被检出——AI 负责判断（ignore）
    targets = {i["target"] for i in items if i["kind"] == "mid_join"}
    assert "electrodeandconductor" in targets


def test_build_review_items_latex():
    doc = _doc([_para("P001", "value is $x = 5 nm")])
    items = ar.build_review_items(doc, enable_mid_join=False)
    assert any(i["kind"] == "latex_syntax" for i in items)


def test_review_items_fix_applied():
    doc = _doc([_para("P001", "the electrodeandconductor interface")])
    items = ar.build_review_items(doc, enable_mid_join=True)
    mid = next(i for i in items if i["kind"] == "mid_join"
               and i["target"] == "electrodeandconductor")
    ai = FakeAI(['[{"index": %d, "action": "fix", "corrected": "electrode and conductor"}]'
                 % mid["id"]])
    res = ar.review_items([mid], doc, ai=ai)
    assert res["fixed"] == 1
    assert "electrode and conductor" in doc.paragraphs[0].text_en
    assert len(res["candidates"]) == 1
    cand = res["candidates"][0]
    assert cand["confidence"] == 0.7
    assert cand["source"] == "ai_review"
    assert cand["auto_apply"] is False


def test_review_items_ignore_keeps_text():
    doc = _doc([_para("P001", "the maintain stability of the system")])
    items = ar.build_review_items(doc, enable_mid_join=True)
    mid = next(i for i in items if i["kind"] == "mid_join"
               and i["target"] == "maintain")
    ai = FakeAI(['[{"index": %d, "action": "ignore", "note": "maintain 是合法词"}]'
                 % mid["id"]])
    res = ar.review_items([mid], doc, ai=ai)
    assert res["fixed"] == 0
    assert doc.paragraphs[0].text_en == "the maintain stability of the system"


def test_review_items_empty_list():
    ai = FakeAI()
    res = ar.review_items([], _doc([_para("P001", "x")]), ai=ai)
    assert res["fixed"] == 0 and res["candidates"] == []


def test_parse_decisions_fence_and_garbage():
    raw = '```json\n[{"index": 1, "action": "fix", "corrected": "a b"}, {"index": 2, "action": "ignore"}]\n```'
    d = ar.parse_decisions(raw)
    assert len(d) == 2 and d[0]["index"] == 1
    # 无 JSON → 空
    assert ar.parse_decisions("无法确定，全部忽略") == []


def test_mine_rule_validation():
    cand = ar.mine_rule({"kind": "mid_join", "category": "spacing",
                         "target": "electrodeandconductor"},
                        "electrode and conductor", "拆词")
    assert cand is not None
    assert cand["pattern"] == "electrodeandconductor"     # re.escape 无特殊字符
    assert cand["replacement"] == "electrode and conductor"
    # 太短 target → None
    assert ar.mine_rule({"kind": "x", "target": "ab"}, "a b") is None


def test_apply_corrections_to_user_rules(tmp_path):
    corrections = [
        {"location": "frontmatter", "original": "and Dezhi Wu", "corrected": "Dezhi Wu",
         "category": "metadata", "note": "作者 and 残留"},
        {"location": "body", "original": "Eficient", "corrected": "Efficient",
         "category": "spacing", "note": "OCR 漏字母"},
    ]
    res = ar.apply_corrections(tmp_path, corrections, paper="10.1000/test.1")
    assert res["added"] == 2
    rules = load_rules(tmp_path, level="user")
    assert len(rules) == 2
    meta = next(r for r in rules if r["category"] == "metadata")
    assert meta["source"] == "user_correction"
    assert meta["confidence"] == 1.0
    assert meta["scope"] == "paper:10.1000/test.1"
    assert meta["evidence"][0]["review"] == "user"


def test_apply_corrections_conflict(tmp_path):
    corrections = [{"original": "Eficient", "corrected": "Efficient",
                    "category": "spacing"}]
    ar.apply_corrections(tmp_path, corrections)
    res = ar.apply_corrections(tmp_path, corrections)     # 同 pattern 再转 → 冲突
    assert res["added"] == 0
    assert len(res["conflicts"]) == 1
