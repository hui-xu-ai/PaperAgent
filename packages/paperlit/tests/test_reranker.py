# -*- coding: utf-8 -*-
"""paperlit P5 测试：Reranker 客户端（mock API）。"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from paperlit.vector.reranker import rerank


class TestReranker:
    def test_requires_api_key(self):
        with pytest.raises(ValueError, match="API key"):
            rerank("query", ["doc1"], api_key="")

    def test_empty_query(self):
        result = rerank("", ["doc1"], api_key="fake-key")
        assert result == []

    def test_empty_documents(self):
        result = rerank("query", [], api_key="fake-key")
        assert result == []

    def test_all_empty_documents(self):
        result = rerank("query", ["", "  ", ""], api_key="fake-key")
        assert result == []

    def test_rerank_basic(self):
        with patch("paperlit.vector.reranker._post_json") as mock_post:
            mock_post.return_value = {
                "results": [
                    {"index": 0, "relevance_score": 0.9},
                    {"index": 1, "relevance_score": 0.7},
                    {"index": 2, "relevance_score": 0.3},
                ]
            }
            result = rerank("battery", ["solid electrolyte",
                                         "polymer film", "cooking recipe"],
                            api_key="fake-key")
            assert len(result) == 3
            assert result[0] == (0, 0.9)
            assert result[1] == (1, 0.7)
            assert result[2] == (2, 0.3)

    def test_rerank_sorted_by_score(self):
        with patch("paperlit.vector.reranker._post_json") as mock_post:
            mock_post.return_value = {
                "results": [
                    {"index": 0, "relevance_score": 0.2},
                    {"index": 1, "relevance_score": 0.95},
                    {"index": 2, "relevance_score": 0.5},
                ]
            }
            result = rerank("q", ["a", "b", "c"], api_key="fake-key")
            assert result[0][0] == 1
            assert result[0][1] == 0.95

    def test_rerank_top_n(self):
        with patch("paperlit.vector.reranker._post_json") as mock_post:
            mock_post.return_value = {
                "results": [
                    {"index": 0, "relevance_score": 0.9},
                    {"index": 1, "relevance_score": 0.7},
                ]
            }
            result = rerank("q", ["a", "b", "c"], api_key="fake-key",
                            top_n=1)
            assert len(result) == 1

    def test_rerank_batch_split(self):
        with patch("paperlit.vector.reranker._post_json") as mock_post:
            def side_effect(url, body, api_key, timeout=60):
                docs = body["documents"]
                return {
                    "results": [
                        {"index": i, "relevance_score": 0.5 + i * 0.1}
                        for i in range(len(docs))
                    ]
                }
            mock_post.side_effect = side_effect

            docs = [f"doc_{i}" for i in range(5)]
            result = rerank("query", docs, api_key="fake-key",
                            batch_size=2)
            assert mock_post.call_count == 3
            assert len(result) == 5

    def test_rerank_api_failure_returns_empty(self):
        with patch("paperlit.vector.reranker._post_json") as mock_post:
            mock_post.return_value = None
            result = rerank("query", ["doc1"], api_key="fake-key")
            assert result == []

    def test_rerank_preserves_original_indices(self):
        with patch("paperlit.vector.reranker._post_json") as mock_post:
            mock_post.return_value = {
                "results": [
                    {"index": 0, "relevance_score": 0.3},
                    {"index": 1, "relevance_score": 0.8},
                ]
            }
            docs = ["a", "", "c"]
            result = rerank("q", docs, api_key="fake-key")
            orig_indices = [r[0] for r in result]
            assert 0 in orig_indices
            assert 2 in orig_indices
