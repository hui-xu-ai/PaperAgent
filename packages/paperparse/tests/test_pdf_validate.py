#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: tests/test_pdf_validate.py
功能: T4 PDF 校验单元测试
对外接口: 无（测试）
版本: v1.0.0 (2025-xx-xx)
版本历史:
  v1.0.0 初始版本
"""
import pymupdf
import pytest

from paperparse.core.pdf_validate import validate_pdf
from paperparse.middleware.errors import PaperError


def _make_text_pdf(path, pages=2):
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), "Hello PaperAIReader test page %d" % (i + 1), fontsize=12)
    doc.save(str(path))
    doc.close()


def _make_scanned_like_pdf(path):
    """无文本层 PDF：仅插入一张图片"""
    doc = pymupdf.open()
    page = doc.new_page()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 200))
    pix.clear_with(200)
    page.insert_image(pymupdf.Rect(50, 50, 250, 250), pixmap=pix)
    doc.save(str(path))
    doc.close()


def test_valid_text_pdf(tmp_work):
    p = tmp_work / "ok.pdf"
    _make_text_pdf(p)
    info = validate_pdf(p)
    assert info.exists and info.is_pdf
    assert info.pages == 2
    assert info.has_text_layer is True
    assert info.is_scanned_hint is False


def test_scanned_hint(tmp_work):
    p = tmp_work / "scan.pdf"
    _make_scanned_like_pdf(p)
    info = validate_pdf(p)
    assert info.is_pdf
    assert info.has_text_layer is False
    assert info.is_scanned_hint is True


def test_missing_file(tmp_work):
    with pytest.raises(PaperError) as ei:
        validate_pdf(tmp_work / "nope.pdf")
    assert ei.value.code == "PAPER-0001"


def test_not_pdf(tmp_work):
    p = tmp_work / "fake.pdf"
    p.write_text("this is not a pdf", encoding="utf-8")
    with pytest.raises(PaperError) as ei:
        validate_pdf(p)
    assert ei.value.code == "PAPER-0002"
