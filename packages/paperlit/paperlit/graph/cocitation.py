# -*- coding: utf-8 -*-
"""共被引聚类：基于两篇文献被共同引用的频率进行社区发现。

共被引强度(A,B) = |{C : C→A 且 C→B}| / min(in_degree(A), in_degree(B))
使用 Louvain 算法进行社区划分（igraph 可选依赖）。
"""
from __future__ import annotations

import logging
from collections import defaultdict

from ..db import LitStore

logger = logging.getLogger(__name__)


def _build_cocitation_matrix(store: LitStore, min_cocitations: int = 2
                             ) -> tuple[list[str], dict[tuple[int, int], int]]:
    """构建共被引矩阵（只保留共被引次数 >= min_cocitations 的边）。

    Returns:
        (dois, edges): DOI 列表 + {(i,j): weight} 边字典
    """
    with store._conn() as conn:
        rows = conn.execute("""
            SELECT citing_doi, cited_doi FROM citations
        """).fetchall()

    cited_by: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        cited_by[row["cited_doi"]].add(row["citing_doi"])

    dois = sorted(cited_by.keys())
    doi_to_idx = {doi: i for i, doi in enumerate(dois)}
    n = len(dois)

    cocitation: dict[tuple[int, int], int] = defaultdict(int)

    for citing_dois in cited_by.values():
        citing_list = sorted(doi for doi in citing_dois if doi in doi_to_idx)
        for i_idx in range(len(citing_list)):
            for j_idx in range(i_idx + 1, len(citing_list)):
                i = doi_to_idx[citing_list[i_idx]]
                j = doi_to_idx[citing_list[j_idx]]
                if i > j:
                    i, j = j, i
                cocitation[(i, j)] += 1

    filtered = {k: v for k, v in cocitation.items() if v >= min_cocitations}
    logger.info("cocitation matrix: %d nodes, %d edges (min_cocitations=%d)",
                n, len(filtered), min_cocitations)
    return dois, filtered


def compute_cocitation_clusters(store: LitStore, min_cocitations: int = 2
                                ) -> dict:
    """计算共被引聚类。

    Args:
        store: LitStore 实例
        min_cocitations: 最小共被引次数（过滤噪声边）

    Returns:
        {"papers": int, "clusters": int, "method": str}
    """
    dois, edges = _build_cocitation_matrix(store, min_cocitations)

    if len(dois) < 2 or not edges:
        logger.info("cocitation: not enough data for clustering")
        return {"papers": len(dois), "clusters": 0, "method": "none"}

    try:
        import igraph as ig
        cluster_ids = _cluster_with_igraph(dois, edges)
        method = "louvain"
    except ImportError:
        cluster_ids = _cluster_fallback(dois, edges)
        method = "connected_components"

    with store._conn() as conn:
        for i, doi in enumerate(dois):
            conn.execute(
                "UPDATE papers SET cocitation_cluster=? WHERE doi=?",
                (cluster_ids[i], doi)
            )

    n_clusters = len(set(cluster_ids))
    logger.info("cocitation clustering: %d papers → %d clusters (%s)",
                len(dois), n_clusters, method)
    return {"papers": len(dois), "clusters": n_clusters, "method": method}


def _cluster_with_igraph(dois: list[str],
                         edges: dict[tuple[int, int], int]) -> list[int]:
    """使用 igraph Louvain 聚类。"""
    import igraph as ig

    g = ig.Graph(n=len(dois), edges=list(edges.keys()), directed=False)
    g.es["weight"] = list(edges.values())

    clustering = g.community_multilevel(weights="weight")
    return clustering.membership


def _cluster_fallback(dois: list[str],
                      edges: dict[tuple[int, int], int]) -> list[int]:
    """无 igraph 时的降级方案：连通分量作为聚类。"""
    n = len(dois)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for (i, j) in edges:
        union(i, j)

    return [find(i) for i in range(n)]


def get_cluster_papers(store: LitStore, cluster_id: int) -> list[dict]:
    """获取指定聚类的所有文献。"""
    with store._conn() as conn:
        rows = conn.execute("""
            SELECT doi, title, year, journal, times_cited, paper_rank
            FROM papers
            WHERE cocitation_cluster = ? AND is_reference = 0
            ORDER BY paper_rank DESC
        """, (cluster_id,)).fetchall()
    return [dict(r) for r in rows]
