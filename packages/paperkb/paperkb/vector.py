# -*- coding: utf-8 -*-
"""向量索引抽象层（接口预留，初期 Noop）。

设计（KB-DESIGN v0.6 §9 建议 11）：FTS5 为主检索，向量作为 1000+ 篇后的
可选叠加。实现即插即用：config.vector_impl 切换，数据统一放 data/vector/。
"""
from __future__ import annotations

import logging
from typing import Protocol

from .config import Roots

logger = logging.getLogger(__name__)


class VectorIndex(Protocol):
    def add_document(self, doi: str, text: str) -> None: ...
    def search(self, query: str, top_k: int = 10) -> list[dict]: ...
    def rebuild(self, roots: Roots) -> None: ...


class NoopVectorIndex:
    """初期空实现：记录日志不动作（FTS5 为主检索）。"""

    def __init__(self, roots: Roots):
        self.roots = roots

    def add_document(self, doi: str, text: str) -> None:
        logger.debug("[vector:noop] add_document %s (%d chars)", doi, len(text))

    def search(self, query: str, top_k: int = 10) -> list[dict]:
        logger.debug("[vector:noop] search %r top_k=%d", query, top_k)
        return []

    def rebuild(self, roots: Roots) -> None:
        logger.info("[vector:noop] rebuild 无动作（FTS5 为主检索）")


def build_vector_index(roots: Roots, impl: str = "noop") -> VectorIndex:
    """按配置构建向量索引实现（未来: sqlite-vec / chroma 即插即用）。"""
    if impl == "noop":
        return NoopVectorIndex(roots)
    raise ValueError(f"未知向量实现: {impl!r}（当前仅支持 noop）")
