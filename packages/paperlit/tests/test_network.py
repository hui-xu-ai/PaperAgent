# -*- coding: utf-8 -*-
"""paperlit 文献计量图谱测试：network 导出 / 过滤 / 分面 / 邻居 / 详情。"""
from __future__ import annotations

import pytest

from paperlit.config import Roots
from paperlit.db import LitStore
from paperlit.models import Paper, Citation
from paperlit.graph.network import (
    build_network, get_filter_facets, get_neighbors, get_node_detail,
)


@pytest.fixture
def lit_store(tmp_path):
    roots = Roots(data_dir=tmp_path / "data", lit_dir=tmp_path / "lit")
    roots.ensure()
    store = LitStore(roots)
    store.init_schema()
    return store


def _add(store, doi, **kw):
    # impact_factor/quartile/library_citations 由清洗模块直接 UPDATE 写入
    # （upsert_paper 不持久化这些列），测试模拟同一生产路径。
    metrics = {k: kw.pop(k) for k in
               ("impact_factor", "quartile", "library_citations") if k in kw}
    store.upsert_paper(Paper(doi=doi, title=kw.pop("title", f"Paper {doi}"), **kw))
    if metrics:
        sets = ", ".join(f"{k}=?" for k in metrics)
        with store._conn() as conn:
            conn.execute(f"UPDATE papers SET {sets} WHERE doi=?",
                         (*metrics.values(), doi))


def _cite(store, citing, cited):
    store.upsert_citation(Citation(citing_doi=citing, cited_doi=cited))


@pytest.fixture
def small_graph(lit_store):
    """A(2020,Q1,IF10,库内被引2) ← B,C 引用；D(2018,Q3,IF2) 孤立。"""
    _add(lit_store, "A", year="2020", quartile="Q1", impact_factor=10.0,
         times_cited=50, library_citations=2, abstract="abs A",
         keywords=["k1", "k2"], authors=["X", "Y"])
    _add(lit_store, "B", year="2021", quartile="Q2", impact_factor=5.0,
         times_cited=10, library_citations=0)
    _add(lit_store, "C", year="2022", quartile="Q1", impact_factor=8.0,
         times_cited=5, library_citations=0)
    _add(lit_store, "D", year="2018", quartile="Q3", impact_factor=2.0,
         times_cited=1, library_citations=0)
    _cite(lit_store, "B", "A")
    _cite(lit_store, "C", "A")
    return lit_store


class TestBuildNetwork:
    def test_empty(self, lit_store):
        net = build_network(lit_store)
        assert net["nodes"] == [] and net["edges"] == []
        assert net["meta"]["matched_nodes"] == 0

    def test_nodes_and_edges(self, small_graph):
        # exclude_isolated 默认 True（2026-09-20 加孤立节点过滤）；本例要断言"节点+边都建出来"，
        # 含孤立的 D，故显式关掉过滤。
        net = build_network(small_graph, exclude_isolated=False)
        ids = {n["id"] for n in net["nodes"]}
        assert ids == {"A", "B", "C", "D"}
        # B→A, C→A 两条边
        assert net["meta"]["returned_edges"] == 2
        assert {"source": "B", "target": "A"} in net["edges"]

    def test_exclude_isolated_default(self, small_graph):
        """默认过滤孤立节点（度数为 0）：D 无任何引用关系 ⇒ 默认不出现，关掉才出现。"""
        assert {n["id"] for n in build_network(small_graph)["nodes"]} == {"A", "B", "C"}
        assert {n["id"] for n in
                build_network(small_graph, exclude_isolated=False)["nodes"]} == {"A", "B", "C", "D"}

    def test_node_attributes(self, small_graph):
        net = build_network(small_graph)
        a = next(n for n in net["nodes"] if n["id"] == "A")
        assert a["year"] == 2020
        assert a["impact_factor"] == 10.0
        assert a["quartile"] == "Q1"
        assert a["library_citations"] == 2
        assert a["in_kb"] is False

    def test_in_kb_marking(self, small_graph):
        net = build_network(small_graph, kb_dois={"A", "C"})
        by_id = {n["id"]: n for n in net["nodes"]}
        assert by_id["A"]["in_kb"] is True
        assert by_id["B"]["in_kb"] is False

    def test_filter_year(self, small_graph):
        net = build_network(small_graph, year_min=2020)
        assert {n["id"] for n in net["nodes"]} == {"A", "B", "C"}

    def test_filter_impact_factor(self, small_graph):
        net = build_network(small_graph, min_impact_factor=5.0)
        assert {n["id"] for n in net["nodes"]} == {"A", "B", "C"}

    def test_filter_quartile(self, small_graph):
        net = build_network(small_graph, quartiles=["Q1"])
        assert {n["id"] for n in net["nodes"]} == {"A", "C"}

    def test_filter_library_citations(self, small_graph):
        # 只剩 A 时它自己就没有边了（B/C 被滤掉）⇒ 必须关掉孤立过滤，否则会被当孤立节点再滤一次、结果空集。
        net = build_network(small_graph, min_library_citations=1, exclude_isolated=False)
        assert {n["id"] for n in net["nodes"]} == {"A"}
        # 节点被截断后，边也随之收敛（B/C 不在集合内）
        assert net["edges"] == []

    def test_in_kb_only(self, small_graph):
        net = build_network(small_graph, in_kb_only=True, kb_dois={"A", "B"})
        assert {n["id"] for n in net["nodes"]} == {"A", "B"}

    def test_limit_and_truncation(self, small_graph):
        net = build_network(small_graph, limit=2, sort_by="library_citations")
        assert net["meta"]["returned_nodes"] == 2
        assert net["meta"]["matched_nodes"] == 4
        assert net["meta"]["truncated"] is True
        # A（库内被引2）必入选
        assert "A" in {n["id"] for n in net["nodes"]}

    def test_sort_by_times_cited(self, small_graph):
        # 只取 1 个节点时它没有边（引用方 B/C 未入选）⇒ 关掉孤立过滤才能验排序。
        net = build_network(small_graph, limit=1, sort_by="times_cited", exclude_isolated=False)
        assert net["nodes"][0]["id"] == "A"  # times_cited=50 最高

    def test_edges_only_between_selected(self, small_graph):
        net = build_network(small_graph, quartiles=["Q1"])  # A,C
        # B→A 被排除（B 不在集合），仅剩 C→A
        assert net["edges"] == [{"source": "C", "target": "A"}]


class TestFilterFacets:
    def test_facets(self, small_graph):
        f = get_filter_facets(small_graph, kb_dois={"A"})
        assert f["total_papers"] == 4
        assert f["year"]["min"] == 2018 and f["year"]["max"] == 2022
        assert f["impact_factor"]["max"] == 10.0
        assert f["library_citations"]["max"] == 2
        assert f["quartiles"]["Q1"] == 2
        assert f["in_kb_count"] == 1

    def test_empty(self, lit_store):
        f = get_filter_facets(lit_store)
        assert f["total_papers"] == 0
        assert f["year"]["min"] is None


class TestNeighbors:
    def test_neighbors(self, small_graph):
        nb = get_neighbors(small_graph, "A")
        assert set(nb["citing"]) == {"B", "C"}  # 引用 A 的
        assert nb["cited"] == []               # A 引用的

    def test_neighbors_reverse(self, small_graph):
        nb = get_neighbors(small_graph, "B")
        assert nb["citing"] == []
        assert nb["cited"] == ["A"]


class TestNodeDetail:
    def test_detail(self, small_graph):
        d = get_node_detail(small_graph, "A")
        assert d["title"] == "Paper A"
        assert d["abstract"] == "abs A"
        assert d["keywords"] == ["k1", "k2"]
        assert d["authors"] == ["X", "Y"]
        assert d["in_degree"] == 2 and d["out_degree"] == 0

    def test_missing(self, small_graph):
        assert get_node_detail(small_graph, "NOPE") is None
