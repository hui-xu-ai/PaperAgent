# -*- coding: utf-8 -*-
"""知识库编译结果向量索引。

索引对象：L1 _note.md + L2 _wiki.md（不索引全文，降低成本）。
复用 paperlit 的 SiliconFlow bge-m3 embedding 管线。
存储：data/vector/kb_vectors/（numpy 持久化 + JSON 元数据）。

检索策略（混合检索）：
1. 概念倒排过滤（从 biblio.db concepts 表，零成本）
2. 向量相似度排序（在候选集内）
3. 引用关系加权（citations 表）
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Protocol

import numpy as np

from .config import Roots

logger = logging.getLogger(__name__)

EMBEDDING_DIM = 1024  # bge-m3 维度

try:
    import faiss
    HAS_FAISS = True
except ImportError:
    HAS_FAISS = False


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


class KbVectorIndex:
    """知识库编译结果向量索引。

    每篇文献最多 4 个 passage：
    - {doi}__title: 标题（高信噪比，快速定位领域）
    - {doi}__note: L1 _note.md 内容
    - {doi}__wiki: L2 _wiki.md 内容
    - {doi}__concepts: 概念定义拼接

     compound key 格式：`{doi}__{passage_type}`
    """

    def __init__(self, roots: Roots, api_key: str = "",
                 model: str = "BAAI/bge-m3"):
        self.roots = roots
        self.api_key = api_key
        self.model = model
        self.dim = EMBEDDING_DIM

        self.index_dir = roots.vector_dir / "kb_vectors"
        self.index_dir.mkdir(parents=True, exist_ok=True)

        self._key_to_idx: dict[str, int] = {}
        self._idx_to_key: list[str] = []
        self._index = None
        self._vectors: np.ndarray | None = None

        self._load_meta()
        self._load_vectors()

    @property
    def size(self) -> int:
        return len(self._idx_to_key)

    def index_paper(self, doi: str, note_text: str = "",
                    wiki_text: str = "", concepts: list[dict] | None = None,
                    title: str = "", force: bool = False) -> int:
        """索引一篇文献的编译结果（增量更新）。

        Returns:
            新增/更新的 passage 数
        """
        if not self.api_key:
            logger.warning("embedding API key 未配置，跳过向量索引")
            return 0

        passages = []
        keys = []

        # 标题（高信噪比语义信号）
        title_key = f"{doi}__title"
        if title and len(title.strip()) > 5:
            if force or title_key not in self._key_to_idx:
                passages.append(title.strip())
                keys.append(title_key)

        # L1 _note.md
        note_key = f"{doi}__note"
        if note_text and len(note_text.strip()) > 50:
            if force or note_key not in self._key_to_idx:
                passages.append(self._truncate(note_text, 2000))
                keys.append(note_key)

        # L2 _wiki.md
        wiki_key = f"{doi}__wiki"
        if wiki_text and len(wiki_text.strip()) > 50:
            if force or wiki_key not in self._key_to_idx:
                passages.append(self._truncate(wiki_text, 3000))
                keys.append(wiki_key)

        # 概念定义拼接
        if concepts:
            concept_key = f"{doi}__concepts"
            concept_text = "\n".join(
                f"{c.get('name', '')}: {c.get('definition', '')}"
                for c in concepts if c.get("name")
            )
            if concept_text and len(concept_text.strip()) > 20:
                if force or concept_key not in self._key_to_idx:
                    passages.append(concept_text)
                    keys.append(concept_key)

        if not passages:
            return 0

        # 批量 embedding
        from paperlit.vector import encode_texts
        embeddings = encode_texts(passages, model=self.model,
                                  api_key=self.api_key)

        added = 0
        for key, emb in zip(keys, embeddings):
            if emb and len(emb) == self.dim:
                if self._add_single(key, emb):
                    added += 1

        if added > 0:
            self._save_meta()
            self._save_vectors()
            logger.info("向量索引更新: doi=%s added=%d total=%d",
                        doi, added, self.size)
        return added

    def search(self, query: str, top_k: int = 20,
               exclude_doi: str = "") -> list[dict]:
        """向量相似度搜索。

        Args:
            query: 查询文本
            top_k: 返回数量
            exclude_doi: 排除的 DOI（搜索自身时排除）

        Returns:
            [{"key": "...", "doi": "...", "passage_type": "...", "score": 0.85}, ...]
        """
        if not self.api_key or self.size == 0:
            return []

        from paperlit.vector import encode_query
        query_vec = encode_query(query, model=self.model, api_key=self.api_key)
        if not query_vec or len(query_vec) != self.dim:
            return []

        results = self._search_by_vector(query_vec, top_k * 3)

        # 解析 compound key，过滤排除项
        output = []
        for key, score in results:
            parts = key.rsplit("__", 1)
            doi = parts[0]
            ptype = parts[1] if len(parts) > 1 else "unknown"
            if exclude_doi and doi == exclude_doi:
                continue
            output.append({
                "key": key,
                "doi": doi,
                "passage_type": ptype,
                "score": score,
            })
            if len(output) >= top_k:
                break

        return output

    def search_by_concepts(self, concept_names: list[str], top_k: int = 20,
                           store=None, exclude_doi: str = "") -> list[dict]:
        """混合检索：概念倒排过滤 + 向量相似度。

        Args:
            concept_names: 查询概念列表
            top_k: 返回数量
            store: KBStore（用于概念倒排查询）
            exclude_doi: 排除的 DOI

        Returns:
            [{"doi": "...", "score": 0.85, "shared_concepts": [...]}, ...]
        """
        if not concept_names:
            return []

        # 阶段 1：概念倒排过滤（零成本 SQL）
        candidate_dois = set()
        if store is not None:
            for name in concept_names:
                rows = store.concept_rows(name)
                for r in rows:
                    candidate_dois.add(r["paper_doi"])

        if exclude_doi:
            candidate_dois.discard(exclude_doi)

        if not candidate_dois:
            return []

        # 阶段 2：在候选集内做向量搜索
        query_text = " ".join(concept_names)
        vector_results = self.search(query_text, top_k=top_k * 3,
                                     exclude_doi=exclude_doi)

        # 过滤到候选集
        filtered = [r for r in vector_results if r["doi"] in candidate_dois]

        # 按 DOI 聚合（取最高分 passage）
        doi_scores: dict[str, float] = {}
        for r in filtered:
            doi = r["doi"]
            if doi not in doi_scores or r["score"] > doi_scores[doi]:
                doi_scores[doi] = r["score"]

        sorted_dois = sorted(doi_scores.keys(),
                             key=lambda d: doi_scores[d], reverse=True)

        return [
            {"doi": doi, "score": doi_scores[doi],
             "shared_concepts": concept_names}
            for doi in sorted_dois[:top_k]
        ]

    def _add_single(self, key: str, embedding: list[float]) -> bool:
        """添加单条向量到索引。"""
        if key in self._key_to_idx:
            return False

        idx = len(self._idx_to_key)
        self._key_to_idx[key] = idx
        self._idx_to_key.append(key)

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

        return True

    def _search_by_vector(self, query_vec: list[float], top_k: int
                          ) -> list[tuple[str, float]]:
        """向量最近邻搜索。"""
        if self.size == 0:
            return []

        q = np.array([query_vec], dtype=np.float32)

        if HAS_FAISS and self._index is not None:
            faiss.normalize_L2(q)
            scores, indices = self._index.search(q, min(top_k, self.size))
            results = []
            for score, idx in zip(scores[0], indices[0]):
                if 0 <= idx < len(self._idx_to_key):
                    results.append((self._idx_to_key[idx], float(score)))
            return results
        else:
            if self._vectors is None or len(self._vectors) == 0:
                return []
            norm = np.linalg.norm(q)
            if norm > 0:
                q = q / norm
            sims = (self._vectors @ q.T).flatten()
            top_indices = np.argsort(sims)[::-1][:top_k]
            return [(self._idx_to_key[i], float(sims[i]))
                    for i in top_indices]

    @staticmethod
    def _truncate(text: str, max_chars: int) -> str:
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "..."

    def _save_meta(self) -> None:
        meta_path = self.index_dir / "kb_index_meta.json"
        meta = {
            "dim": self.dim,
            "model": self.model,
            "size": self.size,
            "key_to_idx": self._key_to_idx,
            "idx_to_key": self._idx_to_key,
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False),
                             encoding="utf-8")

    def _load_meta(self) -> None:
        meta_path = self.index_dir / "kb_index_meta.json"
        if not meta_path.exists():
            return
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self._key_to_idx = meta.get("key_to_idx", {})
            self._idx_to_key = meta.get("idx_to_key", [])
        except Exception as e:
            logger.warning("Failed to load KB vector index meta: %s", e)

    def _load_vectors(self) -> None:
        vec_path = self.index_dir / "kb_vectors.npy"
        if not vec_path.exists():
            return
        try:
            self._vectors = np.load(vec_path)
            if HAS_FAISS and self._vectors is not None:
                self._index = faiss.IndexFlatIP(self.dim)
                self._index.add(self._vectors)
            logger.info("KB vector index loaded: %d vectors", len(self._vectors))
        except Exception as e:
            logger.warning("Failed to load KB vectors: %s", e)

    def _save_vectors(self) -> None:
        if self._vectors is not None:
            vec_path = self.index_dir / "kb_vectors.npy"
            np.save(vec_path, self._vectors)

    # --- Protocol 兼容 ---
    def add_document(self, doi: str, text: str) -> None:
        self.index_paper(doi, note_text=text)

    def rebuild(self, roots: Roots) -> None:
        logger.info("[kb-vector] rebuild 需调用 build_from_kb()")


def build_vector_index(roots: Roots, impl: str = "noop",
                       api_key: str = "") -> VectorIndex:
    """按配置构建向量索引实现。"""
    if impl == "noop":
        return NoopVectorIndex(roots)
    if impl == "kb":
        return KbVectorIndex(roots, api_key=api_key)
    raise ValueError(f"未知向量实现: {impl!r}（支持 noop / kb）")
