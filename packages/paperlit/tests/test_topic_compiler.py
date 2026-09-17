# -*- coding: utf-8 -*-
"""paperlit P7 测试：主题编译。"""
from __future__ import annotations

import json

import pytest

from paperlit.config import Roots
from paperlit.db import LitStore
from paperlit.models import Paper
from paperlit.compile.topic_compiler import (
    compile_topics, list_topics, get_topic, _slugify,
)


@pytest.fixture
def lit_store(tmp_path):
    roots = Roots(data_dir=tmp_path / "data", lit_dir=tmp_path / "lit")
    roots.ensure()
    store = LitStore(roots)
    store.init_schema()
    return store


def _seed_papers(store: LitStore):
    papers = [
        Paper(doi="10.1/a", title="Battery A",
              wos_categories=["Electrochemistry", "Energy"],
              times_cited=100, paper_rank=0.5),
        Paper(doi="10.1/b", title="Battery B",
              wos_categories=["Electrochemistry"],
              times_cited=50, paper_rank=0.3),
        Paper(doi="10.1/c", title="Solar C",
              wos_categories=["Photovoltaics", "Energy"],
              times_cited=30, paper_rank=0.2),
        Paper(doi="10.1/d", title="Catalysis D",
              wos_categories=["Catalysis"],
              times_cited=10, paper_rank=0.05),
    ]
    for p in papers:
        store.upsert_paper(p)


class TestSlugify:
    def test_basic(self):
        assert _slugify("Electrochemistry") == "electrochemistry"

    def test_spaces(self):
        assert _slugify("Physical Chemistry") == "physical-chemistry"

    def test_special_chars(self):
        assert _slugify("Energy & Fuels") == "energy-fuels"

    def test_chinese(self):
        assert _slugify("电化学") == "电化学"

    def test_truncation(self):
        long_name = "a" * 100
        assert len(_slugify(long_name)) <= 80


class TestCompileTopics:
    def test_basic_compile(self, lit_store):
        _seed_papers(lit_store)
        result = compile_topics(lit_store, min_papers=1)
        assert result["topics"] > 0
        assert result["papers_covered"] > 0
        assert result["field"] == "wos_categories"

    def test_min_papers_filter(self, lit_store):
        _seed_papers(lit_store)
        result_min1 = compile_topics(lit_store, min_papers=1)
        result_min3 = compile_topics(lit_store, min_papers=3)
        assert result_min3["topics"] <= result_min1["topics"]

    def test_writes_to_db(self, lit_store):
        _seed_papers(lit_store)
        compile_topics(lit_store, min_papers=1)
        with lit_store._conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM topic_cache"
            ).fetchone()[0]
        assert count > 0

    def test_invalid_field(self, lit_store):
        with pytest.raises(ValueError, match="field"):
            compile_topics(lit_store, field="invalid")

    def test_recompile_updates(self, lit_store):
        _seed_papers(lit_store)
        r1 = compile_topics(lit_store, min_papers=1)
        r2 = compile_topics(lit_store, min_papers=1)
        assert r1["topics"] == r2["topics"]

    def test_empty_db(self, lit_store):
        result = compile_topics(lit_store, min_papers=1)
        assert result["topics"] == 0


class TestListTopics:
    def test_list_sorted_by_paper_count(self, lit_store):
        _seed_papers(lit_store)
        compile_topics(lit_store, min_papers=1)
        topics = list_topics(lit_store)
        assert len(topics) > 0
        counts = [t["paper_count"] for t in topics]
        assert counts == sorted(counts, reverse=True)

    def test_list_fields(self, lit_store):
        _seed_papers(lit_store)
        compile_topics(lit_store, min_papers=1)
        topics = list_topics(lit_store)
        t = topics[0]
        assert "slug" in t
        assert "name" in t
        assert "summary" in t
        assert "paper_count" in t

    def test_pagination(self, lit_store):
        _seed_papers(lit_store)
        compile_topics(lit_store, min_papers=1)
        all_topics = list_topics(lit_store)
        page1 = list_topics(lit_store, limit=2, offset=0)
        page2 = list_topics(lit_store, limit=2, offset=2)
        assert len(page1) <= 2
        if len(all_topics) > 2:
            assert page1[0]["slug"] != page2[0]["slug"]


class TestGetTopic:
    def test_get_existing(self, lit_store):
        _seed_papers(lit_store)
        compile_topics(lit_store, min_papers=1)
        topics = list_topics(lit_store)
        slug = topics[0]["slug"]

        topic = get_topic(lit_store, slug)
        assert topic is not None
        assert topic["slug"] == slug
        assert topic["paper_count"] > 0
        assert len(topic["paper_dois"]) == topic["paper_count"]

    def test_get_nonexistent(self, lit_store):
        assert get_topic(lit_store, "nonexistent-slug") is None

    def test_topic_contains_dois(self, lit_store):
        _seed_papers(lit_store)
        compile_topics(lit_store, min_papers=1)
        topics = list_topics(lit_store)

        for t in topics:
            topic = get_topic(lit_store, t["slug"])
            assert isinstance(topic["paper_dois"], list)
            assert all(isinstance(d, str) for d in topic["paper_dois"])
