# -*- coding: utf-8 -*-
"""paperlit P3 测试：PaperRank + 共被引聚类。"""
from __future__ import annotations

import pytest

from paperlit.config import Roots
from paperlit.db import LitStore
from paperlit.models import Paper, Citation
from paperlit.graph import (
    compute_paper_rank, get_top_papers,
    compute_cocitation_clusters, get_cluster_papers,
)


@pytest.fixture
def lit_store(tmp_path):
    roots = Roots(data_dir=tmp_path / "data", lit_dir=tmp_path / "lit")
    roots.ensure()
    store = LitStore(roots)
    store.init_schema()
    return store


def _add_paper(store: LitStore, doi: str, title: str = "") -> None:
    store.upsert_paper(Paper(doi=doi, title=title or f"Paper {doi}"))


def _add_citation(store: LitStore, citing: str, cited: str) -> None:
    store.upsert_citation(Citation(citing_doi=citing, cited_doi=cited))


class TestPaperRank:
    def test_empty_graph(self, lit_store):
        result = compute_paper_rank(lit_store)
        assert result["computed"] == 0
        assert result["converged"] is True

    def test_single_paper(self, lit_store):
        _add_paper(lit_store, "10.1002/a")
        result = compute_paper_rank(lit_store)
        assert result["computed"] == 1
        paper = lit_store.get_paper("10.1002/a")
        assert paper.paper_rank > 0

    def test_linear_chain(self, lit_store):
        """A → B → C：C 被引用最多，rank 最高。"""
        for doi in ["A", "B", "C"]:
            _add_paper(lit_store, doi)
        _add_citation(lit_store, "A", "B")
        _add_citation(lit_store, "B", "C")

        compute_paper_rank(lit_store)

        rank_a = lit_store.get_paper("A").paper_rank
        rank_b = lit_store.get_paper("B").paper_rank
        rank_c = lit_store.get_paper("C").paper_rank

        assert rank_c > rank_b > rank_a

    def test_star_topology(self, lit_store):
        """A→H, B→H, C→H, D→H：H 被 4 篇引用，rank 最高。"""
        for doi in ["A", "B", "C", "D", "H"]:
            _add_paper(lit_store, doi)
        for src in ["A", "B", "C", "D"]:
            _add_citation(lit_store, src, "H")

        compute_paper_rank(lit_store)

        rank_h = lit_store.get_paper("H").paper_rank
        rank_a = lit_store.get_paper("A").paper_rank
        assert rank_h > rank_a * 2

    def test_convergence(self, lit_store):
        """复杂图应能收敛。"""
        for i in range(10):
            _add_paper(lit_store, f"10.1002/p{i}")
        for i in range(10):
            for j in range(i + 1, 10):
                _add_citation(lit_store, f"10.1002/p{i}", f"10.1002/p{j}")

        result = compute_paper_rank(lit_store)
        assert result["converged"] is True
        assert result["iterations"] < 100

    def test_get_top_papers(self, lit_store):
        for doi in ["A", "B", "C"]:
            _add_paper(lit_store, doi)
        _add_citation(lit_store, "A", "B")
        _add_citation(lit_store, "A", "C")
        _add_citation(lit_store, "B", "C")

        compute_paper_rank(lit_store)
        top = get_top_papers(lit_store, limit=3)

        assert len(top) == 3
        assert top[0]["doi"] == "C"

    def test_paper_rank_stored_in_db(self, lit_store):
        _add_paper(lit_store, "10.1002/x")
        _add_paper(lit_store, "10.1002/y")
        _add_citation(lit_store, "10.1002/x", "10.1002/y")

        compute_paper_rank(lit_store)

        with lit_store._conn() as conn:
            row = conn.execute(
                "SELECT * FROM paper_ranks WHERE doi='10.1002/y'"
            ).fetchone()

        assert row is not None
        assert row["paper_rank"] > 0
        assert row["in_degree"] == 1
        assert row["out_degree"] == 0


class TestCocitationClusters:
    def test_empty_graph(self, lit_store):
        result = compute_cocitation_clusters(lit_store)
        assert result["papers"] == 0
        assert result["clusters"] == 0

    def test_two_separate_clusters(self, lit_store):
        """两篇文献被不同的施引文献引用 → 两个聚类。"""
        for doi in ["A", "B", "C1", "C2", "C3"]:
            _add_paper(lit_store, doi)

        _add_citation(lit_store, "C1", "A")
        _add_citation(lit_store, "C2", "A")
        _add_citation(lit_store, "C1", "B")
        _add_citation(lit_store, "C2", "B")

        _add_citation(lit_store, "C3", "A")

        result = compute_cocitation_clusters(lit_store, min_cocitations=2)

        cluster_a = lit_store.get_paper("A").cocitation_cluster
        cluster_b = lit_store.get_paper("B").cocitation_cluster
        assert cluster_a == cluster_b

    def test_get_cluster_papers(self, lit_store):
        for doi in ["A", "B", "C"]:
            _add_paper(lit_store, doi)
        _add_citation(lit_store, "C", "A")
        _add_citation(lit_store, "C", "B")

        compute_cocitation_clusters(lit_store, min_cocitations=1)

        cluster_a = lit_store.get_paper("A").cocitation_cluster
        papers = get_cluster_papers(lit_store, cluster_a)
        assert len(papers) >= 1

    def test_min_cocitations_filter(self, lit_store):
        """共被引次数 < min_cocitations 的边应被过滤。"""
        for doi in ["A", "B", "C"]:
            _add_paper(lit_store, doi)
        _add_citation(lit_store, "C", "A")
        _add_citation(lit_store, "C", "B")

        result = compute_cocitation_clusters(lit_store, min_cocitations=5)
        assert result["clusters"] == 0

    def test_cluster_written_to_db(self, lit_store):
        for doi in ["X", "Y"]:
            _add_paper(lit_store, doi)
        _add_citation(lit_store, "Z", "X")
        _add_citation(lit_store, "Z", "Y")

        compute_cocitation_clusters(lit_store, min_cocitations=1)

        paper_x = lit_store.get_paper("X")
        paper_y = lit_store.get_paper("Y")
        assert paper_x.cocitation_cluster == paper_y.cocitation_cluster
