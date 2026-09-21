# -*- coding: utf-8 -*-
"""路径与运行时配置注入（paperkb 不硬编码任何绝对路径）。

所有根路径由调用方（backend container / agent / CLI）注入，包内一律用
相对 Path 组合——迁移/换机只改注入处。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Roots:
    """知识库/应用数据根路径集合（调用方注入）。

    2026-09-12（用户拍板 · 工业级数据布局）：DB 从"全挤在一个文件"改为**五库物理隔离**，
    全部落在 `data/` 下，与知识资产（knowledge_base/）、解析库（library/）分离：
      data/system/app.db        系统数据（设置/Key/用量/插件/任务）
      data/chat/chat.db         会话与消息（+ 答案缓存）
      data/diary/diary.db       文献日记
      data/biblio/biblio.db     文献元数据（papers/papers_meta/identifiers/compile_*/FTS）
      data/reference/journals.db 期刊分区与影响因子（可重导入）
    """
    data_dir: Path          # data/（应用数据根；DB 分目录存放）
    library_dir: Path       # library/（解析工作区）
    kb_dir: Path            # knowledge_base/（自包含知识库：只放知识资产）

    # ---------- 五库路径 ----------
    @property
    def system_db(self) -> Path:
        return self.data_dir / "system" / "app.db"

    @property
    def chat_db(self) -> Path:
        return self.data_dir / "chat" / "chat.db"

    @property
    def diary_db(self) -> Path:
        return self.data_dir / "diary" / "diary.db"

    @property
    def biblio_db(self) -> Path:
        return self.data_dir / "biblio" / "biblio.db"

    @property
    def reference_db(self) -> Path:
        return self.data_dir / "reference" / "journals.db"

    # ---------- 兼容旧名（paperkb KBStore / 既有测试仍用 main_db） ----------
    @property
    def main_db(self) -> Path:
        """paperkb 主库 = 文献元数据库（biblio.db）。"""
        return self.biblio_db

    @property
    def journals_db(self) -> Path:
        return self.reference_db

    @property
    def vector_dir(self) -> Path:
        return self.data_dir / "vector"

    def ensure(self) -> "Roots":
        """确保目录存在（幂等）。"""
        for d in (self.data_dir, self.library_dir, self.kb_dir,
                  self.system_db.parent, self.chat_db.parent, self.diary_db.parent,
                  self.biblio_db.parent, self.reference_db.parent):
            d.mkdir(parents=True, exist_ok=True)
        return self


@dataclass
class KbSettings:
    """知识库行为设置（可配；初期默认值）。"""
    vector_impl: str = "noop"           # noop | kb（KbVectorIndex：块级 bge-m3 向量）
    fts_enabled: bool = True            # meta_fts 索引开关
    fulltext_fts: bool = False          # en.md 全文索引（可选，M4）
    journal_year: int | None = None     # journals.db 查询年份（None=最新）
    # 价值评分权重（M2 使用）
    score_weights: dict = field(default_factory=lambda: {
        "if": 0.20, "cited": 0.15, "ai_value": 0.25, "topic": 0.15, "year": 0.10,
        "paper_rank": 0.10, "lib_cited": 0.05,
    })
    # 用户研究方向主题表（导入界面设置；AI 编译时据此打主题相关度分）
    preferred_topics: list[str] = field(default_factory=list)
    # --- 检索融合与重排（2026-09-21：多路召回不再用硬编码分数，改 RRF + 交叉编码器精排）---
    rrf_k: int = 60                     # RRF 常数（Cormack 2009 建议 60）
    # 二阶段重排默认**关**（同 vector_impl 的约定：库默认保守，应用层显式开启）——
    # 否则任何调用方（含单测）都会因为环境里有 key 而真的打重排 API。
    rerank_enabled: bool = False
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_pool: int = 30               # 送重排的候选数（top-30 × ~350 token ≈ ¥0.0005/次）
    query_vec_cache: bool = True        # 查询向量缓存（data/vector/query_cache.db）
