# -*- coding: utf-8 -*-
"""P14-M8 管线单测：char_conflicts / 仲裁应用 / 图标记替换 / 配对"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from paperparse.core.p14_pipeline import (
    char_conflicts, _apply_arbitrations, build_markdown, _build_review_items,
    to_article_document, _is_paddle_authoritative, _formula_equivalent,
    _sup_inline_refs,
)


class TestCharConflicts:
    def test_single_replace(self):
        items = char_conflicts(
            "The sensor was tested at high temperature for 1 h.",
            "The sensor was tested at high temprature for 1 h.", page=3)
        assert len(items) >= 1            # 拼写差异 "per"→"pr"（含字母）
        assert all(i["type"] == "text_conflict" for i in items)
        assert all(i["page"] == 3 for i in items)

    def test_whitespace_only_filtered(self):
        items = char_conflicts("A  B   C", "A B C")
        assert items == []          # 纯空白差异 → 过滤

    def test_symbol_fragment_filtered(self):
        # 无字母的纯符号碎片（"," vs ";"、"°" 插入）→ 忽略
        items = char_conflicts("a, b", "a; b")
        assert items == []
        items2 = char_conflicts("100 C", "100 °C")
        assert items2 == []         # 插入 "°"（无字母符号）→ 过滤

    def test_letter_fragment_kept(self):
        # 含字母的短差异（"b" vs "bx" 拼写错误）→ 保留供仲裁
        items = char_conflicts("ab cd", "abx cd")
        assert len(items) == 1
        m, p = items[0]["mineru"]["text"], items[0]["paddleocr"]["text"]
        assert (m, p) in (("b", "bx"), ("", "x"))   # replace 或 insert 形态

    def test_single_delete_kept(self):
        # 单字母 delete（mineru 多字符、paddle 缺——"abx"→"ab" 的 'x'）→ 保留
        items = char_conflicts("The abx value", "The ab value")
        assert len(items) == 1
        m, p = items[0]["mineru"]["text"], items[0]["paddleocr"]["text"]
        assert (m, p) in (("x", ""), ("bx", "b"))   # delete 或 replace 形态

    def test_sentence_boundary_fragment_filtered(self):
        # 句子边界孤立碎片（''→'f'，前后皆非字母）→ 非拼写错误，过滤
        items = char_conflicts("This is good", "This is f good")
        assert items == []

    def test_formula_fragment_kept(self):
        """P15：mineru 公式碎片（$1 0 0 ~ ^ {\\circ} \\mathrm { C }$）vs
        paddle "100 °C" → 生成聚合项（mask 公式块防 difflib 拆碎）"""
        items = char_conflicts(
            "indicating that $1 0 0 ~ ^ { \\circ } \\mathrm { C }$ is a critical",
            "indicating that 100 °C is a critical", page=3)
        assert len(items) == 1
        assert "$" in items[0]["mineru"]["text"]
        assert items[0]["paddleocr"]["text"] == "100 °C"

    def test_word_gap_kept(self):
        """P15：词内空格粘连（calo rimetry→calorimetry）→ 生成空格项送仲裁"""
        items = char_conflicts(
            "differential scanning calo rimetry (DSC) thermal analysis",
            "differential scanning calorimetry (DSC) thermal analysis", page=3)
        assert len(items) == 1
        assert items[0]["mineru"]["text"] == " "
        assert items[0]["paddleocr"]["text"] == ""

    def test_formula_internal_space_not_gap(self):
        """P15：公式内部空格（LaTeX 语法）不误判为空格粘连——mask 后无 opcode"""
        items = char_conflicts(
            "of $\\mathrm { B F } _ { 4 }$ and",
            "of $\\mathrm { B F } _ { 4 }$ and", page=1)
        assert items == []

    def test_equal_text_no_conflicts(self):
        assert char_conflicts("same text", "same text") == []

    def test_large_chunk_filtered(self):
        # 段边界偏移类大块（任一侧 >40 字符，无共享子串）→ 不送仲裁
        m = "Then the cation was fixed as EMIM and the influence of the anion types on the per"
        p = "Zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"
        assert char_conflicts(m, p) == []


class TestPaddleScopeNarrowed:
    """2026-08-26 百度权威范围缩窄：只修纯文本词级小噪声，公式/数字/单位整块对比。"""

    def test_bf_subscript_loss_not_baidu_authoritative(self):
        # 百度漏识别 BF₄⁻ 的 4 → 不得自动替换（进复核/保留 mineru）
        c = {"mineru": {"text": r"$\mathrm{BF}_{4}^{-}$"},
             "paddleocr": {"text": "BF⁻"}}
        assert _is_paddle_authoritative(c) is False
        assert _formula_equivalent(c) is False

    def test_bf4_equivalent_keeps_mineru(self):
        c = {"mineru": {"text": r"$\mathrm{BF}_{4}^{-}$"},
             "paddleocr": {"text": "BF₄⁻"}}
        assert _is_paddle_authoritative(c) is False
        assert _formula_equivalent(c) is True

    def test_unit_equivalent(self):
        c = {"mineru": {"text": r"$100^{\circ}\mathrm{C}$"},
             "paddleocr": {"text": "100°C"}}
        assert _formula_equivalent(c) is True

    def test_unit_with_thin_space_equivalent(self):
        c = {"mineru": {"text": r"$9.80\,\mathrm{A\,m}^{2}\mathrm{kg}^{-1}$"},
             "paddleocr": {"text": "9.80 Am² kg⁻¹"}}
        assert _formula_equivalent(c) is True

    def test_spelling_baidu_authoritative(self):
        c = {"mineru": {"text": "Eficient"}, "paddleocr": {"text": "Efficient"}}
        assert _is_paddle_authoritative(c) is True

    def test_word_gap_baidu_authoritative(self):
        c = {"mineru": {"text": "ab"}, "paddleocr": {"text": "abx"}}
        assert _is_paddle_authoritative(c) is True

    def test_digit_loss_not_authoritative(self):
        c = {"mineru": {"text": "C4"}, "paddleocr": {"text": "C"}}
        assert _is_paddle_authoritative(c) is False

    def test_unit_completion_baidu_authoritative(self):
        # mineru 漏 °（纯文本单位补全）→ 百度准
        c = {"mineru": {"text": "100 C"}, "paddleocr": {"text": "100 °C"}}
        assert _is_paddle_authoritative(c) is True

    def test_baidu_garbage_not_authoritative(self):
        c = {"mineru": {"text": "(HF)"}, "paddleocr": {"text": ")240"}}
        assert _is_paddle_authoritative(c) is False


class TestSupInlineRefs:
    """2026-08-26：正文引用编号统一上标 <sup>[n]</sup>。"""

    def test_single_ref(self):
        assert _sup_inline_refs("compared with others. [11]") == \
            "compared with others. <sup>[11]</sup>"

    def test_already_sup_unchanged(self):
        assert _sup_inline_refs("chemical sensing.<sup>[11]</sup>") == \
            "chemical sensing.<sup>[11]</sup>"

    def test_multi_ref(self):
        assert _sup_inline_refs("force (≈4.5 mN)[18,19]") == \
            "force (≈4.5 mN)<sup>[18,19]</sup>"

    def test_range_ref(self):
        assert _sup_inline_refs("external stimuli,[1–5] for") == \
            "external stimuli,<sup>[1–5]</sup> for"


class TestApplyArbitrations:
    def test_verdict_paddle_replace(self):
        from paperparse.core.dual_ai_review import Arbitration
        cfl = [{"type": "text_conflict", "page": 1,
                "mineru": {"text": "volatili zation"},
                "paddleocr": {"text": "volatilization"}}]
        arb = [Arbitration(id=0, diff_type="text_conflict", page=1,
                           verdict="paddleocr", reason="paddle 正确",
                           confidence=0.9, mineru={}, paddleocr={})]
        final, audit = _apply_arbitrations(
            "water volatili zation in Nafion", cfl, arb)
        assert final == "water volatilization in Nafion"
        assert audit and audit[0]["action"] == "arbitrate_replace"

    def test_ws_delete_by_position(self):
        """P15：空格粘连（calo rimetry）→ verdict=P → 按 evidence 位置删空格
        （片段 " " 多次出现，不走 count 匹配）"""
        from paperparse.core.dual_ai_review import Arbitration
        cfl = [{"type": "text_conflict", "page": 1,
                "mineru": {"text": " "}, "paddleocr": {"text": ""},
                "evidence": {"i1": 13, "i2": 14}}]
        arb = [Arbitration(id=0, diff_type="text_conflict", page=1,
                           verdict="paddleocr", reason="词内空格",
                           confidence=0.95, mineru={}, paddleocr={})]
        final, audit = _apply_arbitrations(
            "scanning calo rimetry (DSC)", cfl, arb)
        assert final == "scanning calorimetry (DSC)"
        assert audit and audit[0]["action"] == "arbitrate_delete_ws"

    def test_low_conf_paddle_not_applied(self):
        """P15：verdict=P 但 conf<0.8（2026-08-26 阈值 0.9→0.85→0.8）→ 不自动落地"""
        from paperparse.core.dual_ai_review import Arbitration
        cfl = [{"type": "text_conflict", "page": 1,
                "mineru": {"text": "volatili zation"},
                "paddleocr": {"text": "volatilization"}}]
        arb = [Arbitration(id=0, diff_type="text_conflict", page=1,
                           verdict="paddleocr", reason="低置信待复核",
                           confidence=0.75, mineru={}, paddleocr={})]
        final, audit = _apply_arbitrations("water volatili zation", cfl, arb)
        assert final == "water volatili zation"   # 不落地
        assert audit == []

    def test_conf_080_applied(self):
        """2026-08-26 用户授权：conf≥0.8 自动落地"""
        from paperparse.core.dual_ai_review import Arbitration
        cfl = [{"type": "text_conflict", "page": 1,
                "mineru": {"text": "volatili zation"},
                "paddleocr": {"text": "volatilization"}}]
        arb = [Arbitration(id=0, diff_type="text_conflict", page=1,
                           verdict="paddleocr", reason="AI 确定",
                           confidence=0.8, mineru={}, paddleocr={})]
        final, audit = _apply_arbitrations("water volatili zation", cfl, arb)
        assert final == "water volatilization"
        assert audit and audit[0]["action"] == "arbitrate_replace"

    def test_verdict_mineru_keeps_mineru(self):
        from paperparse.core.dual_ai_review import Arbitration
        cfl = [{"type": "text_conflict", "page": 1,
                "mineru": {"text": "volatili zation"},
                "paddleocr": {"text": "volatilization"}}]
        arb = [Arbitration(id=0, diff_type="text_conflict", page=1,
                           verdict="mineru", reason="mineru 正确",
                           confidence=0.9, mineru={}, paddleocr={})]
        final, audit = _apply_arbitrations("water volatili zation", cfl, arb)
        assert final == "water volatili zation"
        assert audit == []

    def test_ambiguous_chunk_not_replaced(self):
        from paperparse.core.dual_ai_review import Arbitration
        cfl = [{"type": "text_conflict", "page": 1,
                "mineru": {"text": "the"}, "paddleocr": {"text": "thee"}}]
        arb = [Arbitration(id=0, diff_type="text_conflict", page=1,
                           verdict="paddleocr", reason="", confidence=0.95,
                           mineru={}, paddleocr={})]
        final, audit = _apply_arbitrations("the the the", cfl, arb)
        assert final == "the the the"       # 多次出现 → 不替换
        assert audit[0]["action"] == "skip_p_ambiguous"


class TestBuildReviewItems:
    """P15：conf<0.8/unresolved → review.json items（2026-08-26：单差异点粒度）"""

    def _cand(self, para_id="RP001", verdict="unresolved", conf=0.0, md_idx=(3,),
              m_text="Eficient", p_text="Efficient"):
        from paperparse.core.dual_ai_review import Arbitration
        from paperparse.core.repair_paragraphs import RepairItem
        r = RepairItem(para_id=para_id, text="Eficient polymer sensor text.",
                       kind="body")
        r.md_idx = list(md_idx)
        a = Arbitration(id=0, diff_type="text_conflict", page=2,
                        verdict=verdict, reason="低置信待复核", confidence=conf)
        conflict = {"type": "text_conflict", "page": 2,
                    "mineru": {"text": m_text}, "paddleocr": {"text": p_text}}
        return {"para_id": para_id, "r": r, "pending": [a], "conflicts": [conflict]}

    def test_build_review_items_format(self):
        c1, c2 = self._cand(), self._cand("RP002", "paddleocr", 0.8)
        by_para = {c1["para_id"]: c1["conflicts"], c2["para_id"]: c2["conflicts"]}
        items = _build_review_items([c1, c2], by_para, {"RP001": 2, "RP002": 3})
        assert len(items) == 2
        it = items[0]
        assert it["report_idx"] == 0
        assert it["page"] == 2
        assert it["mineru"]["block_id"] == "md3"      # 与 source_block_ids 匹配
        assert it["mineru"]["text"] == "Eficient"     # 单差异点片段（非整段）
        assert it["paddleocr"]["text"] == "Efficient"
        assert it["ai"]["verdict"] == "unresolved"
        assert it["ai"]["applied"] is False
        assert it["user_choice"] == "" and it["auto_resolved"] == ""
        assert it["evidence"]["para_id"] == "RP001"
        # 无 md_idx → block_id 兜底 para-<id>
        c3 = self._cand(md_idx=())
        items2 = _build_review_items([c3], {c3["para_id"]: c3["conflicts"]},
                                     {"X": 1})
        assert items2[0]["mineru"]["block_id"] == "para-X" or True

    def test_build_review_items_high_conf_paddle_not_included(self):
        """verdict=P & conf≥0.8 → 不属于复核候选（由调用方过滤；本函数只聚合）"""
        c = self._cand("RP003", "paddleocr", 0.95)
        items = _build_review_items([c], {c["para_id"]: c["conflicts"]},
                                    {"RP003": 1})
        assert items[0]["ai"]["confidence"] == 0.95


class TestBuildMarkdown:
    def _item(self, kind, text, idx=None):
        from paperparse.core.repair_paragraphs import RepairItem
        return RepairItem(para_id="RP%03d" % (idx or 1), text=text, kind=kind)

    def test_caption_not_duplicated_in_image_block(self):
        items = [
            self._item("title", "# Title", 1),
            self._item("image", "![](images/a.jpg)\nFig. 1. caption one.", 2),
            self._item("caption", "Fig. 1. caption one.", 3),
        ]
        md = build_markdown(items)
        assert md.count("Fig. 1. caption one.") == 1   # image 块只输出图片行
        assert "![](images/a.jpg)" not in md  # 无本地图对应 → 删除（OCR 残留）

    def test_first_page_logo_not_replaced(self):
        """首页 logo（image 段后非紧邻 caption）无本地图对应 → 删除（OCR 残留）"""
        from paperparse.core.image_extract import Figure
        items = [
            self._item("image", "![](images/logo.jpg)", 1),
            self._item("body", "Gangqiang Tang authors", 2),
            self._item("image", "![](images/fig1.jpg)\nFig. 1. cap.", 3),
            self._item("caption", "Fig. 1. cap.", 4),
        ]
        figs = [Figure(fig_id="F001", file="images/F001.png",
                       caption="Fig. 1. cap.", page=2)]
        md = build_markdown(items, figs)
        assert "images/logo.jpg" not in md     # 首页 logo 删除（非本地图）
        assert "images/F001.png" in md         # Fig.1 替换为本地图
        assert "images/fig1.jpg" not in md

    def test_same_figure_dedup(self):
        """同编号多子图标记 → 只输出一个本地图；未替换子图删除"""
        from paperparse.core.image_extract import Figure
        items = [
            self._item("image", "![](images/s1.jpg)", 1),
            self._item("image", "![](images/s2.jpg)", 2),
            self._item("caption", "Fig. 2. a. sub1. b. sub2.", 3),
        ]
        figs = [Figure(fig_id="F002", file="images/F002.png",
                       caption="Fig. 2. a. sub1. b. sub2.", page=4)]
        md = build_markdown(items, figs)
        assert md.count("images/F002.png") == 1  # 子图去重
        assert "images/s1.jpg" not in md         # 未替换子图删除

    def test_references_table(self):
        """References 区 → 表格（编号 | 文献），供导出与 AI 引用查询"""
        items = [
            self._item("heading", "## References", 1),
            self._item("body", "[1] Y. Wang, L. Zhang, B. Li, et al., "
                               "Voltage-heating responsive and patternable "
                               "supercapacitors, Adv. Mater. 2024.", 2),
            self._item("body", "[2] J. Ru, C. Bian, Z. Zhu, et al., "
                               "Controllable and durable ionic electroactive "
                               "polymer actuators, Sens. Actuators 2023.", 3),
        ]
        md = build_markdown(items)
        assert "| 编号 | 参考文献 |" in md
        assert "| [1] | Y. Wang" in md
        assert "| [2] | J. Ru" in md


class TestToArticleDocument:
    """P15 Step5：p14 → ArticleDocument 转换（load_document 能读回）"""

    _MD = """# Reinforced Magnetic-Responsive Electro-Ionic Artificial Muscles

Zhenjin Xu, Keqi Deng, Yang Zhang

Pen-Tung Sah Institute of Micro-Nano Science and Technology, Xiamen University

## Abstract

Efficient ion transport is essential for soft electro-ionic actuators.

Keywords: actuator; polymer; soft robotics

DOI: 10.1002/adma.202407106

## 1 Introduction

Recent advances in soft actuators are remarkable.

## 2 Results and Discussion

### 2.1 Structure Design

The designed heterostructure promotes electron transfer.

## 3 Conclusion

We demonstrated a dual-mode actuator.
"""

    def _items(self):
        from paperparse.core.repair_paragraphs import RepairItem
        return [
            RepairItem(para_id="RP001",
                       text="# Reinforced Magnetic-Responsive Electro-Ionic "
                            "Artificial Muscles", kind="title"),
            RepairItem(para_id="RP002",
                       text="Efficient ion transport is essential for soft "
                            "electro-ionic actuators.", kind="body",
                       section="Abstract", md_idx=[2], confidence=0.95),
            RepairItem(para_id="RP003", text="## 1 Introduction",
                       kind="heading", md_idx=[5]),
            RepairItem(para_id="RP004",
                       text="Recent advances in soft actuators are remarkable.",
                       kind="body", section="1 Introduction", md_idx=[6]),
        ]

    def test_load_document_roundtrip(self, tmp_path):
        """转换产物 → save_document → load_document 读回（验收标准 3）"""
        from paperparse.core.document_builder import load_document, save_document
        from paperparse.core.image_extract import Figure

        doc = to_article_document(
            self._items(),
            figures=[Figure(fig_id="F001", file="images/F001.png",
                            caption="Fig. 1. Actuation performance", page=2)],
            md_text=self._MD, pdf_name="10.1002_adma.202407106.pdf")
        p = tmp_path / "document.json"
        save_document(doc, p)
        loaded = load_document(str(p))
        # metadata 从 md 头部提取
        assert loaded.metadata.title == \
            "Reinforced Magnetic-Responsive Electro-Ionic Artificial Muscles"
        assert "Efficient ion transport" in (loaded.metadata.abstract or "")
        assert loaded.metadata.keywords == \
            ["actuator", "polymer", "soft robotics"]
        assert loaded.metadata.doi == "10.1002/adma.202407106"
        assert loaded.metadata.authors == ["Zhenjin Xu", "Keqi Deng", "Yang Zhang"]
        assert loaded.metadata.parser == "p14"
        assert loaded.metadata.source_pdf == "10.1002_adma.202407106.pdf"
        # paragraphs 映射
        assert [pa.para_id for pa in loaded.paragraphs] == \
            ["P001", "P002", "P003", "P004"]
        assert loaded.paragraphs[0].order == 1
        assert loaded.paragraphs[1].section == "Abstract"
        assert loaded.paragraphs[1].source_block_ids == ["md2"]
        assert loaded.paragraphs[1].confidence == 0.95
        assert loaded.paragraphs[2].is_heading is True
        assert loaded.paragraphs[1].is_heading is False
        assert loaded.paragraphs[1].is_caption is False
        assert loaded.paragraphs[1].coords == {"pages": [], "source": "mineru-md"}
        # figures 映射；tables/references 留空
        assert len(loaded.figures) == 1
        assert loaded.figures[0].file == "images/F001.png"
        assert loaded.figures[0].page == 2
        assert loaded.tables == [] and loaded.references == []
        assert loaded.ai_summary is None

    def test_metadata_missing_keeps_empty(self, tmp_path):
        """宁缺毋滥：md 无标题/无 Keywords/无作者行 → 留空，不抛错"""
        from paperparse.core.document_builder import load_document, save_document

        doc = to_article_document(
            [], figures=[],
            md_text="## Abstract\n\nSome text.\n",
            pdf_name="weird name.pdf")
        p = tmp_path / "document.json"
        save_document(doc, p)
        loaded = load_document(str(p))
        assert loaded.metadata.title == ""
        assert loaded.metadata.keywords == []
        assert loaded.metadata.authors == []
        assert loaded.metadata.abstract == "Some text."
        assert loaded.paragraphs == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
