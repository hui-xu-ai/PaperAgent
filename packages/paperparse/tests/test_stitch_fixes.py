#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: tests/test_stitch_fixes.py
功能: T-B 词段错位修复回归测试（真实样本 PDF 全流程 S1→S3，只断言关键段落，
      不读全文——D9 纪律）：
      - 标题多行合并（首页标题 3 行 → 1 段；编号标题续行合并）
      - 独立标题不误合并（"2. Results" 与 "2.1."、"3. Conclusion" 与 "Supporting Information"）
      - 正文行不误判标题（"2.0 Hz ..."、"134.2 and ..."、"Owing to ... stor-"）
      - 正文引用句不误判图注（"Figure 4g shows ..."、"Figure 7b ) ..."）
      - 有编号图注独立成段（Figure 7 不被并入 Figure 6）
      - 全宽标题行不被错分列（标题首行顺序正确）
对外接口: 无（测试）
版本: v1.0.0 (2026-08-18)
版本历史:
  v1.0.0 初始版本（T-B 回归）
"""
from pathlib import Path

import pytest

from paperparse.core.block_classify import classify_lines
from paperparse.core.layout import analyze, reading_order
from paperparse.core.pymupdf_fallback import extract_blocks, page_sizes
from paperparse.core.stitch_code import stitch

PDF = str(Path(__file__).resolve().parent / "samples" / "10.1002_adma.202407106.pdf")


@pytest.fixture(scope="module")
def stitched():
    """真实样本 S1→S3 全流程（模块级一次）"""
    blocks = extract_blocks(PDF)
    classify_lines(blocks.blocks)
    lay = analyze(blocks.blocks, page_sizes=page_sizes(PDF))
    ordered = reading_order(blocks.blocks, lay)
    return stitch(ordered, lay), ordered


def _headings(res):
    return [p.text_en for p in res.paragraphs if p.is_heading]


def _captions(res):
    return [p.text_en for p in res.paragraphs if p.is_caption]


def _body_of(res, prefix):
    return [p.text_en for p in res.paragraphs
            if not p.is_heading and not p.is_caption and p.text_en.startswith(prefix)]


# ---------- 标题合并 ----------

def test_title_three_lines_merged(stitched):
    """首页标题 3 行合并为 1 段，且顺序正确（首行在前）"""
    res, ordered = stitched
    heads = _headings(res)
    t = heads[0]
    assert t.startswith("Reinforced Magnetic-Responsive Electro-Ionic")
    assert "Muscles by 3D Laser-Induced Graphene" in t
    assert "Nano-Heterostructures" in t
    # 标题首行（全宽行）不被分到右列：order 必须小于第 2 行
    bmap = {b.block_id: b for b in ordered}
    assert bmap["B00002"].order < bmap["B00003"].order < bmap["B00004"].order


def test_numbered_heading_continuation_merged(stitched):
    """编号标题续行合并：'2.1. ... of' + 'Soft Actuator' → 1 段"""
    heads = _headings(stitched[0])
    h = next(x for x in heads if x.startswith("2.1."))
    assert "Soft Actuator" in h


def test_distinct_headings_not_merged(stitched):
    """独立标题不误合并：'2. Results' 与 '2.1.' 分开；'3. Conclusion' 与 'Supporting Information' 分开"""
    heads = _headings(stitched[0])
    h21 = next(x for x in heads if x.startswith("2.1."))
    assert not h21.startswith("2. Results")          # 未并入 2. Results
    h2 = next(x for x in heads if x.startswith("2. Results"))
    assert not h2.endswith("2.1.")
    conc = next(x for x in heads if x.startswith("3. Conclusion"))
    assert "Supporting Information" not in conc
    supp = next(x for x in heads if x == "Supporting Information")
    assert supp == "Supporting Information"


def test_heading_count_matches_calibration(stitched):
    """标题数量与校准版一致（7 个正文标题：Abstract 由渲染插入）"""
    heads = _headings(stitched[0])
    # 标题：文档标题 + 1. Introduction / 2. Results / 2.1 / 2.2 / 3. Conclusion / Supporting Information
    assert len(heads) == 7


# ---------- 正文不误判标题 ----------

def test_body_not_heading(stitched):
    """正文行恢复 body：'2.0 Hz ...'、'Owing to ... stor-'、'134.2 and ...' 不得为标题"""
    res, _ = stitched
    heads = _headings(res)
    for h in heads:
        assert not h.startswith("2.0 Hz"), "正文 '2.0 Hz (...)' 被误判标题: %r" % h
        assert not h.startswith("134.2"), "正文 '134.2 and ...' 被误判标题: %r" % h
        assert not h.startswith("Owing to the superior"), "正文 'Owing to ...' 被误判标题: %r" % h
    # 它们应存在于正文段中（"2.0 Hz ..." 行并入前一段，作为段中内容）
    body_all = " ".join(p.text_en for p in res.paragraphs
                        if not p.is_heading and not p.is_caption)
    assert "2.0 Hz (Figure 6b" in body_all
    assert any(t.startswith("Owing to the superior") for t in _body_of(res, "Owing"))


# ---------- 图注 ----------

def test_caption_reference_sentences_not_caption(stitched):
    """正文引用句不误判图注：'Figure 4g shows'、'Figure 7b ), indicating' 应为正文"""
    res, _ = stitched
    caps = _captions(res)
    for c in caps:
        assert not c.startswith("Figure 4g"), "正文引用句被误判图注: %r" % c
        assert not c.startswith("Figure 7b"), "正文引用句被误判图注: %r" % c
    body_all = " ".join(p.text_en for p in res.paragraphs
                        if not p.is_heading and not p.is_caption)
    assert "Figure 4g shows the electrochemical" in body_all
    assert "Figure 7b ), indicating" in body_all


def test_all_captions_separate(stitched):
    """7 个图注各自独立成段（Figure 7 不并入 Figure 6）"""
    caps = _captions(stitched[0])
    assert len(caps) == 7
    fig7 = next(c for c in caps if c.startswith("Figure 7."))
    assert fig7.startswith("Figure 7. Dual-responsive")
    fig6 = next(c for c in caps if c.startswith("Figure 6."))
    assert "Figure 7" not in fig6


# ---------- 本轮：首行缩进续接 + 句子括号收尾 + 图注延迟分组 ----------

def test_cross_page_indent_join(stitched):
    """用户问题 2/3：跨页被拆的段落（前块句号完整 + 后块首行顶格）应续接合并"""
    res, _ = stitched
    p = next(x for x in res.paragraphs
             if x.text_en.startswith("The elemental forms of transition metals"))
    assert "Similar properties are observed" in p.text_en, "跨页顶格续接未生效"
    assert p.text_en.rstrip().endswith(".")


def test_bracket_sentence_end_not_swallow(stitched):
    """句子以引文括号结尾（'...(Figure 1b ).'）视为完整句：'By combining' 段不得吞并后续段"""
    res, _ = stitched
    p = next(x for x in res.paragraphs
             if x.text_en.startswith("By combining the pseudocapacitive"))
    assert p.text_en.rstrip().endswith("(Figure 1b ).")
    assert len(p.coords.get("pages", [])) == 1, "不得跨页吞并后续段落"
    # 'Starting' 段应独立且跨图完整（Figure 1 大图在 page 3 打断）
    q = next(x for x in res.paragraphs
             if x.text_en.startswith("Starting from the process"))
    assert "dispersion within the electrodes" in q.text_en
    assert len(q.coords.get("pages", [])) >= 2


def test_ends_sentence_bracket():
    """_ends_sentence 对引文括号/引号收尾的完整句判定"""
    from paperparse.core.stitch_code import _ends_sentence
    assert _ends_sentence("...materials (Figure 1b ).")
    assert _ends_sentence("...as supported by XRD results.")
    assert _ends_sentence("...Note S1).")
    assert not _ends_sentence("...transition metal nanoparticles were")
    assert not _ends_sentence("...ion stor-")
