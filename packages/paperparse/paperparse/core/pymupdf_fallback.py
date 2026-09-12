#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: skill/src/paperparse/core/pymupdf_fallback.py
功能: PyMuPDF 本地解析兜底：**行级文本提取**（每行一个 TextBlock，含字号/加粗/bbox）
      + 行文本尾部污染剥离（页脚模式：Downloaded from / © / DOI 数字序列 / URL）
      + 页面区域高清渲染（S4 大图保留依赖）
      —— 行级是"先分类再拼接"的基础：页眉/页脚/元信息/标题可被精确分类，不再混入正文块
对外接口: extract_blocks / render_clip / page_sizes / strip_line_tail
版本: v1.1.0 (2026-08-19)
版本历史:
  v1.1.0 行级提取重构（kind 由 block_classify 赋值；行尾污染剥离）
  v1.0.0 初始版本（块级；import pymupdf，fitz 已弃用）
"""
from __future__ import annotations

import re
from pathlib import Path

import pymupdf

from paperparse.middleware.errors import PaperError
from paperparse.middleware.schema import ParserBlocks, TextBlock

__all__ = ["extract_blocks", "render_clip", "page_sizes", "strip_line_tail"]

# 行尾污染模式（页脚/版权/下载标记；按顺序剥离，均可整体匹配）
_TAIL_PATTERNS = [
    re.compile(r"\s*\d{5,},\s*\d{4},\s*\d{1,2}\b.*$"),          # 15214095, 2024, 47, Downloaded from ...
    re.compile(r"\s*Downloaded from\s+\S+.*$", re.IGNORECASE),
    re.compile(r"\s*©\s*\d{4}.*$"),
    re.compile(r"\s*https?://\S+.*$", re.IGNORECASE),
    re.compile(r"\s*www\.\S+\s*$", re.IGNORECASE),
]
# 整行页脚模式（Wiley 类期刊每页底部）：页码 "2407106 (7 of 15)"、期刊行 "Adv. Mater. 2024, 36, 2407106"
_FOOTER_LINE_RES = [
    re.compile(r"^\d{5,}\s*\(\d+\s+of\s+\d+\)\s*$"),
    re.compile(r"^[A-Z][\w\s.]*?\d{4}\s*,\s*\d{1,3}\s*,\s*\d{3,8}\s*$"),
]


def strip_line_tail(text: str) -> str:
    """[全局] 剥离行尾页脚/版权/下载标记（用户问题 6 的干扰模式）"""
    out = text
    for pat in _TAIL_PATTERNS:
        out = pat.sub("", out)
    if out:
        for fpat in _FOOTER_LINE_RES:
            if fpat.match(out.strip()):
                return ""                     # 整行页脚（期刊行/页码行）
    return out


def _join_span_texts(span_texts: list[str]) -> str:
    """[局部] 行内 span 连接：行尾连字符断词直连；否则空格连接"""
    out = ""
    for st in span_texts:
        st = st.strip()
        if not st:
            continue
        if out.endswith("-") and not out.endswith("--"):
            out = out[:-1] + st
        else:
            out = (out + " " + st) if out else st
    return out


def extract_blocks(pdf_path: str | Path) -> ParserBlocks:
    """[全局] 行级文本提取（每行一个 TextBlock；kind 待 block_classify 赋值）

    参数:
        pdf_path: PDF 路径
    返回:
        ParserBlocks（source="pymupdf"；blocks 为行级）
    报错:
        PaperError PAPER-0002（打开失败）
    """
    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception as exc:
        raise PaperError("PAPER-0002", stage="S1", path=str(pdf_path),
                         detail={"reason": f"pymupdf 打开失败: {exc}"}) from exc

    raw: list[TextBlock] = []
    try:
        for pno in range(doc.page_count):
            page = doc[pno]
            d = page.get_text("dict")
            for b in d.get("blocks", []):
                if b.get("type") != 0:      # 跳过图片块（由 image_extract 处理）
                    continue
                for line in b.get("lines", []):
                    spans = line.get("spans", [])
                    if not spans:
                        continue
                    text = strip_line_tail(_join_span_texts(
                        [s.get("text", "") for s in spans]))
                    if not text:
                        continue
                    try:
                        bbox = tuple(float(v) for v in line["bbox"])
                    except (TypeError, ValueError, KeyError):
                        continue
                    font_size = max((float(s.get("size", 0.0)) for s in spans), default=0.0)
                    bold = any((int(s.get("flags", 0)) & 2 ** 4) for s in spans)
                    raw.append(TextBlock(
                        block_id="", page=pno + 1, bbox=bbox, text=text,
                        kind="other", font_size=font_size, bold=bold,
                        confidence=1.0, source="pymupdf"))
        page_count = doc.page_count
    finally:
        doc.close()

    for i, tb in enumerate(raw):
        tb.block_id = "B%05d" % (i + 1)

    return ParserBlocks(source="pymupdf", pages=page_count, blocks=raw)


def render_clip(pdf_path: str | Path, page_no: int,
                bbox: tuple[float, float, float, float], dpi: int = 300) -> bytes:
    """[全局] 按 bbox 渲染页面区域为 PNG bytes（S4 大图保留；300 DPI 默认）

    参数:
        pdf_path: PDF 路径
        page_no: 页码（1 基）
        bbox: (x0, y0, x1, y1)
        dpi: 渲染分辨率
    返回:
        PNG 字节
    报错:
        PaperError PAPER-0030（渲染失败）
    """
    try:
        doc = pymupdf.open(str(pdf_path))
        try:
            page = doc[page_no - 1]
            zoom = dpi / 72.0
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom),
                                  clip=pymupdf.Rect(*bbox))
            return pix.tobytes("png")
        finally:
            doc.close()
    except Exception as exc:
        raise PaperError("PAPER-0030", stage="S4", path=str(pdf_path),
                         detail={"page": page_no, "bbox": list(bbox),
                                 "exc": str(exc)[:300]}) from exc


def page_sizes(pdf_path: str | Path) -> dict[int, list[float]]:
    """[全局] 每页尺寸 {页: [宽, 高]}（供 layout 分析）"""
    doc = pymupdf.open(str(pdf_path))
    try:
        return {i + 1: [round(doc[i].rect.width, 2), round(doc[i].rect.height, 2)]
                for i in range(doc.page_count)}
    finally:
        doc.close()
