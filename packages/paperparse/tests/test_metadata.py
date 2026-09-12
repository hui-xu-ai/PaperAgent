#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: tests/test_metadata.py
功能: T11 元数据提取单元测试
对外接口: 无（测试）
版本: v1.0.0 (2025-xx-xx)
版本历史:
  v1.0.0 初始版本
"""
from paperparse.core.metadata import extract_metadata
from paperparse.middleware.schema import TextBlock


def _b(bid, page, y, text, kind="body", order=None):
    tb = TextBlock(block_id=bid, page=page, bbox=(50.0, float(y), 300.0, float(y + 20)),
                   text=text, kind=kind)
    tb.order = order if order is not None else int(bid[1:])
    return tb


def test_full_metadata():
    blocks = [
        _b("B1", 1, 50, "Reinforced Magnetic-Responsive Electro-Ionic Artificial Muscles", kind="title"),
        _b("B2", 1, 90, "Zhenjin Xu, Keqi Deng, Yang Zhang, Bin Zhu", kind="body"),
        _b("B3", 1, 130, "Efficient ion transport and enriched responsive modals via modification.", kind="body"),
        _b("B4", 1, 300, "1. Introduction", kind="heading"),
        _b("B5", 2, 100, "Keywords: artificial muscle, ionic actuator, laser induced graphene", kind="body"),
    ]
    meta = extract_metadata(blocks, source_pdf="10.1002_adma.202407106.pdf")
    assert meta.title.startswith("Reinforced Magnetic")
    assert meta.authors[0] == "Zhenjin Xu"
    assert "Efficient ion transport" in meta.abstract
    assert meta.keywords == ["artificial muscle", "ionic actuator", "laser induced graphene"]
    assert meta.doi == "10.1002/adma.202407106"  # 文件名回退


def test_doi_regex_from_text():
    blocks = [_b("B1", 1, 50, "DOI: 10.1002/adma.202407106", kind="body")]
    meta = extract_metadata(blocks)
    assert meta.doi == "10.1002/adma.202407106"


def test_year_from_pdf_meta():
    blocks = [_b("B1", 1, 50, "Title Text Here", kind="title")]
    meta = extract_metadata(blocks, pdf_meta={"creationDate": "D:20240601"})
    assert meta.year == 2024


def test_missing_fields_empty():
    blocks = [_b("B1", 1, 50, "some short line", kind="body")]
    meta = extract_metadata(blocks)
    assert meta.title == "" and meta.doi is None and meta.keywords == []
