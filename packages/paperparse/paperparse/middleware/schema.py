#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: skill/src/paperparse/middleware/schema.py
功能: 数据契约库：全部跨模块 pydantic 模型、INTERFACE_VERSION、JSON Schema 导出
      —— 所有功能包之间只允许通过本模块的模型通信（契约先行）
对外接口: INTERFACE_VERSION / validate_doc / export_schema / 全部模型类
版本: v1.0.0 (2026-08-19)
版本历史:
  v1.0.0 初始版本（契约基线；破坏性变更必须 bump INTERFACE_VERSION 并登记 CHANGELOG）
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

INTERFACE_VERSION = "1.0"

__all__ = [
    "INTERFACE_VERSION", "validate_doc", "export_schema",
    "TextBlock", "ParserBlocks", "LayoutInfo", "Paragraph", "StitchResult",
    "Figure", "Table", "Reference", "ArticleMetadata", "DocumentAudit",
    "ArticleDocument", "CalibrationReport", "WarningItem", "ErrorEnvelope",
    "PdfInfo", "OutputPaths", "RunStats", "PipelineResult", "JobSummary",
    "AuditEvent", "ValidationError",
]


def _now_iso() -> str:
    """[局部] 当前 UTC 时间 ISO 字符串"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Model(BaseModel):
    """[全局] 契约基类：未知字段忽略（向前兼容），序列化 json 时排除 None"""
    model_config = ConfigDict(extra="ignore")


# ---------- 解析层 ----------

BlockKind = Literal[
    "title", "heading", "body", "caption", "header", "footer", "footnote",
    "reference", "equation", "table", "figure", "meta", "other",
]


class TextBlock(Model):
    """[全局] 解析器输出的原始文本块（行级：一行一块；唯一跨解析器形态）"""
    block_id: str = Field(description="块唯一 ID，如 B0001")
    page: int = Field(ge=1, description="页码（从 1 起）")
    bbox: tuple[float, float, float, float] = Field(description="x0,y0,x1,y1（PDF 坐标）")
    text: str
    kind: BlockKind = "other"
    column: Optional[int] = None
    order: Optional[int] = None
    font_size: Optional[float] = None
    bold: bool = False
    confidence: Optional[float] = None
    source: Literal["mineru", "pymupdf", "paddleocr"] = "pymupdf"


class ParserBlocks(Model):
    """[全局] S1 解析阶段产物"""
    source: Literal["mineru", "pymupdf", "paddleocr"]
    pages: int
    blocks: list[TextBlock] = Field(default_factory=list)
    raw_path: Optional[str] = None
    # P-ENHANCE R02：MinerU 原始排版 JSON（content_list.json）本地路径（S1.5 布局骨架消费）
    raw_content_list: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)


# ---------- 版面层 ----------

class LayoutInfo(Model):
    """[全局] S2 版面分析产物"""
    page_count: int
    is_two_column: bool = False
    columns_per_page: dict[int, int] = Field(default_factory=dict)
    column_thresholds: dict[int, float] = Field(default_factory=dict, description="页 → 列分界 x 坐标")
    page_sizes: dict[int, list[float]] = Field(default_factory=dict, description="页 → [宽, 高]")
    noise_block_ids: list[str] = Field(default_factory=list, description="被过滤的页眉/页脚/页码块")
    reference_zone_pages: list[int] = Field(default_factory=list, description="检测到参考文献区的页")
    note: str = ""


# ---------- 拼接层 ----------

class Paragraph(Model):
    """[全局] S3 拼接产物；text_zh 由 M5 翻译阶段写回"""
    para_id: str = Field(description="如 P001")
    order: int
    section: str = Field(default="", description="章节路径，如 '2 Results and Discussion'")
    text_en: str
    text_zh: Optional[str] = None
    source_block_ids: list[str] = Field(default_factory=list, description="溯源：组成该段落的块 ID")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    needs_ai_check: bool = False
    is_calibrated: bool = False
    is_heading: bool = False
    is_caption: bool = False
    coords: dict = Field(default_factory=dict, description="{pages:[...], bbox0:[x0,y0,x1,y1], bbox1:[...]}")


class StitchResult(Model):
    """[全局] S3 拼接阶段产物"""
    paragraphs: list[Paragraph] = Field(default_factory=list)
    low_confidence_ids: list[str] = Field(default_factory=list)
    stats: dict = Field(default_factory=dict, description="block_count/paragraph_count/cross_column_joins/hyphen_fixes")
    calibrated: bool = False


class CalibrationReport(Model):
    """[全局] S3.5 校准 MD 匹配报告"""
    calib_source: str = ""
    matched_paragraph_ids: list[str] = Field(default_factory=list)
    fixed_paragraph_ids: list[str] = Field(default_factory=list)
    unmatched_count: int = 0
    stats: dict = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


# ---------- 图/表/参考文献 ----------

class Figure(Model):
    """[全局] 图片（大图保留策略产物）"""
    fig_id: str = Field(description="如 F001")
    file: str = Field(description="相对 paper.md 的路径，如 images/F001.png")
    caption: Optional[str] = None
    page: int = Field(ge=1)
    bbox: Optional[tuple[float, float, float, float]] = None
    sha256: str = ""
    # R10：摘要图（graphical abstract）——渲染时插在 Abstract 标题下
    abstract: bool = False


class Table(Model):
    """[全局] 表格"""
    table_id: str
    page: int = Field(ge=1)
    caption: Optional[str] = None
    markdown: Optional[str] = None


class Reference(Model):
    """[全局] 参考文献条目（[n] 归一化）"""
    ref_id: str = Field(description="如 R1")
    number: int = Field(ge=1)
    raw_text: str
    doi: Optional[str] = None


# ---------- 元数据 ----------

class ArticleMetadata(Model):
    """[全局] 文献元数据（S5 产物；关键词将进入 Obsidian tags）"""
    title: str = ""
    article_type: str = Field(default="", description="出版商类型标签，如 Research Article")
    authors: list[str] = Field(default_factory=list)
    affiliations: list[str] = Field(default_factory=list)
    abstract: Optional[str] = None
    keywords: list[str] = Field(default_factory=list)
    doi: Optional[str] = None
    journal: Optional[str] = None
    year: Optional[int] = None
    volume: Optional[str] = None
    issue: Optional[str] = None
    pages: Optional[str] = None
    source_pdf: str = ""
    source_html: Optional[str] = None
    parser: str = ""
    extraction_time: str = Field(default_factory=_now_iso)


# ---------- 文档（单一事实源） ----------

class DocumentAudit(Model):
    """[全局] 文档级审计摘要（不含全文）"""
    run_id: str = ""
    warnings: list[str] = Field(default_factory=list)


class ArticleDocument(Model):
    """[全局] 单一事实源：管线最终产物；Markdown 只是它的渲染视图"""
    schema_version: str = INTERFACE_VERSION
    metadata: ArticleMetadata = Field(default_factory=ArticleMetadata)
    paragraphs: list[Paragraph] = Field(default_factory=list)
    figures: list[Figure] = Field(default_factory=list)
    tables: list[Table] = Field(default_factory=list)
    references: list[Reference] = Field(default_factory=list)
    ai_summary: Optional[dict] = None
    audit: DocumentAudit = Field(default_factory=DocumentAudit)


# ---------- 运行结果 ----------

class WarningItem(Model):
    """[全局] 警告条目（不进错误信封的软问题）"""
    code: str
    stage: str
    user_message: str


class ErrorEnvelope(Model):
    """[全局] 错误信封（RFC 7807 风格）：AI 与用户双视角"""
    code: str
    severity: Literal["error", "warning"] = "error"
    stage: str = ""
    retryable: bool = False
    user_message: str = ""
    recovery: str = ""
    ai_detail: dict = Field(default_factory=dict)


class OutputPaths(Model):
    """[全局] 输出文件路径（相对或绝对）"""
    md: str = ""
    images_dir: str = ""
    document_json: str = ""
    audit_dir: str = ""


class RunStats(Model):
    """[全局] 运行统计（监督/预算用）"""
    pages: int = 0
    tokens: int = 0
    duration_sec: float = 0.0
    parser: str = ""
    parse_source: str = ""      # v2.2 解析来源: mineru-v4 | mineru | pymupdf-local | auto-local
    latex_count: int = 0        # v2.2 document.json 中 $ 总数（>0=LaTeX 保留，高精度）


class PipelineResult(Model):
    """[全局] 统一门面 convert_pdf 的返回"""
    run_id: str
    status: Literal["success", "partial", "failed"] = "failed"
    outputs: OutputPaths = Field(default_factory=OutputPaths)
    warnings: list[WarningItem] = Field(default_factory=list)
    error: Optional[ErrorEnvelope] = None
    stats: RunStats = Field(default_factory=RunStats)


class JobSummary(Model):
    """[全局] 批量执行器回传主进程的紧凑摘要（上下文隔离，绝不含文献全文）"""
    job_id: str
    doi: str = ""
    status: Literal["pending", "running", "success", "partial", "failed"] = "pending"
    warnings_count: int = 0
    tokens: int = 0
    md_path: str = ""
    error_code: Optional[str] = None


# ---------- PDF 校验 ----------

class PdfInfo(Model):
    """[全局] S0 校验产物"""
    path: str
    exists: bool = False
    is_pdf: bool = False
    pages: Optional[int] = None
    has_text_layer: bool = False
    is_scanned_hint: bool = False
    is_encrypted: bool = False
    size_bytes: int = 0
    error_code: Optional[str] = None


class AuditEvent(Model):
    """[全局] 审计事件（audit.py 使用；payload 只放数据摘要，禁止全文）"""
    run_id: str
    ts: str = Field(description="UTC ISO 时间")
    stage: str = ""
    kind: Literal["input", "output", "error", "warning", "degrade", "info"] = "info"
    summary: str = ""
    payload: dict = Field(default_factory=dict)
    tokens: Optional[int] = None
    duration_ms: Optional[int] = None


# ---------- 工具函数 ----------

def validate_doc(data: dict) -> ArticleDocument:
    """[全局] 校验并构造 ArticleDocument（供各阶段/AI 写回时使用）

    参数:
        data: JSON 可反序列化字典
    返回:
        ArticleDocument 实例
    报错:
        pydantic.ValidationError（参数非法时抛出，由调度层包装为 PAPER-0501）
    """
    return ArticleDocument.model_validate(data)


def export_schema(model_cls: type[Model]) -> dict:
    """[全局] 导出模型的 JSON Schema（供 tools/*.json 与契约测试）

    参数:
        model_cls: 本模块中的 Model 子类
    返回:
        JSON Schema 字典
    """
    return model_cls.model_json_schema()
