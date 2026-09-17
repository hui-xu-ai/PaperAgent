# -*- coding: utf-8 -*-
"""PaperRank：基于引用图谱的文献重要性评分（类 PageRank）。

算法：PR(p) = (1-d)/N + d * Σ(PR(q)/L(q))，其中 q→p 表示 q 引用了 p，L(q) 是 q 的引用数。
阻尼系数 d=0.85（学术引用常用值），迭代至收敛（Δ < 1e-6）或达到最大轮次。
"""
from __future__ import annotations

import logging
from datetime import datetime

from ..db import LitStore

logger = logging.getLogger(__name__)

_DAMPING = 0.85
_MAX_ITER = 100
_TOLERANCE = 1e-6


def compute_paper_rank(store: LitStore, damping: float = _DAMPING,
                       max_iter: int = _MAX_ITER,
                       tolerance: float = _TOLERANCE) -> dict:
    """计算所有文献的 PaperRank 值。

    Args:
        store: LitStore 实例
        damping: 阻尼系数（默认 0.85）
        max_iter: 最大迭代轮次
        tolerance: 收敛阈值（L1 范数）

    Returns:
        {"computed": int, "iterations": int, "converged": bool}
    """
    with store._conn() as conn:
        all_dois = [r["doi"] for r in conn.execute(
            "SELECT doi FROM papers"
        ).fetchall()]

        if not all_dois:
            return {"computed": 0, "iterations": 0, "converged": True}

        doi_to_idx = {doi: i for i, doi in enumerate(all_dois)}
        n = len(all_dois)

        out_degree = [0] * n
        in_edges: list[list[int]] = [[] for _ in range(n)]

        rows = conn.execute(
            "SELECT citing_doi, cited_doi FROM citations"
        ).fetchall()

        for row in rows:
            citing = row["citing_doi"]
            cited = row["cited_doi"]
            if citing in doi_to_idx and cited in doi_to_idx:
                i = doi_to_idx[citing]
                j = doi_to_idx[cited]
                out_degree[i] += 1
                in_edges[j].append(i)

    ranks = [1.0 / n] * n

    iterations = 0
    converged = False

    for iteration in range(max_iter):
        iterations += 1
        new_ranks = [(1.0 - damping) / n] * n

        for j in range(n):
            for i in in_edges[j]:
                if out_degree[i] > 0:
                    new_ranks[j] += damping * ranks[i] / out_degree[i]

            dangling_sum = sum(
                ranks[i] for i in range(n) if out_degree[i] == 0
            )
            new_ranks[j] += damping * dangling_sum / n

        diff = sum(abs(new_ranks[i] - ranks[i]) for i in range(n))
        ranks = new_ranks

        if diff < tolerance:
            converged = True
            logger.info("PaperRank converged at iteration %d (Δ=%.2e)",
                        iteration + 1, diff)
            break

    now = datetime.now().isoformat(timespec="seconds")

    with store._conn() as conn:
        for i, doi in enumerate(all_dois):
            conn.execute(
                "UPDATE papers SET paper_rank=? WHERE doi=?",
                (ranks[i], doi)
            )
            conn.execute("""
                INSERT OR REPLACE INTO paper_ranks
                (doi, paper_rank, in_degree, out_degree, computed_at)
                VALUES (?, ?, ?, ?, ?)
            """, (doi, ranks[i], len(in_edges[i]), out_degree[i], now))

    logger.info("PaperRank computed: %d papers, %d iterations, converged=%s",
                n, iterations, converged)
    return {"computed": n, "iterations": iterations, "converged": converged}


def get_top_papers(store: LitStore, limit: int = 20) -> list[dict]:
    """获取 PaperRank 最高的文献列表。"""
    with store._conn() as conn:
        rows = conn.execute("""
            SELECT p.doi, p.title, p.year, p.journal, p.times_cited,
                   p.paper_rank, pr.in_degree, pr.out_degree
            FROM papers p
            LEFT JOIN paper_ranks pr ON p.doi = pr.doi
            WHERE p.is_reference = 0
            ORDER BY p.paper_rank DESC
            LIMIT ?
        """, (limit,)).fetchall()

    return [dict(r) for r in rows]
