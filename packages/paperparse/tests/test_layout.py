#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: tests/test_layout.py
功能: T7 版面分析单元测试（合成布局，无文本依赖）
对外接口: 无（测试）
版本: v1.0.0 (2025-xx-xx)
版本历史:
  v1.0.0 初始版本
"""
from paperparse.core.layout import analyze, reading_order
from paperparse.middleware.schema import TextBlock

PAGE_W, PAGE_H = 400.0, 600.0


def _block(bid, page, x0, y0, x1, y1, kind="body", text="t"):
    return TextBlock(block_id=bid, page=page, bbox=(x0, y0, x1, y1),
                     text=text, kind=kind)


def _two_column_blocks():
    """合成双栏：左栏 x≈0-190，右栏 x≈210-400"""
    blocks = [
        _block("B1", 1, 20, 100, 180, 130, text="left first"),
        _block("B2", 1, 20, 140, 180, 170, text="left second"),
        _block("B3", 1, 220, 100, 380, 130, text="right first"),
        _block("B4", 1, 220, 140, 380, 170, text="right second"),
        _block("B5", 1, 20, 180, 180, 210, text="left third"),
        _block("B6", 1, 220, 180, 380, 210, text="right third"),
    ]
    return blocks


def test_two_column_detection():
    layout = analyze(_two_column_blocks(), page_sizes={1: [PAGE_W, PAGE_H]})
    assert layout.is_two_column is True
    assert layout.columns_per_page[1] == 2
    assert layout.column_thresholds[1] == 200.0


def test_reading_order_two_column():
    layout = analyze(_two_column_blocks(), page_sizes={1: [PAGE_W, PAGE_H]})
    ordered = reading_order(_two_column_blocks(), layout)
    texts = [b.text for b in ordered]
    assert texts == ["left first", "left second", "left third",
                     "right first", "right second", "right third"]


def test_single_column():
    blocks = [
        _block("B1", 1, 20, 100, 380, 130),
        _block("B2", 1, 20, 140, 380, 170),
        _block("B3", 1, 20, 180, 380, 210),
    ]
    layout = analyze(blocks, page_sizes={1: [PAGE_W, PAGE_H]})
    assert layout.is_two_column is False
    assert layout.columns_per_page[1] == 1


def test_noise_filter():
    """页眉/页码过滤（第 2 页页眉；首页顶部豁免——可能是标题）"""
    blocks = _two_column_blocks() + [
        _block("H2", 2, 20, 10, 180, 30, text="Journal Name"),   # 第 2 页页眉
        _block("P1", 1, 190, 580, 210, 595, text="5"),           # 页码
        _block("T1", 1, 20, 10, 180, 30, text="Paper Title"),    # 首页顶部 → 豁免
    ]
    layout = analyze(blocks, page_sizes={1: [PAGE_W, PAGE_H], 2: [PAGE_W, PAGE_H]})
    assert "H2" in layout.noise_block_ids
    assert "P1" in layout.noise_block_ids
    assert "T1" not in layout.noise_block_ids
    ordered = reading_order(blocks, layout)
    ids = {b.block_id for b in ordered}
    assert "H2" not in ids and "P1" not in ids
    assert "T1" in ids


def test_reference_zone():
    blocks = _two_column_blocks() + [
        _block("R1", 3, 20, 100, 180, 130, kind="heading", text="References"),
    ]
    layout = analyze(blocks, page_sizes={1: [PAGE_W, PAGE_H], 3: [PAGE_W, PAGE_H]})
    assert layout.reference_zone_pages == [3]


def test_reference_zone_by_marker():
    """无 References 标题时，按 [n] 标记识别参考文献区"""
    blocks = [
        _block("M1", 2, 20, 100, 180, 130, text="[1] A. Author, J. Mag. 2020, 5, 1."),
        _block("M2", 2, 20, 140, 180, 170, text="[2] B. Writer, Adv. Mater. 2021, 33, 2."),
        _block("M3", 2, 220, 100, 380, 130, text="[3] C. Editor, Nature 2022, 10, 3."),
    ]
    layout = analyze(blocks, page_sizes={2: [PAGE_W, PAGE_H]})
    assert layout.reference_zone_pages == [2]
