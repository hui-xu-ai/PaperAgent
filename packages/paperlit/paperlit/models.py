# -*- coding: utf-8 -*-
"""数据模型（papers / citations / search results）。

paperlit 的 Paper 模型在 paperkb.PaperMeta 基础上增加：
  - source_* 字段：每个元数据字段的来源标签（wos / crossref / openalex / semantic_scholar）
  - is_reference：标记该文献是否作为某篇主文献的参考文献被引入
  - paper_rank：预计算的 PaperRank 值
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class CitedRef(BaseModel):
    """bib Cited-References 中的一条引用。"""
    author: str = ""
    journal: str = ""
    year: str = ""
    volume: str = ""
    doi: str = ""
    raw: str = ""


class Paper(BaseModel):
    """文献检索库中的单篇文献。"""
    doi: str = ""
    title: str = ""
    abstract: str = ""
    authors: list[str] = Field(default_factory=list)
    affiliations: list[str] = Field(default_factory=list)
    corresponding: list[str] = Field(default_factory=list)
    journal: str = ""
    year: str = ""
    issn: str = ""
    eissn: str = ""
    keywords: list[str] = Field(default_factory=list)
    research_areas: list[str] = Field(default_factory=list)
    wos_categories: list[str] = Field(default_factory=list)
    times_cited: int = 0
    wos_id: str = ""
    references: list[CitedRef] = Field(default_factory=list)

    # 来源标签（每个字段记录数据来源）
    source_main: str = ""           # 主数据来源：wos / openalex / crossref / semantic_scholar
    source_file: str = ""           # 来源 bib 文件名
    source_abstract: str = ""       # 摘要来源
    source_authors: str = ""        # 作者信息来源

    # 状态标记
    is_reference: bool = False      # 是否作为参考文献引入（非主文献）
    is_enriched: bool = False       # 是否已通过 DOI 补全元数据
    imported_at: str = ""
    enriched_at: str = ""

    # 图谱与评分
    paper_rank: float = 0.0         # 预计算 PaperRank
    cocitation_cluster: int = 0     # 共被引聚类 ID

    # 期刊指标（清洗模块填充）
    impact_factor: float = 0.0      # 期刊影响因子
    quartile: str = ""              # JCR 分区（Q1/Q2/Q3/Q4）
    library_citations: int = 0      # 库内被引次数

    @property
    def has_doi(self) -> bool:
        return bool(self.doi)

    @property
    def ref_count(self) -> int:
        return len(self.references)


class Citation(BaseModel):
    """引用边（citing_doi → cited_doi）。"""
    citing_doi: str
    cited_doi: str
    source: str = ""                # 引用关系来源（wos bib / crossref / openalex）
    cited_brief: str = ""           # 被引文献简要（作者/期刊/年）


class SearchResult(BaseModel):
    """检索结果单条。"""
    doi: str
    title: str = ""
    authors: list[str] = Field(default_factory=list)
    year: str = ""
    journal: str = ""
    abstract: str = ""
    times_cited: int = 0
    impact_factor: float = 0.0
    quartile: str = ""
    library_citations: int = 0
    paper_rank: float = 0.0
    relevance_score: float = 0.0    # 向量/reranker 相关性分数
    final_score: float = 0.0        # 综合排序分数
    match_source: str = ""          # 命中路径：vector / keyword / graph
