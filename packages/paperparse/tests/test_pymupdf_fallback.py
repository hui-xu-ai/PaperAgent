#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: tests/test_pymupdf_fallback.py
功能: T6 PyMuPDF 兜底解析单元测试（行级提取 + 行尾污染剥离）
对外接口: 无（测试）
版本: v1.1.0 (2025-xx-xx)
版本历史:
  v1.1.0 行级重构适配
  v1.0.0 初始版本
"""
import pymupdf
import pytest

from paperparse.core.pymupdf_fallback import (
    extract_blocks,
    page_sizes,
    render_clip,
    strip_line_tail,
)


def _make_pdf(path, pages=2):
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), "Line one of page %d." % (i + 1), fontsize=12)
        page.insert_text((72, 100), "Line two of page %d." % (i + 1), fontsize=12)
    doc.save(str(path))
    doc.close()


def test_extract_blocks_line_level(tmp_work):
    p = tmp_work / "t.pdf"
    _make_pdf(p)
    result = extract_blocks(p)
    assert result.source == "pymupdf"
    assert result.pages == 2
    assert len(result.blocks) == 4          # 每页 2 行
    b = result.blocks[0]
    assert b.bbox[0] < b.bbox[2]
    assert b.font_size and b.font_size > 0
    assert b.text
    assert b.kind == "other"                # 待 block_classify 赋值


def test_render_clip_png(tmp_work):
    p = tmp_work / "t.pdf"
    _make_pdf(p)
    data = render_clip(p, 1, (0, 0, 200, 150), dpi=100)
    assert data[:4] == b"\x89PNG"
    assert len(data) > 100


def test_page_sizes(tmp_work):
    p = tmp_work / "t.pdf"
    _make_pdf(p)
    sizes = page_sizes(p)
    assert sizes[1][0] > 0 and sizes[1][1] > 0
    assert len(sizes) == 2


def test_strip_line_tail():
    assert strip_line_tail(
        "platform with a compact size, 15214095, 2024, 47, Downloaded from https://x"
    ) == "platform with a compact size,"
    assert strip_line_tail("n metal nanoparticles were 15214095, 2024, 47, Downloade") \
        == "n metal nanoparticles were"
    assert strip_line_tail("Adv. Mater. 2024, 36, 2407106© 2024 Wiley-VCH GmbH2407106 (1") \
        == ""    # 期刊页脚整行剥离
    assert strip_line_tail("2407106 (7 of 15)") == ""   # 页码行
    assert strip_line_tail("www.advancedsciencenews.comwww.advmat.de") == ""
    # 行尾网址按"正文不混入网址"规则剥离（用户问题 12）
    assert strip_line_tail("see details at www.nature.com") == "see details at"


def test_real_sample_head(samples_dir):
    """真实样本轻量断言：行级提取统计（D9：不读全文）"""
    pdf = samples_dir / "10.1002_adma.202407106.pdf"
    if not pdf.exists():
        pytest.skip("样本缺失")
    result = extract_blocks(str(pdf))
    assert result.pages == 15
    assert len(result.blocks) > 300          # 行级：15 页双栏应远超 300 行
    assert all(b.block_id.startswith("B") for b in result.blocks[:10])
