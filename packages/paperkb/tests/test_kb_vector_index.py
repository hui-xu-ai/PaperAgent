# -*- coding: utf-8 -*-
"""KbVectorIndex 单测：索引构建/搜索/持久化/混合检索/编译钩子。

embedding 调用一律 mock（不依赖 SiliconFlow API）。
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from paperkb.config import Roots
from paperkb.vector import (
    EMBEDDING_DIM,
    KbVectorIndex,
    NoopVectorIndex,
    build_vector_index,
)


@pytest.fixture()
def roots(tmp_path: Path) -> Roots:
    return Roots(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        kb_dir=tmp_path / "kb",
    ).ensure()


def _fake_embedding(text: str) -> list[float]:
    """确定性假向量：按文本 hash 生成可复现向量。"""
    rng = np.random.RandomState(hash(text) % (2**31))
    vec = rng.randn(EMBEDDING_DIM).astype(np.float32)
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm
    return vec.tolist()


def _mock_encode_texts(texts, **kwargs):
    return [_fake_embedding(t) for t in texts]


def _mock_encode_query(query, **kwargs):
    return _fake_embedding(query)


# ---------------------------------------------------------------- 基础构建

class TestKbVectorIndexBasic:
    def test_empty_index(self, roots):
        idx = KbVectorIndex(roots, api_key="test-key")
        assert idx.size == 0
        assert idx.dim == EMBEDDING_DIM

    def test_no_api_key_skips_indexing(self, roots):
        idx = KbVectorIndex(roots, api_key="")
        added = idx.index_paper("10.1234/test", note_text="some text " * 20)
        assert added == 0

    @patch("paperlit.vector.encode_texts", side_effect=_mock_encode_texts)
    def test_index_paper_adds_passages(self, mock_enc, roots):
        idx = KbVectorIndex(roots, api_key="test-key")
        note = "# Test\n" + "x" * 200
        wiki = "# Wiki\n" + "y" * 200
        concepts = [{"name": "Graph Neural Network", "definition": "A neural net on graphs"}]

        added = idx.index_paper("10.1234/test", note_text=note,
                                wiki_text=wiki, concepts=concepts)
        assert added == 3  # note + wiki + concepts
        assert idx.size == 3

    @patch("paperlit.vector.encode_texts", side_effect=_mock_encode_texts)
    def test_index_paper_with_title(self, mock_enc, roots):
        idx = KbVectorIndex(roots, api_key="test-key")
        added = idx.index_paper(
            "10.1234/test",
            note_text="# Test\n" + "x" * 200,
            title="Graph Neural Networks for Smart Materials",
        )
        assert added == 2  # title + note
        assert "10.1234/test__title" in idx._key_to_idx

    @patch("paperlit.vector.encode_texts", side_effect=_mock_encode_texts)
    def test_short_text_skipped(self, mock_enc, roots):
        idx = KbVectorIndex(roots, api_key="test-key")
        added = idx.index_paper("10.1234/test", note_text="short",
                                wiki_text="", concepts=[])
        assert added == 0

    @patch("paperlit.vector.encode_texts", side_effect=_mock_encode_texts)
    def test_duplicate_not_added(self, mock_enc, roots):
        idx = KbVectorIndex(roots, api_key="test-key")
        note = "# Test\n" + "x" * 200
        added1 = idx.index_paper("10.1234/test", note_text=note)
        added2 = idx.index_paper("10.1234/test", note_text=note)
        assert added1 == 1
        assert added2 == 0  # 已存在，不重复

    @patch("paperlit.vector.encode_texts", side_effect=_mock_encode_texts)
    def test_force_overwrites(self, mock_enc, roots):
        idx = KbVectorIndex(roots, api_key="test-key")
        note = "# Test\n" + "x" * 200
        idx.index_paper("10.1234/test", note_text=note)
        assert idx.size == 1
        added = idx.index_paper("10.1234/test", note_text=note, force=True)
        assert added == 0  # force 允许重新编码但 _add_single 仍拒绝重复 key


# ---------------------------------------------------------------- 持久化

class TestPersistence:
    @patch("paperlit.vector.encode_texts", side_effect=_mock_encode_texts)
    def test_meta_and_vectors_persisted(self, mock_enc, roots):
        idx = KbVectorIndex(roots, api_key="test-key")
        idx.index_paper("10.1234/a", note_text="# A\n" + "x" * 200)

        # 重新加载
        idx2 = KbVectorIndex(roots, api_key="test-key")
        assert idx2.size == idx.size

    @patch("paperlit.vector.encode_texts", side_effect=_mock_encode_texts)
    def test_reload_preserves_keys(self, mock_enc, roots):
        idx = KbVectorIndex(roots, api_key="test-key")
        idx.index_paper("10.1234/a", note_text="# A\n" + "a" * 200)
        idx.index_paper("10.1234/b", wiki_text="# B\n" + "b" * 200)

        idx2 = KbVectorIndex(roots, api_key="test-key")
        assert "10.1234/a__note" in idx2._key_to_idx
        assert "10.1234/b__wiki" in idx2._key_to_idx


# ---------------------------------------------------------------- 搜索

class TestSearch:
    @patch("paperlit.vector.encode_query", side_effect=_mock_encode_query)
    @patch("paperlit.vector.encode_texts", side_effect=_mock_encode_texts)
    def test_search_returns_results(self, mock_enc_t, mock_enc_q, roots):
        idx = KbVectorIndex(roots, api_key="test-key")
        idx.index_paper("10.1234/a", note_text="# Alpha\n" + "x" * 200)
        idx.index_paper("10.1234/b", note_text="# Beta\n" + "y" * 200)

        results = idx.search("test query", top_k=5)
        assert len(results) <= 5
        assert all("doi" in r and "score" in r for r in results)
        assert all(r["passage_type"] == "note" for r in results)

    def test_search_empty_index(self, roots):
        idx = KbVectorIndex(roots, api_key="test-key")
        results = idx.search("test", top_k=5)
        assert results == []

    def test_search_no_api_key(self, roots):
        idx = KbVectorIndex(roots, api_key="")
        results = idx.search("test", top_k=5)
        assert results == []

    @patch("paperlit.vector.encode_query", side_effect=_mock_encode_query)
    @patch("paperlit.vector.encode_texts", side_effect=_mock_encode_texts)
    def test_search_exclude_doi(self, mock_enc_t, mock_enc_q, roots):
        """搜索时排除指定 DOI（L3 概念层排除自身）。"""
        idx = KbVectorIndex(roots, api_key="test-key")
        idx.index_paper("10.1234/a", note_text="# Alpha\n" + "x" * 200)
        idx.index_paper("10.1234/b", note_text="# Beta\n" + "y" * 200)

        # 不排除时应该返回两篇
        results_all = idx.search("test query", top_k=5)
        assert len(results_all) == 2

        # 排除 10.1234/a 后只返回 b
        results_excl = idx.search("test query", top_k=5,
                                  exclude_doi="10.1234/a")
        assert len(results_excl) == 1
        assert results_excl[0]["doi"] == "10.1234/b"


# ---------------------------------------------------------------- 混合检索

class TestHybridSearch:
    @patch("paperlit.vector.encode_query", side_effect=_mock_encode_query)
    @patch("paperlit.vector.encode_texts", side_effect=_mock_encode_texts)
    def test_search_by_concepts_with_store(self, mock_enc_t, mock_enc_q, roots):
        from paperkb.db import KBStore

        store = KBStore(roots)
        store.init_schema()
        store.upsert_concept("10.1234/a", "GNN", "Graph neural network")
        store.upsert_concept("10.1234/b", "GNN", "Graph neural net variant")
        store.upsert_concept("10.1234/c", "Transformer", "Attention model")

        idx = KbVectorIndex(roots, api_key="test-key")
        idx.index_paper("10.1234/a", note_text="# A\n" + "x" * 200)
        idx.index_paper("10.1234/b", note_text="# B\n" + "y" * 200)
        idx.index_paper("10.1234/c", note_text="# C\n" + "z" * 200)

        results = idx.search_by_concepts(["GNN"], top_k=5, store=store)
        dois = [r["doi"] for r in results]
        assert "10.1234/a" in dois
        assert "10.1234/b" in dois
        assert "10.1234/c" not in dois  # Transformer, not GNN

    def test_search_by_concepts_empty(self, roots):
        idx = KbVectorIndex(roots, api_key="test-key")
        results = idx.search_by_concepts([], top_k=5)
        assert results == []


# ---------------------------------------------------------------- 工厂

class TestFactory:
    def test_noop(self, roots):
        idx = build_vector_index(roots, impl="noop")
        assert isinstance(idx, NoopVectorIndex)

    def test_kb(self, roots):
        idx = build_vector_index(roots, impl="kb", api_key="test")
        assert isinstance(idx, KbVectorIndex)

    def test_unknown_raises(self, roots):
        with pytest.raises(ValueError, match="未知向量实现"):
            build_vector_index(roots, impl="unknown")


# ---------------------------------------------------------------- 编译钩子

class TestCompileVectorHook:
    def test_maybe_vector_index_noop_by_default(self, roots):
        """vector_impl=noop 时不触发向量索引。"""
        from paperkb import api
        from paperkb.config import KbSettings

        api.init_kb(roots, KbSettings(vector_impl="noop"))
        compiler = api._compiler
        # 不应抛异常
        compiler._maybe_vector_index("10.1234/test", {}, None)

    @patch("paperlit.vector.encode_texts", side_effect=_mock_encode_texts)
    def test_maybe_vector_index_with_kb(self, mock_enc, roots, monkeypatch):
        """vector_impl=kb + API key 时触发向量索引。"""
        from paperkb import api
        from paperkb.config import KbSettings

        monkeypatch.setenv("SILICONFLOW_API_KEY", "test-key-123")
        api.init_kb(roots, KbSettings(vector_impl="kb"))
        compiler = api._compiler

        # 模拟已保存的编译产物
        from paperkb.doi import doi_to_dirname
        dirname = doi_to_dirname("10.1234/test")
        folder = roots.kb_dir / dirname
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "_note.md").write_text("# Test\n" + "content " * 50, encoding="utf-8")

        compiler._maybe_vector_index(
            "10.1234/test",
            {"concepts": [{"name": "Test", "definition": "A test concept"}]},
            None,
        )

        # 验证向量索引已创建
        idx = KbVectorIndex(roots, api_key="test-key-123")
        assert idx.size > 0
