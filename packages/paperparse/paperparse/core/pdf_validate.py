#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: skill/src/paperparse/core/pdf_validate.py
功能: S0 PDF 校验：文件存在性、PDF 魔数、页数、文本层检测、扫描版提示、加密检测
      （OCR 预留接口：PdfInfo.is_scanned_hint + ocr_available 配置占位，首版不内置 OCR）
对外接口: validate_pdf
版本: v1.0.0 (2026-08-19)
版本历史:
  v1.0.0 初始版本
"""
from __future__ import annotations

import os
from pathlib import Path

import pymupdf

from paperparse.middleware.errors import PaperError
from paperparse.middleware.schema import PdfInfo

__all__ = ["validate_pdf"]

TEXT_SAMPLE_PAGES = 3          # 文本层检测抽样页数
TEXT_LEN_THRESHOLD = 50        # 每页文本长度阈值（字符）
SCANNED_TEXT_RATIO = 0.02      # 文本占比阈值（文本字符数 / 页面像素规模近似）

PDF_MAGIC = b"%PDF"


def _file_signature(path: Path) -> bytes:
    """[局部] 读取文件头（前 8 字节）用于魔数判断"""
    try:
        with open(path, "rb") as f:
            return f.read(8)
    except OSError:
        return b""


def validate_pdf(path: str | Path) -> PdfInfo:
    """[全局] 校验 PDF 并采集基础信息（S0 阶段产物）

    参数:
        path: PDF 文件路径
    返回:
        PdfInfo（exists/is_pdf/pages/has_text_layer/is_scanned_hint/is_encrypted）
    报错:
        PaperError: PAPER-0001（文件不存在/不可读）、PAPER-0002（非 PDF 或损坏）
    """
    p = Path(path)
    info = PdfInfo(path=str(p), size_bytes=0)

    if not p.exists() or not p.is_file():
        raise PaperError("PAPER-0001", stage="S0", path=str(p))
    info.exists = True
    try:
        info.size_bytes = p.stat().st_size
    except OSError:
        info.size_bytes = 0

    if not _file_signature(p).startswith(PDF_MAGIC):
        raise PaperError("PAPER-0002", stage="S0", path=str(p),
                         detail={"reason": "文件头不是 %PDF"})
    info.is_pdf = True

    try:
        doc = pymupdf.open(str(p))
    except Exception as exc:
        raise PaperError("PAPER-0002", stage="S0", path=str(p),
                         detail={"reason": f"pymupdf 打开失败: {exc}"}) from exc

    try:
        info.pages = doc.page_count
        info.is_encrypted = bool(doc.needs_pass)

        # 文本层检测：抽样前 N 页
        total_text_len = 0
        sampled = min(TEXT_SAMPLE_PAGES, doc.page_count)
        for i in range(sampled):
            try:
                total_text_len += len(doc[i].get_text() or "")
            except Exception:
                continue
        info.has_text_layer = total_text_len >= TEXT_LEN_THRESHOLD

        # 扫描版提示：无文本层且页数合理（有文本层则视为数字版）
        if not info.has_text_layer:
            info.is_scanned_hint = True
    finally:
        doc.close()

    return info
