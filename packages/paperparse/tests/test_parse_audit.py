# -*- coding: utf-8 -*-
"""★2026-09-17 L2/L3 解析后审计单测：
· L2 `build_text_layer_audit`：文本层一致性（不依赖任何事前触发判据）
· L3 `assert_quality`：产物质量硬断言
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from paperparse.core.parse_audit import assert_quality, build_text_layer_audit  # noqa: E402


class TestTextLayerAudit:
    def _item(self, text, page_raw, pid="RP001", page=1):
        return {"para_id": pid, "page": page, "text": text, "page_raw": page_raw}

    def test_clean_document_no_suspect(self):
        a = build_text_layer_audit([
            self._item("The actuator shows a large strain of 0.93% at 3 V.",
                       "The actuator shows a large strain of 0.93% at 3 V.")])
        assert a["severity"] == "none" and a["suspect_total"] == 0
        assert a["suggested_action"] == "none"
        assert a["counts"]["paras"] == 1

    def test_page_bad_glyphs_are_flagged(self):
        """该页文本层含坏字形（cmap 错映射）→ 至少 medium，建议 rescan_ocr。"""
        a = build_text_layer_audit([
            self._item("size <2 nm pores dominate", "size \x03 2 nm pores dominate")])
        assert a["severity"] == "medium"
        assert a["suggested_action"] == "rescan_ocr"
        assert a["counts"]["page_ctrl"] == 1
        assert "字形映射不可信" in a["suspects"][0]["reasons"][0]

    def test_output_control_chars_are_high(self):
        """产物自身含控制字符（乱码进了正文）→ high。"""
        a = build_text_layer_audit([
            self._item("size \x03 2 nm pores", "size \x03 2 nm pores")])
        assert a["severity"] == "high"
        assert any("控制字符" in r for r in a["suspects"][0]["reasons"])

    def test_unverified_tokens_flagged(self):
        """产物里带单位的高信号 token（如 `2 mA cm^-2`）在文本层找不到 → 可疑（两通道同错的抓手）。

        注意 `unverified_tokens` 有页门控（归一化后 ≥ `MIN_PAGE_CHARS` 才判）⇒ 文本层要给足长度。
        """
        filler = "lorem ipsum dolor sit amet consectetur adipiscing elit " * 5
        a = build_text_layer_audit([
            self._item("a current of 2 mA cm-2 was applied here",
                       filler + " but this page text mentions no current density")])
        assert a["counts"]["paras"] == 1
        assert a["counts"]["unverified"] >= 1
        assert any("文本层找不到" in r for r in a["suspects"][0]["reasons"])

    def test_missing_page_text_reported_not_judged(self):
        """页文本层不可用（映射失败/扫描件）→ 计入 coverage，不据此下结论。"""
        a = build_text_layer_audit([self._item("some text", "")])
        assert a["counts"]["paras"] == 0 and a["counts"]["paras_no_pagetext"] == 1
        assert a["severity"] == "none"

    def test_sup_word_fragments_flagged(self):
        a = build_text_layer_audit([
            self._item("E<sup>lectrochemical</sup> <sup>actuators</sup> work",
                       "Electrochemical actuators work")])
        assert any("碎片" in r for r in a["suspects"][0]["reasons"])


class TestQualityAssertions:
    def test_clean_passes(self):
        q = assert_quality(en_md="A $x$ B\n\nC.\n", doc={"paragraphs": [1, 2]},
                           stats={"figures": {"image_markers": 6, "captions": 6, "match": True},
                                  "formula_self_check": []})
        assert q["passed"] is True and q["failed"] == []
        assert q["detail"]["control_chars"] == 0

    def test_control_char_and_dollar_imbalance_fail(self):
        q = assert_quality(en_md="bad \x03 char and $unbalanced\n",
                           doc={"paragraphs": [1]}, stats={})
        assert q["passed"] is False
        assert any("control_chars" in f for f in q["failed"])
        assert any("dollar_balance" in f for f in q["failed"])

    def test_figure_mismatch_and_formula_issues_fail(self):
        q = assert_quality(en_md="ok text.\n", doc={"paragraphs": [1] * 10},
                           stats={"figures": {"image_markers": 5, "captions": 6, "match": False},
                                  "formula_self_check": ["a", "b", "c", "d"]})
        assert q["passed"] is False
        assert any("figure_match" in f for f in q["failed"])
        assert any("formula_self_check" in f for f in q["failed"])

    def test_doc_as_object_and_list_formula_stats(self):
        """p14 里 `doc` 是 ArticleDocument 对象、`formula_self_check` 是**列表** ⇒ 都要能接。"""
        class _Doc:
            paragraphs = [1, 2, 3, 4, 5]

        q = assert_quality(en_md="fine.\n", doc=_Doc(),
                           stats={"figures": 5, "formula_self_check": ["one issue"]})
        assert q["passed"] is True and q["detail"]["paragraphs"] == 5
        assert q["detail"]["formula_issues"] == 1
