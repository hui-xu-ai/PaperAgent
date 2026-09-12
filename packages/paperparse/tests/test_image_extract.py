#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: tests/test_image_extract.py
功能: T10 图片提取（大图保留/去重/小图过滤/图注关联）单元测试
对外接口: 无（测试）
版本: v1.0.0 (2025-xx-xx)
版本历史:
  v1.0.0 初始版本
"""
import pymupdf

from paperparse.core.image_extract import extract_figures
from paperparse.core.pymupdf_fallback import extract_blocks


def _make_fig_pdf(path):
    doc = pymupdf.open()
    page = doc.new_page()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 100, 100))
    pix.clear_with(150)
    # 同一张大图插入两次（应去重为 1 张）
    page.insert_image(pymupdf.Rect(60, 100, 260, 300), pixmap=pix)
    page.insert_image(pymupdf.Rect(60, 350, 260, 550), pixmap=pix)
    # 小图（装饰，应过滤）
    small = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 20, 20))
    small.clear_with(90)
    page.insert_image(pymupdf.Rect(60, 600, 80, 620), pixmap=small)
    # 图注（在图上方的测试布局里放于图下方）
    page.insert_text((72, 580), "Fig. 1. Test caption for the figure.", fontsize=10)
    doc.save(str(path))
    doc.close()


def test_extract_figures_dedup_and_caption(tmp_work):
    pdf = tmp_work / "figs.pdf"
    _make_fig_pdf(pdf)
    from paperparse.core.block_classify import classify_lines
    blocks = extract_blocks(str(pdf)).blocks
    classify_lines(blocks)                      # 管线顺序：S1 提取 → S2 分类 → S4 图注关联
    figs = extract_figures(str(pdf), blocks, tmp_work / "images", dpi=72)
    assert len(figs) == 1
    fig = figs[0]
    assert fig.fig_id == "F001"
    assert fig.file == "images/F001.png"
    assert (tmp_work / "images" / "F001.png").exists()
    assert fig.caption == "Fig. 1. Test caption for the figure."
    assert fig.page == 1
    assert len(fig.sha256) == 64
