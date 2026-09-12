#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: tests/test_stitch.py
功能: T8 代码优先拼接单元测试
对外接口: 无（测试）
版本: v1.0.0 (2025-xx-xx)
版本历史:
  v1.0.0 初始版本
"""
from paperparse.core.stitch_code import stitch
from paperparse.middleware.schema import LayoutInfo, TextBlock


def _b(bid, page, col, y, text, kind="body", x0=0.0, x1=100.0, order=None):
    tb = TextBlock(block_id=bid, page=page, bbox=(x0, float(y), x1, float(y + 30)),
                   text=text, kind=kind, column=col)
    tb.order = order if order is not None else int(bid[1:])
    return tb


def _layout(reference_pages=None):
    return LayoutInfo(page_count=3, reference_zone_pages=reference_pages or [])


def test_merge_same_column():
    blocks = [
        _b("B1", 1, 0, 100, "The quick brown fox jumps over the lazy"),
        _b("B2", 1, 0, 140, "dog and runs away."),
    ]
    res = stitch(blocks, _layout())
    assert len(res.paragraphs) == 1
    assert res.paragraphs[0].text_en == "The quick brown fox jumps over the lazy dog and runs away."
    assert res.stats["cross_column_joins"] == 0


def test_terminator_splits():
    blocks = [
        _b("B1", 1, 0, 100, "First sentence ends."),
        _b("B2", 1, 0, 140, "Second paragraph starts here."),
    ]
    res = stitch(blocks, _layout())
    assert len(res.paragraphs) == 2
    assert res.paragraphs[0].text_en == "First sentence ends."
    assert res.paragraphs[1].text_en == "Second paragraph starts here."


def test_cross_column_continuation():
    blocks = [
        _b("B1", 1, 0, 500, "This sentence continues across the column boundary without"),
        _b("B2", 1, 1, 100, "stopping here."),
    ]
    res = stitch(blocks, _layout())
    assert len(res.paragraphs) == 1
    assert res.paragraphs[0].text_en.endswith("without stopping here.")
    assert res.stats["cross_column_joins"] == 1
    assert res.paragraphs[0].confidence == 0.65
    assert res.paragraphs[0].needs_ai_check is True


def test_hyphen_fix():
    blocks = [
        _b("B1", 1, 0, 100, "The material was synthesized in an experi-"),
        _b("B2", 1, 0, 140, "ment chamber."),
    ]
    res = stitch(blocks, _layout())
    assert res.paragraphs[0].text_en == "The material was synthesized in an experiment chamber."
    assert res.stats["hyphen_fixes"] == 1


def test_heading_section():
    blocks = [
        _b("H1", 1, 0, 100, "2. Results and Discussion", kind="heading"),
        _b("B1", 1, 0, 140, "Results show improvement."),
    ]
    res = stitch(blocks, _layout())
    assert res.paragraphs[0].text_en == "2. Results and Discussion"
    assert res.paragraphs[0].confidence == 1.0
    assert res.paragraphs[1].section == "2. Results and Discussion"
    assert res.stats["sections"] == 1


def test_caption_standalone():
    blocks = [
        _b("B1", 1, 0, 100, "Body text before the figure."),
        _b("C1", 1, 0, 300, "Fig. 1. Example caption.", kind="caption"),
    ]
    res = stitch(blocks, _layout())
    texts = [p.text_en for p in res.paragraphs]
    assert "Fig. 1. Example caption." in texts
    cap = [p for p in res.paragraphs if p.text_en.startswith("Fig.")][0]
    assert cap.needs_ai_check is False


def test_reference_zone_excluded():
    blocks = [
        _b("B1", 2, 0, 100, "Body paragraph on normal page."),
        _b("R1", 3, 0, 100, "[1] A. Author, J. Mag. 2020, 5, 1."),
        _b("R2", 3, 0, 140, "[2] B. Writer, Adv. Mater. 2021, 33, 2."),
    ]
    res = stitch(blocks, _layout(reference_pages=[3]))
    assert len(res.paragraphs) == 1
    assert res.paragraphs[0].text_en.startswith("Body paragraph")


def test_low_confidence_ids():
    blocks = [
        _b("B1", 1, 0, 500, "unfinished sentence at column end"),
        _b("B2", 1, 1, 100, "continued next column."),
    ]
    res = stitch(blocks, _layout())
    assert res.low_confidence_ids == [res.paragraphs[0].para_id]


def test_meta_rows_excluded():
    """作者/邮箱/ORCID/DOI 等 meta 行绝不混入正文（用户问题 5）"""
    blocks = [
        _b("B1", 1, 0, 100, "First line of introduction text without"),
        _b("M1", 1, 0, 130, "Z. Xu, K. Deng, Y. Zhang, B. Zhu, J. Yang,", kind="meta"),
        _b("M2", 1, 0, 160, "E-mail: zjy@xmu.edu.cn", kind="meta"),
        _b("B2", 1, 0, 190, "ending punctuation."),
    ]
    res = stitch(blocks, _layout())
    assert len(res.paragraphs) == 1
    text = res.paragraphs[0].text_en
    assert "Z. Xu" not in text and "E-mail" not in text
    assert text == "First line of introduction text without ending punctuation."


def test_lowercase_start_joins():
    """段首小写 → 强制续接（句子被版面/图打断，用户问题 9/10）"""
    blocks = [
        _b("B1", 1, 0, 100, "The observed improvement from 2.15 (pristine LIG)"),
        _b("B2", 1, 1, 100, "to 3.27 S cm−1 (P-LIG) results"),
    ]
    res = stitch(blocks, _layout())
    assert len(res.paragraphs) == 1
    assert res.paragraphs[0].text_en.endswith("(pristine LIG) to 3.27 S cm−1 (P-LIG) results")


def test_caption_delay_does_not_break_sentence():
    """图注遇到未完成句子时延迟插入，不打断拼接（跨图拼接，用户问题 9）"""
    blocks = [
        _b("B1", 1, 0, 100, "with core–shell transition metal nanoparticles were"),
        _b("C1", 1, 0, 200, "Fig. 2. SEM image of the sample.", kind="caption"),
        _b("B2", 2, 0, 100, "transformed from the organic components of do"),
        _b("B3", 2, 0, 140, "main materials."),
    ]
    res = stitch(blocks, _layout())
    texts = [p.text_en for p in res.paragraphs]
    merged = [t for t in texts if t.startswith("with core–shell")]
    assert merged and "transformed from the organic" in merged[0]
    # 图注仍在输出中
    assert any("Fig. 2." in t for t in texts)


def test_contamination_stripped_and_flagged():
    """拼接后污染剥离：页脚/下载标记被清除（用户问题 6/12）"""
    blocks = [
        _b("B1", 1, 0, 100, "The device shows a compact size, 15214095, 2024, 47, Downloaded from"),
        _b("B2", 1, 0, 140, "https://advanced.onlinelibrary.wiley.com and good stability."),
    ]
    res = stitch(blocks, _layout())
    text = res.paragraphs[0].text_en
    assert "15214095" not in text
    assert "Downloaded" not in text
    assert "wiley" not in text


def test_caption_number_grouping():
    """无编号图注续行（跨列被打断）并入最近编号图注段（用户问题 8）"""
    blocks = [
        _b("C1", 1, 0, 100, "Figure 4. Schematic illustration of energy band structures", kind="caption"),
        _b("B1", 1, 0, 140, "Some body paragraph in between."),
        _b("C2", 2, 1, 100, "energy, Φ is vacuum electrostatic potential, E F stands for", kind="caption"),
        _b("C3", 2, 1, 130, "Fermi level, E c and E v are conduction band.", kind="caption"),
    ]
    res = stitch(blocks, _layout())
    caps = [p for p in res.paragraphs if p.is_caption]
    assert len(caps) == 1
    assert caps[0].text_en.startswith("Figure 4.")
    assert "energy, Φ" in caps[0].text_en
    assert "Fermi level" in caps[0].text_en
