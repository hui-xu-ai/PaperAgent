#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: tests/test_block_classify.py
功能: 块特征分类单元测试（作者行/缩写作者行/邮箱/机构/编号标题/图注/污染校验）
对外接口: 无（测试）
版本: v1.0.0 (2025-xx-xx)
版本历史:
  v1.0.0 初始版本
"""
import pytest

from paperparse.core.block_classify import (
    classify_lines,
    is_author_line,
    is_contaminated,
    strip_contamination,
)
from paperparse.middleware.schema import TextBlock


def _row(text, font=10.0, page=2, bold=False):
    return TextBlock(block_id="B1", page=page, bbox=(0, 0, 100, 20),
                     text=text, font_size=font, bold=bold)


def test_classify_caption_continuation_by_font():
    """图注续行：小号字（fs 8 < 正文 9）即使大写开头也归 caption；正文行（fs 9）不误判"""
    rows = [
        _row("Fig. 4. Schematic illustration of energy band structures", font=8),
        _row("LIG composites. (g) EIS of the actuators containing", font=8),
        _row("to 3.27 S cm−1 (P-LIG) results from the additional free", font=9),
        _row("Body line one continues the paragraph text here.", font=9),
        _row("Body line two continues the paragraph text here.", font=9),
    ]
    classify_lines(rows)
    assert rows[0].kind == "caption"
    assert rows[1].kind == "caption"
    assert rows[2].kind == "body"


def test_is_author_line():
    assert is_author_line("Zhenjin Xu, Keqi Deng, Yang Zhang, Bin Zhu")
    assert is_author_line("Jianyi Zheng, and Dezhi Wu")
    assert is_author_line("Giao T.M. Nguyen, Cedric Plesse")   # 中间名缩写（R06 放宽）
    assert not is_author_line("This is a normal sentence with several words in it.")
    assert not is_author_line("Efficient ion transport and enriched responsive modals.")
    # 纯缩写行（"Z. Xu, K. Deng"）走 initials 分类（AUTHOR_INITIALS_RE 前置）；此处断言其
    # 在 classify_lines 中仍为 meta，而非误入 body
    rows = [_row("Z. Xu, K. Deng, Y. Zhang, B. Zhu, J. Yang,")]
    classify_lines(rows)
    assert rows[0].kind == "meta"


def test_classify_meta_lines():
    rows = [
        _row("Z. Xu, K. Deng, Y. Zhang, B. Zhu, J. Yang,"),
        _row("E-mail: zjy@xmu.edu.cn; wdz@xmu.edu.cn"),
        _row("The ORCID identification number(s) for the author(s)"),
        _row("Pen-Tung Sah Institute of Micro-Nano Science and Technology"),
        _row("Received: May 19, 2024"),
        _row("DOI: 10.1002/adma.202407106"),
    ]
    classify_lines(rows)
    assert all(r.kind == "meta" for r in rows)


def test_classify_heading_and_caption():
    rows = [
        _row("2.1. Electron/Ion Transport Mechanism and Structure Design", font=12),
        _row("Soft Actuator", font=12),                      # 标题续行
        _row("1. Introduction", font=12),
        _row("Fig. 1. Schematic illustrations of the actuator.", font=9),
        _row("energy, Φ is vacuum electrostatic potential,", font=9),   # 图注续行
        _row("Table 2. Performance comparison.", font=9),
    ]
    classify_lines(rows)
    assert rows[0].kind == "heading"
    assert rows[1].kind == "heading"      # 标题续行（无编号、短）
    assert rows[2].kind == "heading"
    assert rows[3].kind == "caption"
    assert rows[4].kind == "caption"      # 图注续行（小写开头）
    assert rows[5].kind == "caption"


def test_classify_title_vs_body():
    rows = [
        _row("Reinforced Magnetic-Responsive Electro-Ionic Artificial Muscles", font=16, page=1),
        _row("Body text line one of the abstract.", font=10, page=1),
        _row("Body text line two continues the abstract.", font=10, page=1),
        _row("Body text line three of the abstract.", font=10, page=1),
        _row("Body text line four of the abstract.", font=10, page=1),
        _row("Body text line five of the abstract.", font=10, page=1),
    ]
    classify_lines(rows)
    assert rows[0].kind == "title"
    assert all(r.kind == "body" for r in rows[1:])


def test_contamination():
    assert is_contaminated("See https://advanced.onlinelibrary.wiley.com for details")
    assert is_contaminated("license under a Creative Commons License")
    assert is_contaminated("words 15214095, 2024, 47, Downloaded from x")
    assert not is_contaminated("A clean sentence about the actuator.")
    cleaned = strip_contamination("The results are good. zjy@xmu.edu.cn 15214095, 2024, 47")
    assert "zjy" not in cleaned and "15214095" not in cleaned
