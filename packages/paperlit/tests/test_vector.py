# -*- coding: utf-8 -*-
"""paperlit P4 测试：向量索引（mock embedding API）。"""
from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pytest

from paperlit.config import Roots
from paperlit.db import LitStore
from paperlit.models import Paper
from paperlit.vector import VectorIndex, encode_texts, encode_query


@pytest.fixture
def lit_store(tmp_path):
    roots = Roots(data_dir=tmp_path / "data", lit_dir=tmp_path / "lit")
    roots.ensure()
    store = LitStore(roots)
    store.init_schema()
    return store


@pytest.fixture
def vector_index(tmp_path, lit_store):
    faiss_dir = tmp_path / "lit" / "faiss"
    return VectorIndex(dim=8, store=lit_store, index_dir=faiss_dir)


def _mock_embedding(text: str, dim: int = 8) -> list[float]:
    """生成确定性伪向量（基于文本哈希）。"""
    h = hash(text) % (2**32)
    rng = np.random.RandomState(h)
    vec = rng.randn(dim).astype(np.float32)
    return (vec / np.linalg.norm(vec)).tolist()


def _mock_encode_texts(texts: list[str], **kwargs) -> list[list[float]]:
    return [_mock_embedding(t) for t in texts]


class TestEmbeddings:
    def test_encode_requires_api_key(self):
        with pytest.raises(ValueError, match="API key"):
            encode_texts(["test"], api_key="")

    def test_encode_empty(self):
        result = encode_texts([], api_key="fake-key")
        assert result == []

    def test_encode_with_mock(self):
        with patch("paperlit.vector.embeddings._post_json") as mock_post:
            mock_post.return_value = {
                "data": [{"index": 0, "embedding": [0.1] * 8}]
            }
            result = encode_texts(["test paper"], api_key="fake-key")
            assert len(result) == 1
            assert len(result[0]) == 8

    def test_encode_query_mock(self):
        with patch("paperlit.vector.embeddings._post_json") as mock_post:
            mock_post.return_value = {
                "data": [{"index": 0, "embedding": [0.2] * 8}]
            }
            result = encode_query("solid electrolyte", api_key="fake-key")
            assert len(result) == 8


class TestVectorIndex:
    def test_empty_index(self, vector_index):
        assert vector_index.size == 0
        results = vector_index.search([0.1] * 8, top_k=5)
        assert results == []

    def test_build_index(self, vector_index):
        embeddings = [
            ("10.1002/a", _mock_embedding("paper A")),
            ("10.1002/b", _mock_embedding("paper B")),
            ("10.1002/c", _mock_embedding("paper C")),
        ]
        count = vector_index.build(embeddings)
        assert count == 3
        assert vector_index.size == 3

    def test_search_returns_results(self, vector_index):
        embeddings = [
            ("10.1002/a", _mock_embedding("solid electrolyte battery")),
            ("10.1002/b", _mock_embedding("polymer membrane")),
            ("10.1002/c", _mock_embedding("solid state battery")),
        ]
        vector_index.build(embeddings)

        query_vec = _mock_embedding("solid electrolyte battery")
        results = vector_index.search(query_vec, top_k=2)

        assert len(results) == 2
        assert results[0][0] == "10.1002/a"

    def test_add_incremental(self, vector_index):
        vector_index.build([("10.1002/a", _mock_embedding("A"))])
        assert vector_index.size == 1

        added = vector_index.add("10.1002/b", _mock_embedding("B"))
        assert added is True
        assert vector_index.size == 2

    def test_add_duplicate_ignored(self, vector_index):
        vector_index.build([("10.1002/a", _mock_embedding("A"))])
        added = vector_index.add("10.1002/a", _mock_embedding("A2"))
        assert added is False
        assert vector_index.size == 1

    def test_add_invalid_embedding(self, vector_index):
        added = vector_index.add("10.1002/x", [0.1, 0.2])
        assert added is False

    def test_persistence(self, tmp_path, lit_store):
        faiss_dir = tmp_path / "lit" / "faiss"

        idx1 = VectorIndex(dim=8, store=lit_store, index_dir=faiss_dir)
        idx1.build([
            ("10.1002/a", _mock_embedding("A")),
            ("10.1002/b", _mock_embedding("B")),
        ])

        idx2 = VectorIndex(dim=8, store=lit_store, index_dir=faiss_dir)
        idx2.load_vectors()
        assert idx2.size == 2

        results = idx2.search(_mock_embedding("A"), top_k=1)
        assert len(results) == 1
        assert results[0][0] == "10.1002/a"

    def test_get_embedding_idx(self, vector_index):
        vector_index.build([
            ("10.1002/a", _mock_embedding("A")),
            ("10.1002/b", _mock_embedding("B")),
        ])
        assert vector_index.get_embedding_idx("10.1002/a") == 0
        assert vector_index.get_embedding_idx("10.1002/b") == 1
        assert vector_index.get_embedding_idx("10.1002/x") == -1

    def test_build_with_invalid_embeddings(self, vector_index):
        embeddings = [
            ("10.1002/a", _mock_embedding("A")),
            ("10.1002/b", []),
            ("10.1002/c", [0.1, 0.2]),
            ("10.1002/d", _mock_embedding("D")),
        ]
        count = vector_index.build(embeddings)
        assert count == 2
        assert vector_index.size == 2
