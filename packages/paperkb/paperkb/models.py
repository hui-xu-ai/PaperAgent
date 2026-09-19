# -*- coding: utf-8 -*-
"""数据模型（papers_meta / citations / compile_jobs）。

papers_meta = bib 权威元数据（按 DOI 查询）；document.json 的 metadata 不可信，
只作无 bib 时的兜底显示（见 KB-DESIGN v0.6 §4.0）。
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class CitedRef(BaseModel):
    """bib Cited-References 中的一条引用。"""
    author: str = ""
    journal: str = ""
    year: str = ""
    volume: str = ""
    doi: str = ""           # 可为空（WOS 部分条目无 DOI）
    raw: str = ""           # 原始文本（展示兜底）


class PaperMeta(BaseModel):
    """单篇文献的权威元数据（bib 导入产物）。

    P0-B（2026-09-11）：主键是 **rid**（统一资源键，见 doi.make_rid）；`doi` 只是
    外部标识之一（无 DOI 的中文文献/书/学位论文/无标识资料 rid 非空而 doi 为空）。
    """
    rid: str = ""                                           # 统一资源键（主键）
    doi: str = ""
    title: str = ""
    abstract: str = ""
    authors: list[str] = Field(default_factory=list)
    affiliations: list[str] = Field(default_factory=list)   # 含通讯作者标记的原始文本
    corresponding: list[str] = Field(default_factory=list)  # 通信作者（姓名或邮箱）
    journal: str = ""
    year: str = ""
    month: str = ""
    issn: str = ""
    eissn: str = ""
    keywords: list[str] = Field(default_factory=list)
    research_areas: list[str] = Field(default_factory=list)
    wos_categories: list[str] = Field(default_factory=list)
    funding: str = ""
    times_cited: int = 0
    wos_id: str = ""                                        # WOS:xxxx
    references: list[CitedRef] = Field(default_factory=list)
    source_file: str = ""                                   # 来源 bib 文件
    imported_at: str = ""
    paper_id: int | None = None                             # 可选：关联解析篇 papers.id
    journal_override: str = ""                              # 人工纠正的期刊名（匹配失败时）
    kind: str = ""                                          # 资源类型（paper/thesis/book/…）
                                                            # 空 = 由 rid 前缀推导（layout.kind_for）
    ai_value_score: float | None = None                     # AI 价值评分（0-5，L1+L2 编译时产出）
    topic_score: float | None = None                        # 主题匹配评分（0-1，L1+L2 编译时产出）

    @property
    def citation_count(self) -> int:
        return len(self.references)


class Citation(BaseModel):
    """引用边（citing_doi → cited_doi）。"""
    citing_doi: str
    cited_doi: str
    cited_brief: str = ""        # 引用展示（作者/期刊/年），cited 不在库内时也可见


class CompileJob(BaseModel):
    """编译队列状态（M3 使用；M0 建表）。"""
    paper_doi: str
    level: str = "L1"            # L1/L2/L3
    status: str = "pending"      # pending/queued/compiling/done/failed
    value_score: float = 0.0
    priority: int = 0
    error: str = ""
    started_at: str = ""
    done_at: str = ""
