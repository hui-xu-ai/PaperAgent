# -*- coding: utf-8 -*-
"""路径与运行时配置注入（paperlit 不硬编码任何绝对路径）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Roots:
    """paperlit 数据根路径集合（调用方注入）。"""
    data_dir: Path              # data/（应用数据根）
    lit_dir: Path               # literature/（文献检索库根目录）

    @property
    def lit_db(self) -> Path:
        return self.lit_dir / "lit.db"

    @property
    def faiss_dir(self) -> Path:
        return self.lit_dir / "faiss"

    @property
    def bib_import_dir(self) -> Path:
        """bib 文件导入目录（用户拖入 WoS 导出文件的位置）。"""
        return self.lit_dir / "bib_inbox"

    @property
    def journals_db(self) -> Path:
        return self.data_dir / "reference" / "journals.db"

    def ensure(self) -> "Roots":
        for d in (self.lit_dir, self.faiss_dir, self.bib_import_dir,
                  self.lit_db.parent):
            d.mkdir(parents=True, exist_ok=True)
        return self


@dataclass
class LitSettings:
    """文献检索行为设置。"""
    # 嵌入模型（硅基流动 API）
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = 1024
    embedding_api_key: str = ""
    # Reranker 模型（硅基流动 API）
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    reranker_api_key: str = ""
    # 检索参数
    top_k_coarse: int = 200         # L1 粗筛候选数
    top_k_rerank: int = 50          # L2 rerank 后保留数
    top_k_deliver: int = 20         # L3 单次交付数
    # 元数据补全
    enrich_batch_size: int = 50     # OpenAlex 批量查询批次大小
    enrich_rate_limit: float = 5.0  # 每秒请求数
    # 价值评分权重
    score_weights: dict = field(default_factory=lambda: {
        "reranker": 0.40, "cited": 0.20, "pagerank": 0.15,
        "topic": 0.10, "year": 0.05, "star": 0.10,
    })
