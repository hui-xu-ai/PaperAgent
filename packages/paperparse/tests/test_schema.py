#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: tests/test_schema.py
功能: T1 契约库单元测试（往返序列化 / 校验 / JSON Schema 导出）
对外接口: 无（测试）
版本: v1.0.0 (2025-xx-xx)
版本历史:
  v1.0.0 初始版本
"""
import json

import pytest
from pydantic import ValidationError

from paperparse.middleware.schema import (
    INTERFACE_VERSION,
    ArticleDocument,
    ArticleMetadata,
    Paragraph,
    TextBlock,
    export_schema,
    validate_doc,
)


def test_interface_version():
    assert INTERFACE_VERSION == "1.0"


def test_round_trip_document():
    doc = ArticleDocument(
        metadata=ArticleMetadata(title="T", keywords=["a", "b"]),
        paragraphs=[Paragraph(para_id="P001", order=1, text_en="Hello world.", confidence=0.9)],
    )
    data = doc.model_dump(mode="json")
    doc2 = validate_doc(data)
    assert doc2.metadata.title == "T"
    assert doc2.paragraphs[0].text_en == "Hello world."
    assert doc2.schema_version == INTERFACE_VERSION


def test_textblock_fields():
    tb = TextBlock(block_id="B1", page=1, bbox=(0.0, 0.0, 100.0, 50.0), text="x", source="mineru")
    assert tb.bbox[2] == 100.0
    assert tb.source == "mineru"


def test_invalid_page_raises():
    with pytest.raises(ValidationError):
        TextBlock(block_id="B1", page=0, bbox=(0, 0, 1, 1), text="x")


def test_export_schema_and_dumpable():
    s = export_schema(ArticleDocument)
    assert "properties" in s
    assert "metadata" in s["properties"]
    json.dumps(s)  # 可 JSON 序列化（供 tools/*.json 导出）


def test_extra_fields_ignored():
    doc = validate_doc({"metadata": {"title": "T", "unknown_field": 1}, "paragraphs": []})
    assert doc.metadata.title == "T"
