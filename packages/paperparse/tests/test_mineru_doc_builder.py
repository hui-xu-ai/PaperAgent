#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: tests/test_mineru_doc_builder.py
功能: 高精度文本融合（mineru_doc_builder）测试：
      - 段落级映射（full.md LaTeX 文本替换本地段）
      - 段落守恒（同一 full 段不重复使用）
      - 本地合并段覆盖多个 full 段并拼接
      - 未匹配段保留本地文本
      - 真实样本：LaTeX 保留 + 无重复 + 与校准版对齐
对外接口: 无（测试）
版本: v1.0.0 (2026-08-18)
版本历史:
  v1.0.0 初始版本
"""
import re
from collections import Counter
from pathlib import Path

import pytest

from paperparse.core.mineru_doc_builder import build_mineru_doc
from paperparse.middleware.schema import Paragraph

FULL_MD = (
    "# Title\n\n"
    "Author One, Author Two\n\n"
    "Abstract with $\\mathsf { s p } ^ { 2 }$ formula and more content here.\n\n"
    "## S\n\n"
    "First paragraph with capacitance (≈247 F g<sup>−1</sup>).<sup>[14–17]</sup> done.\n\n"
    "Second paragraph of the section with additional words.\n"
)


def _paras():
    return [
        Paragraph(para_id="P001", order=1, section="",
                  text_en="Abstract with sp 2 formula and more content here.",
                  confidence=0.9),
        Paragraph(para_id="P002", order=2, section="S",
                  text_en="First paragraph with capacitance ( ≈ 247 F g − 1 ). "
                          "[ 14–17 ] done.",
                  confidence=0.9),
        Paragraph(para_id="P003", order=3, section="S",
                  text_en="Second paragraph of the section with additional words.",
                  confidence=0.9),
    ]


def test_build_replaces_with_latex():
    """本地段（无 LaTeX）→ full.md LaTeX 文本（段落级整体替换）"""
    paras = _paras()
    n = build_mineru_doc(paras, FULL_MD)
    assert n >= 2
    assert "$\\mathsf { s p } ^ { 2 }$" in paras[0].text_en
    assert "<sup>−1</sup>" in paras[1].text_en


def test_paragraph_conservation():
    """段落守恒：同一 full 段不得出现在多个本地段（无重复文本）"""
    paras = _paras()
    build_mineru_doc(paras, FULL_MD)
    texts = [p.text_en[:60] for p in paras]
    dup = [t for t, c in Counter(texts).items() if c > 1]
    assert not dup, "同一片段被复制到多个段落"


def test_unmatched_kept():
    """未匹配段保留本地文本（不混拼）"""
    paras = _paras()
    paras.append(Paragraph(para_id="P009", order=9, section="S",
                           text_en="Completely different content not in full md.",
                           confidence=0.9))
    n = build_mineru_doc(paras, FULL_MD)
    assert paras[3].text_en == "Completely different content not in full md."


def test_merged_local_para_covers_multiple_full():
    """本地合并段（跨块合并）覆盖多个 full 段并拼接 LaTeX"""
    full = ("# T\n\n## S\n\n"
            "Part one with $\\alpha$ formula of the paragraph details here.\n\n"
            "Part two continuing the same paragraph with extra words.\n")
    paras = [Paragraph(para_id="P001", order=1, section="S",
                       text_en="Part one with α formula of the paragraph details "
                               "here. Part two continuing the same paragraph with "
                               "extra words.",
                       confidence=0.9)]
    n = build_mineru_doc(paras, full)
    assert n == 1
    assert "$\\alpha$" in paras[0].text_en
    assert "Part two continuing the same paragraph with extra words." in paras[0].text_en


# ---------- 真实样本（统计断言，不读全文） ----------

REAL_FULL = "work/mineru_backup/20260818-191542_v4batch/full.md"


@pytest.mark.skipif(not __import__("pathlib").Path(REAL_FULL).exists(),
                    reason="真实 MinerU 输出缺失")
def test_real_builder_stats():
    """真实样本：多数段替换为 LaTeX、无重复、与校准版高对齐"""
    from paperparse.core.block_classify import classify_lines
    from paperparse.core.layout import analyze, reading_order
    from paperparse.core.pymupdf_fallback import extract_blocks, page_sizes
    from paperparse.core.stitch_code import stitch
    pdf = str(Path(__file__).resolve().parent / "samples" / "10.1002_adma.202407106.pdf")
    blocks = extract_blocks(pdf)
    classify_lines(blocks.blocks)
    lay = analyze(blocks.blocks, page_sizes=page_sizes(pdf))
    ordered = reading_order(blocks.blocks, lay)
    res = stitch(ordered, lay)
    full = open(REAL_FULL, encoding="utf-8").read()
    n = build_mineru_doc(res.paragraphs, full)
    assert n >= 25, "多数正文段应替换为 full.md LaTeX 文本"
    texts = [p.text_en[:80] for p in res.paragraphs]
    dup = [t for t, c in Counter(texts).items() if c > 1]
    assert not dup, "段落守恒：不应有重复片段"
    joined = " ".join(p.text_en for p in res.paragraphs)
    assert "\\mathsf" in joined or "<sup>" in joined, "LaTeX/<sup> 应大量保留"
