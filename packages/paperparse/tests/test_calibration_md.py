#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: tests/test_calibration_md.py
功能: T9 校准 MD 解析与相似度匹配单元测试
对外接口: 无（测试）
版本: v1.0.0 (2025-xx-xx)
版本历史:
  v1.0.0 初始版本
"""
from paperparse.core.calibration_md import (
    apply_calibration,
    match_paragraphs,
    normalize,
    parse_md,
)
from paperparse.core.stitch_code import stitch
from paperparse.middleware.schema import LayoutInfo, Paragraph, TextBlock

CALIB_MD = """# Test Paper Title
Some journal line
Research Article

## Abstract

This is the abstract paragraph with content about ionic actuators.

## 1 Introduction

The first introduction paragraph mentions laser induced graphene.

The second introduction paragraph continues the story.

Search for more papers by this author

## References

[1] Some reference entry.
"""


def test_parse_md(tmp_work):
    p = tmp_work / "calib.md"
    p.write_text(CALIB_MD, encoding="utf-8")
    doc = parse_md(p)
    assert doc.title == "Test Paper Title"
    headings = [s.heading for s in doc.sections]
    assert headings == ["Abstract", "1 Introduction", "References"]
    assert doc.total_paragraphs == 4  # Abstract 1 + Introduction 2 + References 1（前置行不计）


def test_normalize_ligatures():
    assert normalize("Arti\ufb01cial Muscle") == "artificial muscle"
    assert normalize("  multi\n  space ") == "multi space"


def test_match_and_unmatched():
    paragraphs = [
        Paragraph(para_id="P001", order=1, text_en=(
            "This is the abstract paragraph with content about ionic actuators."),
            confidence=0.6),
        Paragraph(para_id="P002", order=2, text_en=(
            "Completely unrelated topic about quantum computing hardware."),
            confidence=0.9),
    ]
    # 用内存构建：直接构造 CalibDoc
    from paperparse.core.calibration_md import CalibDoc, CalibSection
    calib = CalibDoc(title="t", sections=[CalibSection(
        heading="Abstract", paragraphs=[
            "This is the abstract paragraph with content about ionic actuators."])])
    report = match_paragraphs(paragraphs, calib)
    assert report.matched_paragraph_ids == ["P001"]
    assert report.unmatched_count == 1
    assert report.stats["candidate_count"] == 2


def test_apply_calibration():
    from paperparse.core.calibration_md import CalibrationReport
    p = Paragraph(para_id="P001", order=1, text_en="text", confidence=0.6,
                  needs_ai_check=True)
    report = CalibrationReport(matched_paragraph_ids=["P001"],
                               fixed_paragraph_ids=["P001"])
    from paperparse.middleware.schema import StitchResult
    res = StitchResult(paragraphs=[p])
    apply_calibration(res, report)
    assert p.is_calibrated is True
    assert p.confidence == 0.7
    assert p.needs_ai_check is False


def test_parse_md_real_structure(tmp_work):
    """模拟 Wiley 网页版 MD 结构（与真实校准 MD 同构，仅 4 段）"""
    p = tmp_work / "wiley.md"
    p.write_text(
        "# Real Title\n\n## Abstract\n\nAbstract text here.\n\n"
        "## 1 Introduction\n\nFirst paragraph.\n\nSecond paragraph.\n\n",
        encoding="utf-8")
    doc = parse_md(p)
    assert doc.sections[1].heading == "1 Introduction"
    assert len(doc.sections[1].paragraphs) == 2
