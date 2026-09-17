# -*- coding: utf-8 -*-
"""FAISS 向量索引管理（构建 / 搜索 / 持久化 / 增量更新）。

无 FAISS 时降级为 numpy 暴力搜索（<10k 文献够用）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np

from ..db import LitStore

logger = logging.getLogger(__name__)

try:
    import faiss
    HAS_FAISS = True
except ImportError:
    HAS_FAISS = False
    logger.info("FAISS 未安装，使用 numpy 降级搜索")


class VectorIndex:
    """向量索引封装（FAISS 或 numpy 降级）。"""

    def __init__(self, dim: int, store: LitStore, index_dir: Path):
        self.dim = dim
        self.store = store
        self.index_dir = Path(index_dir)
        self.index_dir.mkdir(parents=True, exist_ok=True)

        self._doi_to_idx: dict[str, int] = {}
        self._idx_to_doi: list[str] = []
        self._index = None
        self._vectors: np.ndarray | None = None

        self._load_meta()

    @property
    def size(self) -> int:
        return len(self._idx_to_doi)

    def build(self, doi_embeddings: list[tuple[str, list[float]]]) -> int:
        """从零构建索引。

        Args:
            doi_embeddings: [(doi, embedding), ...]

        Returns:
            成功索引的文献数
        """
        valid = [(doi, emb) for doi, emb in doi_embeddings
                 if emb and len(emb) == self.dim]

        if not valid:
            logger.warning("无有效向量可索引")
            return 0

        self._doi_to_idx = {}
        self._idx_to_doi = []
        vectors = []

        for i, (doi, emb) in enumerate(valid):
            self._doi_to_idx[doi] = i
            self._idx_to_doi.append(doi)
            vectors.append(emb)

        mat = np.array(vectors, dtype=np.float32)

        if HAS_FAISS:
            self._index = faiss.IndexFlatIP(self.dim)
            faiss.normalize_L2(mat)
            self._index.add(mat)
        else:
            norms = np.linalg.norm(mat, axis=1, keepdims=True)
            norms[norms == 0] = 1
            self._vectors = mat / norms

        self._save_meta()
        self._save_vectors(mat)

        logger.info("VectorIndex built: %d vectors (dim=%d, faiss=%s)",
                    len(valid), self.dim, HAS_FAISS)
        return len(valid)

    def add(self, doi: str, embedding: list[float]) -> bool:
        """增量添加单条向量。"""
        if not embedding or len(embedding) != self.dim:
            return False

        if doi in self._doi_to_idx:
            return False

        idx = len(self._idx_to_doi)
        self._doi_to_idx[doi] = idx
        self._idx_to_doi.append(doi)

        vec = np.array([embedding], dtype=np.float32)

        if HAS_FAISS and self._index is not None:
            faiss.normalize_L2(vec)
            self._index.add(vec)
        else:
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm
            if self._vectors is None:
                self._vectors = vec
            else:
                self._vectors = np.vstack([self._vectors, vec])

        self._save_meta()
        return True

    def search(self, query_vec: list[float], top_k: int = 20
               ) -> list[tuple[str, float]]:
        """搜索最近邻。

        Returns:
            [(doi, score), ...] 按相似度降序
        """
        if not query_vec or len(query_vec) != self.dim:
            return []

        if self.size == 0:
            return []

        q = np.array([query_vec], dtype=np.float32)

        if HAS_FAISS and self._index is not None:
            faiss.normalize_L2(q)
            scores, indices = self._index.search(q, min(top_k, self.size))
            results = []
            for score, idx in zip(scores[0], indices[0]):
                if idx >= 0 and idx < len(self._idx_to_doi):
                    results.append((self._idx_to_doi[idx], float(score)))
            return results
        else:
            if self._vectors is None or len(self._vectors) == 0:
                return []
            norm = np.linalg.norm(q)
            if norm > 0:
                q = q / norm
            sims = (self._vectors @ q.T).flatten()
            top_indices = np.argsort(sims)[::-1][:top_k]
            return [(self._idx_to_doi[i], float(sims[i])) for i in top_indices]

    def get_embedding_idx(self, doi: str) -> int:
        """获取文献在索引中的位置（-1 表示未索引）。"""
        return self._doi_to_idx.get(doi, -1)

    def _save_meta(self) -> None:
        meta_path = self.index_dir / "index_meta.json"
        meta = {
            "dim": self.dim,
            "size": self.size,
            "doi_to_idx": self._doi_to_idx,
            "idx_to_doi": self._idx_to_doi,
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False),
                             encoding="utf-8")

    def _load_meta(self) -> None:
        meta_path = self.index_dir / "index_meta.json"
        if not meta_path.exists():
            return
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self._doi_to_idx = meta.get("doi_to_idx", {})
            self._idx_to_doi = meta.get("idx_to_doi", [])
        except Exception as e:
            logger.warning("Failed to load index meta: %s", e)

    def _save_vectors(self, mat: np.ndarray) -> None:
        vec_path = self.index_dir / "vectors.npy"
        np.save(vec_path, mat)

    def load_vectors(self) -> bool:
        """加载已保存的向量（numpy 降级模式用）。"""
        vec_path = self.index_dir / "vectors.npy"
        if not vec_path.exists():
            return False

        try:
            self._vectors = np.load(vec_path)

            if HAS_FAISS and self._vectors is not None:
                self._index = faiss.IndexFlatIP(self.dim)
                self._index.add(self._vectors)

            logger.info("Loaded %d vectors from %s",
                        len(self._vectors), vec_path)
            return True
        except Exception as e:
            logger.warning("Failed to load vectors: %s", e)
            return False
