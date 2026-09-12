#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: tests/test_latex_normalize.py
功能: LaTeX 规范化/卫生（方案确认）测试：
      - strip_control_chars 清控制字（\\x08/\\x07/\\x0b 等），保 \\n/\\t/\\r
      - normalize_document 对 text_en/text_zh 统一清扫 + KNOWN_FIXES（\\bar{2}→^2）
对外接口: 无（测试）
版本: v1.0.0 (2026-08-19)
版本历史:
  v1.0.0 初始版本
"""
from paperparse.core.latex_normalize import normalize_document, strip_control_chars
from paperparse.middleware.schema import ArticleDocument, ArticleMetadata, Paragraph


def test_strip_control_chars_keeps_newline_tab():
    s = "a\x08b\x07c\x0bd\ne\rf\th"
    out = strip_control_chars(s)
    assert "\x08" not in out and "\x07" not in out and "\x0b" not in out
    assert "abcd\ne\rf\th" == out      # \n \r \t 保留


def test_normalize_document_cleans_en_and_zh():
    doc = ArticleDocument(
        metadata=ArticleMetadata(extraction_time="x", abstract=""),
        paragraphs=[
            Paragraph(para_id="P001", order=1, section="S",
                      text_en="The nanoscale \x08 material achieved $A ^ { 2 }$.",
                      text_zh="该 \x08 材料实现 $A ^ { 2 }$。", confidence=0.9),
        ])
    n = normalize_document(doc)
    assert n == 1
    assert "\x08" not in doc.paragraphs[0].text_en
    assert "\x08" not in doc.paragraphs[0].text_zh


def test_normalize_document_fixes_bar2_in_zh():
    """KNOWN_FIXES 同步修 text_zh 的 \\bar{2}→^2（语义错，含控制字污染形态）"""
    doc = ArticleDocument(
        metadata=ArticleMetadata(extraction_time="x"),
        paragraphs=[Paragraph(para_id="P1", order=1, section="S",
                              text_en="1.01 $\\mathsf { A } \\mathsf { m } ^ { 2 }$.",
                              confidence=0.9)])
    doc.paragraphs[0].text_zh = ("剩磁 1.01 $\\mathsf { A } \\mathsf { m } ^ { \x08ar { 2 } } "
                                 "\\mathrm { k g } ^ { - 1 }$。")
    n = normalize_document(doc)
    assert n == 1
    z = doc.paragraphs[0].text_zh
    assert "\x08" not in z
    assert "^ { 2 }" in z
    assert "ar" not in z


def test_normalize_document_returns_zero_when_clean():
    doc = ArticleDocument(
        metadata=ArticleMetadata(extraction_time="x"),
        paragraphs=[Paragraph(para_id="P1", order=1, section="S",
                              text_en="clean text $x$", confidence=0.9)])
    assert normalize_document(doc) == 0
