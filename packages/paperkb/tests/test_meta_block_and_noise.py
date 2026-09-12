# -*- coding: utf-8 -*-
"""元数据补全 + 上下文噪声清理单测（2026-09-12 用户反馈）。

覆盖三条用户反馈：
1) L1 元数据缺**研究单位/通信作者标注/关键词** → `frontmatter.meta_block` 必须给出这三项
   （papers_meta 优先，缺失时用 document.json metadata + 首页段落本地兜底）；
2) 送给 AI 的原文混入 **References / ACKNOWLEDGMENTS / Conflict of Interest** →
   `context.context_paragraphs` 必须截断尾部杂项（实测 PNAS 篇曾漏 7040 字符 ≈1828 token）；
3) 截断不得伤到正文（结论段必须保留）。
"""
from __future__ import annotations

from paperkb.context import (context_paragraphs, shared_ctx, tail_cut_index)  # noqa: F401
from paperkb.doc import PaperDoc, Para
from paperkb.frontmatter import (guess_affiliations, guess_authors, guess_corresponding,
                                 guess_keywords, meta_block)


def _doc(paras: list[tuple[str, str]], title: str = "T") -> PaperDoc:
    doc = PaperDoc(doi="10.1/x", title=title)
    doc.paragraphs = [Para(para_id=pid, section=sec, text_en=txt)
                      for pid, sec, txt in paras]
    return doc


class TestTailNoiseCut:
    def test_cuts_acknowledgments_and_references(self):
        """后 40% 出现 ACKNOWLEDGMENTS → 其后（含参考文献条目）全部不进上下文"""
        body = [("P%03d" % i, "Results", "Body paragraph number %d with enough words." % i)
                for i in range(1, 9)]
        tail = [("P009", "Results", "ACKNOWLEDGMENTS. We thank the funding agency."),
                ("P010", "Results", "1. A. Author, Title of paper, Journal 2020, 1, 1."),
                ("P011", "Results", "2. B. Author, Another paper, Journal 2021, 2, 2.")]
        doc = _doc(body + tail)
        kept = [p.para_id for p in context_paragraphs(doc)]
        assert "P009" not in kept and "P010" not in kept and "P011" not in kept
        assert kept == ["P%03d" % i for i in range(1, 9)]
        assert tail_cut_index(doc) == 8
        ctx = shared_ctx(doc)
        assert "ACKNOWLEDGMENTS" not in ctx and "Title of paper" not in ctx

    def test_keeps_body_when_no_end_matter(self):
        doc = _doc([("P001", "Intro", "Only body text here.")])
        assert [p.para_id for p in context_paragraphs(doc)] == ["P001"]

    def test_mid_document_supporting_sentence_not_cut(self):
        """前半段的长引用句（"Supporting Information Figure S1 shows …"）不得触发截断"""
        paras = [("P001", "Intro", "Intro paragraph."),
                 ("P002", "Intro",
                  "Supporting Information Figure S1 shows the full set of measurements "
                  "collected during the experiment, which we discuss in detail below.")]
        paras += [("P%03d" % i, "Intro", "Later body paragraph %d." % i) for i in range(3, 12)]
        doc = _doc(paras)
        assert tail_cut_index(doc) == len(paras)

    def test_short_supporting_note_early_is_cut(self):
        """前 50% 的"短声明段"（Wiley 的 Supporting Information 说明）仍要截断"""
        paras = [("P001", "Intro", "a"), ("P002", "Intro", "b"), ("P003", "Intro", "c"),
                 ("P004", "Intro", "d"),
                 ("P005", "Intro", "Supporting Information is available from the Wiley "
                                   "Online Library or from the author."),
                 ("P006", "Intro", "## References"), ("P007", "Intro", "1. Ref entry 2020.")]
        doc = _doc(paras)
        assert tail_cut_index(doc) == 4


class TestFrontmatterExtraction:
    DOC = _doc([("P001", "", "# Title of the paper"),
                ("P002", "", "Gangqiang Tang <sup>a,1</sup>, Mingfei Jiang <sup>a,1</sup>, "
                             "Xin Zhao <sup>a</sup>"),
                ("P003", "", "<sup>a</sup> Jiangsu Provincial Key Laboratory of Special "
                             "Robot Technology, Hohai University, Changzhou 213200, China"),
                ("P004", "", "Keywords: Ionic polymer sensor Ionic liquid Heating time"),
                ("P005", "", "## 1. Introduction"),
                ("P006", "", "Body text.")])

    def test_affiliations(self):
        got = guess_affiliations(self.DOC)
        assert len(got) == 1 and "Hohai University" in got[0]

    def test_keywords_split_without_separators(self):
        assert guess_keywords(self.DOC) == ["Ionic polymer sensor", "Ionic liquid", "Heating time"]

    def test_authors(self):
        assert guess_authors(self.DOC) == ["Gangqiang Tang", "Mingfei Jiang", "Xin Zhao"]

    def test_corresponding_marks(self):
        doc = _doc([("P001", "", "# T"),
                    ("P002", "", "Zhenjin Xu, Keqi Deng, Jianyi Zheng, \\* and Dezhi Wu\\*"),
                    ("P003", "", "Body.")])
        assert guess_corresponding(doc, []) == ["Dezhi Wu"]
        # 公式段（含 $ { } \）不得被当作者行
        doc2 = _doc([("P001", "", "$\\eta^{*}$ is the coefficient."),
                     ("P002", "", "Body text.")])
        assert guess_corresponding(doc2, []) == []


class TestMetaBlock:
    def test_block_has_affiliations_keywords_corresponding(self):
        meta = {"title": "T", "authors": ["A One", "B Two"],
                "affiliations": ["Dept of X, University of Y"],
                "keywords": ["k1", "k2"], "corresponding": ["B Two"],
                "journal": "J", "year": "2024", "abstract": "abs"}
        out = meta_block(meta, None)
        assert "作者：A One, B Two*（* 为通信作者）" in out
        assert "通信作者：B Two" in out
        assert "研究单位：Dept of X, University of Y" in out
        assert "关键词：k1, k2" in out
        assert "期刊：J 2024" in out

    def test_block_falls_back_to_doc(self):
        out = meta_block(None, TestFrontmatterExtraction.DOC, abstract_chars=10)
        assert "研究单位：" in out and "关键词：Ionic polymer sensor" in out
        assert "作者：Gangqiang Tang" in out
