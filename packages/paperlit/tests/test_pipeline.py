# -*- coding: utf-8 -*-
"""paperlit P5 测试：三层漏斗管线（mock embedding + reranker）。"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import numpy as np
import pytest

from paperlit.config import Roots, LitSettings
from paperlit.db import LitStore
from paperlit.models import Paper
from paperlit.retrieve.pipeline import search_funnel, _build_passage, _l3_deliver


@pytest.fixture
def lit_store(tmp_path):
    roots = Roots(data_dir=tmp_path / "data", lit_dir=tmp_path / "lit")
    roots.ensure()
    store = LitStore(roots)
    store.init_schema()
    return store


@pytest.fixture
def settings():
    return LitSettings(
        top_k_coarse=10,
        top_k_rerank=5,
        top_k_deliver=3,
    )


def _make_paper(doi: str, title: str, **kw) -> Paper:
    return Paper(doi=doi, title=title, abstract=kw.get("abstract", ""),
                 authors=kw.get("authors", []), journal=kw.get("journal", ""),
                 year=kw.get("year", ""), times_cited=kw.get("times_cited", 0),
                 paper_rank=kw.get("paper_rank", 0.0))


def _seed_papers(store: LitStore):
    papers = [
        _make_paper("10.1/a", "Solid electrolyte for lithium battery",
                     times_cited=100, paper_rank=0.5),
        _make_paper("10.1/b", "Polymer membrane fuel cell",
                     times_cited=50, paper_rank=0.3),
        _make_paper("10.1/c", "Cooking recipes with lithium",
                     times_cited=5, paper_rank=0.01),
        _make_paper("10.1/d", "Solid state battery cathode",
                     times_cited=200, paper_rank=0.8),
    ]
    for p in papers:
        store.upsert_paper(p)


class TestBuildPassage:
    def test_combines_fields(self):
        d = {"title": "Battery", "abstract": "A solid electrolyte",
             "authors": ["Alice", "Bob"], "journal": "Nature", "year": "2024"}
        passage = _build_passage(d)
        assert "Battery" in passage
        assert "Alice" in passage
        assert "Nature" in passage

    def test_truncates_abstract(self):
        d = {"title": "T", "abstract": "x" * 1000}
        passage = _build_passage(d)
        assert len(passage) < 2000


class TestSearchFunnel:
    def test_empty_query(self, lit_store, settings):
        result = search_funnel("", lit_store, None, settings)
        assert result == []

    def test_no_candidates(self, lit_store, settings):
        result = search_funnel("nonexistent topic", lit_store, None, settings)
        assert result == []

    def test_keyword_only_no_vector(self, lit_store, settings):
        _seed_papers(lit_store)
        with patch("paperlit.retrieve.pipeline.rerank") as mock_rerank:
            mock_rerank.return_value = [
                (0, 0.9), (1, 0.5), (2, 0.1),
            ]
            result = search_funnel("battery", lit_store, None, settings,
                                   reranker_api_key="fake-key")
            assert len(result) <= settings.top_k_deliver
            assert all(r.doi for r in result)

    def test_reranker_called(self, lit_store, settings):
        _seed_papers(lit_store)
        with patch("paperlit.retrieve.pipeline.rerank") as mock_rerank:
            mock_rerank.return_value = [(0, 0.8)]
            search_funnel("electrolyte", lit_store, None, settings,
                          reranker_api_key="fake-key")
            mock_rerank.assert_called_once()

    def test_no_api_key_skips_reranker(self, lit_store, settings):
        _seed_papers(lit_store)
        with patch("paperlit.retrieve.pipeline.rerank") as mock_rerank:
            result = search_funnel("battery", lit_store, None, settings,
                                   reranker_api_key="")
            mock_rerank.assert_not_called()
            assert len(result) > 0

    def test_results_sorted_by_final_score(self, lit_store, settings):
        _seed_papers(lit_store)
        with patch("paperlit.retrieve.pipeline.rerank") as mock_rerank:
            mock_rerank.return_value = [
                (0, 0.9), (1, 0.7), (2, 0.3), (3, 0.1),
            ]
            result = search_funnel("battery", lit_store, None, settings,
                                   reranker_api_key="fake-key")
            scores = [r.final_score for r in result]
            assert scores == sorted(scores, reverse=True)

    def test_top_k_deliver_respected(self, lit_store, settings):
        _seed_papers(lit_store)
        with patch("paperlit.retrieve.pipeline.rerank") as mock_rerank:
            mock_rerank.return_value = [
                (i, 0.9 - i * 0.1) for i in range(4)
            ]
            result = search_funnel("battery", lit_store, None, settings,
                                   reranker_api_key="fake-key")
            assert len(result) <= settings.top_k_deliver

    def test_match_source_is_funnel(self, lit_store, settings):
        _seed_papers(lit_store)
        with patch("paperlit.retrieve.pipeline.rerank") as mock_rerank:
            mock_rerank.return_value = [(0, 0.8)]
            result = search_funnel("battery", lit_store, None, settings,
                                   reranker_api_key="fake-key")
            assert all(r.match_source == "funnel" for r in result)


class TestL3Deliver:
    def test_final_score_combines_signals(self):
        settings = LitSettings(top_k_deliver=10)
        candidates = {
            "a": {"title": "A", "times_cited": 100, "paper_rank": 0.8},
            "b": {"title": "B", "times_cited": 10, "paper_rank": 0.1},
        }
        ranked = [("a", 0.9), ("b", 0.5)]
        results = _l3_deliver(ranked, candidates, settings)
        assert results[0].doi == "a"
        assert results[0].final_score > results[1].final_score

    def test_deliver_truncates(self):
        settings = LitSettings(top_k_deliver=2)
        candidates = {
            f"d{i}": {"title": f"T{i}", "times_cited": i, "paper_rank": 0.1}
            for i in range(5)
        }
        ranked = [(f"d{i}", 0.5) for i in range(5)]
        results = _l3_deliver(ranked, candidates, settings)
        assert len(results) == 2
