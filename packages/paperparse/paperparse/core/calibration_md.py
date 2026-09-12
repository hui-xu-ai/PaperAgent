#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: skill/src/paperparse/core/calibration_md.py
功能: S3.5 校准 MD 辅助（零 token）：流式解析网页版 Markdown（Wiley 等格式），
      与代码拼接段落做词重叠（Dice）相似度匹配，验证/修复拼接置信度
      —— 只读文件一次、内存只保留归一化词索引，禁止全文驻留
对外接口: parse_md / match_paragraphs / apply_calibration / normalize
版本: v1.0.0 (2026-08-19)
版本历史:
  v1.0.0 初始版本
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from paperparse.middleware.errors import PaperError
from paperparse.middleware.schema import CalibrationReport, Paragraph, StitchResult

__all__ = ["parse_md", "match_paragraphs", "apply_calibration", "normalize"]

MAX_CALIB_BYTES = 8 * 1024 * 1024   # 大小守卫：超过则警告并截断处理
MATCH_THRESHOLD = 0.45              # Dice 相似度阈值（同一文献网页版通常 >0.5）

_LIGATURES = str.maketrans({"\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl"})
_WORD_RE = re.compile(r"[a-z0-9]+")
_HEADING_RE = re.compile(r"^(#{1,4})\s+(.*?)\s*$")
_BOILER_RE = re.compile(
    r"^(search for more papers by this author|full access|research article|"
    r"volume \d+|issue \d+|received:|revised:|accepted:|published online|"
    r"© \d{4}|downloaded from https?://|www\.\S+)", re.IGNORECASE)


@dataclass
class CalibSection:
    """[全局] 校准 MD 的一个章节（标题 + 段落列表）"""
    heading: str = ""
    paragraphs: list[str] = field(default_factory=list)


@dataclass
class CalibDoc:
    """[全局] 校准 MD 解析结果（流式构建）"""
    title: str = ""
    sections: list[CalibSection] = field(default_factory=list)
    truncated: bool = False
    total_paragraphs: int = 0


def normalize(text: str) -> str:
    """[全局] 文本归一化：小写 + 连字替换 + 空白折叠（供相似度计算）"""
    t = text.translate(_LIGATURES).lower()
    return re.sub(r"\s+", " ", t).strip()


def _tokens(norm: str) -> set[str]:
    """[局部] 词集合（相似度用）"""
    return set(_WORD_RE.findall(norm))


def _dice(a: set[str], b: set[str]) -> float:
    """[局部] Dice 系数 = 2|A∩B| / (|A|+|B|)"""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return 2.0 * inter / (len(a) + len(b))


def parse_md(path: str | Path) -> CalibDoc:
    """[全局] 流式解析校准 MD：标题/章节/段落（逐行处理，不全文驻留）

    参数:
        path: 校准 MD 路径（如 tests/samples/10.1002_adma.202407106.md）
    返回:
        CalibDoc
    报错:
        PaperError PAPER-0001（文件不存在）
    """
    p = Path(path)
    if not p.exists():
        raise PaperError("PAPER-0001", stage="S3.5", path=str(p))

    size = p.stat().st_size
    truncated = size > MAX_CALIB_BYTES

    doc = CalibDoc(truncated=truncated)
    current = CalibSection()
    buf: list[str] = []

    def flush_para() -> None:
        """[局部] 冲刷当前缓冲为段落（仅当已进入有标题的章节时计数）"""
        nonlocal buf
        text = " ".join(x.strip() for x in buf).strip()
        if text and not _BOILER_RE.match(text):
            current.paragraphs.append(text)
            if current.heading:
                doc.total_paragraphs += 1
        buf = []

    def flush_section() -> None:
        nonlocal current
        flush_para()
        if current.heading:
            doc.sections.append(current)
        else:
            current.paragraphs.clear()   # 无标题的前置行视为噪声，不计入
        current = CalibSection()

    with open(p, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if doc.total_paragraphs > 5000:      # 防失控
                doc.truncated = True
                break
            m = _HEADING_RE.match(line)
            if m:
                flush_section()
                heading = m.group(2).strip()
                # 文档标题：首个一级标题只记 title，不成章节（对应 Wiley 网页版结构）
                if not doc.title and m.group(1) == "#" and len(m.group(2)) < 200:
                    doc.title = heading
                    current = CalibSection()
                    continue
                current.heading = heading
                continue
            if not line.strip():
                flush_para()
                continue
            buf.append(line)
    flush_section()

    if not doc.sections and not doc.title:
        doc.truncated = True
    return doc


def match_paragraphs(paragraphs: list[Paragraph], calib: CalibDoc,
                     threshold: float = MATCH_THRESHOLD) -> CalibrationReport:
    """[全局] 拼接段落 ↔ 校准段落 相似度匹配

    参数:
        paragraphs: stitch() 产出的段落列表
        calib: parse_md() 产物
        threshold: Dice 匹配阈值
    返回:
        CalibrationReport（matched/fixed/unmatched + 统计）
    """
    calib_paras: list[str] = []
    for sec in calib.sections:
        calib_paras.extend(sec.paragraphs)
    calib_tokens = [_tokens(normalize(cp)) for cp in calib_paras]

    matched: list[str] = []
    fixed: list[str] = []
    unmatched = 0
    scores: list[float] = []

    for para in paragraphs:
        if not para.text_en:
            continue
        pt = _tokens(normalize(para.text_en))
        best = 0.0
        for ct in calib_tokens:
            s = _dice(pt, ct)
            if s > best:
                best = s
                if best >= threshold:
                    break
        scores.append(best)
        if best >= threshold:
            matched.append(para.para_id)
            if para.needs_ai_check or para.confidence < 0.8:
                fixed.append(para.para_id)
        else:
            unmatched += 1

    avg = (sum(scores) / len(scores)) if scores else 0.0
    return CalibrationReport(
        calib_source="",
        matched_paragraph_ids=matched,
        fixed_paragraph_ids=fixed,
        unmatched_count=unmatched,
        stats={
            "candidate_count": len(paragraphs),
            "calib_paragraph_count": len(calib_paras),
            "calib_section_count": len(calib.sections),
            "avg_best_dice": round(avg, 3),
            "threshold": threshold,
            "truncated": calib.truncated,
        },
        notes=["校准 MD 截断处理" if calib.truncated else "校准 MD 完整解析"],
    )


def apply_calibration(stitch_result: StitchResult, report: CalibrationReport) -> StitchResult:
    """[全局] 将校准报告回写段落：is_calibrated + 置信度加成

    参数:
        stitch_result: stitch() 产物（原地修改）
        report: match_paragraphs() 产物
    返回:
        修改后的 StitchResult
    """
    if not report.calib_source:
        report.calib_source = "calibration-md"
    matched = set(report.matched_paragraph_ids)
    for p in stitch_result.paragraphs:
        if p.para_id in matched:
            p.is_calibrated = True
            p.confidence = round(min(0.95, p.confidence + 0.10), 3)
            if p.confidence >= 0.7:
                p.needs_ai_check = False
    stitch_result.calibrated = bool(matched)
    stitch_result.low_confidence_ids = [
        p.para_id for p in stitch_result.paragraphs if p.needs_ai_check]
    return stitch_result
