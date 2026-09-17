# -*- coding: utf-8 -*-
"""三层漏斗检索管线。

L1 粗筛：向量召回（top_k_coarse）+ 关键词 FTS 召回 → 去重合并
L2 精排：bge-reranker 打分 → 取 top_k_rerank
L3 交付：综合排序（reranker_score * w_r + paper_rank * w_p + cited * w_c）→ top_k_deliver
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..models import SearchResult
from ..vector.embeddings import encode_query
from ..vector.reranker import rerank

if TYPE_CHECKING:
    from ..config import LitSettings
    from ..db import LitStore
    from ..vector.faiss_index import VectorIndex

logger = logging.getLogger(__name__)


def _build_passage(paper_dict: dict) -> str:
    title = paper_dict.get("title", "")
    abstract = paper_dict.get("abstract", "")[:500]
    authors = " ".join(paper_dict.get("authors", [])[:3])
    journal = paper_dict.get("journal", "")
    year = paper_dict.get("year", "")
    parts = [p for p in (title, authors, journal, year, abstract) if p]
    return " | ".join(parts)


def _l1_coarse_recall(
    query: str,
    store: "LitStore",
    vector_index: "VectorIndex | None",
    settings: "LitSettings",
) -> dict[str, dict]:
    """L1：合并向量 + 关键词召回，按 DOI 去重。返回 {doi: paper_dict}。"""
    candidates: dict[str, dict] = {}

    if vector_index is not None and vector_index.size > 0:
        query_vec = encode_query(query, model=settings.embedding_model,
                                 api_key=settings.embedding_api_key)
        if query_vec:
            hits = vector_index.search(query_vec, top_k=settings.top_k_coarse)
            for doi, _score in hits:
                paper = store.get_paper(doi)
                if paper:
                    candidates[doi] = paper.model_dump()

    fts_results = store.search_fts(query, limit=settings.top_k_coarse)
    for sr in fts_results:
        if sr.doi not in candidates:
            paper = store.get_paper(sr.doi)
            if paper:
                candidates[sr.doi] = paper.model_dump()

    logger.info("L1 coarse recall: %d candidates (vector+fts)", len(candidates))
    return candidates


def _l2_rerank(
    query: str,
    candidates: dict[str, dict],
    settings: "LitSettings",
    api_key: str,
) -> list[tuple[str, float]]:
    """L2：reranker 精排。返回 [(doi, rerank_score)] 按分数降序。"""
    if not candidates or not api_key:
        return [(doi, 0.0) for doi in candidates]

    dois = list(candidates.keys())
    passages = [_build_passage(candidates[doi]) for doi in dois]

    scored = rerank(
        query=query,
        documents=passages,
        model=settings.reranker_model,
        api_key=api_key,
        top_n=settings.top_k_rerank,
    )

    result = []
    for orig_idx, score in scored:
        if orig_idx < len(dois):
            result.append((dois[orig_idx], score))

    logger.info("L2 rerank: %d → %d", len(candidates), len(result))
    return result


def _l3_deliver(
    ranked: list[tuple[str, float]],
    candidates: dict[str, dict],
    settings: "LitSettings",
) -> list[SearchResult]:
    """L3：综合排序 + 截断交付。"""
    weights = settings.score_weights
    w_r = weights.get("reranker", 0.40)
    w_p = weights.get("pagerank", 0.15)
    w_c = weights.get("cited", 0.20)

    max_cited = max(
        (candidates[doi].get("times_cited", 0) for doi, _ in ranked),
        default=1,
    ) or 1
    max_rank = max(
        (candidates[doi].get("paper_rank", 0.0) for doi, _ in ranked),
        default=1e-6,
    ) or 1e-6

    results = []
    for doi, rerank_score in ranked:
        p = candidates[doi]
        cited_norm = p.get("times_cited", 0) / max_cited
        rank_norm = p.get("paper_rank", 0.0) / max_rank

        final = (w_r * rerank_score
                 + w_p * rank_norm
                 + w_c * cited_norm)

        results.append(SearchResult(
            doi=doi,
            title=p.get("title", ""),
            authors=p.get("authors", []),
            year=p.get("year", ""),
            journal=p.get("journal", ""),
            abstract=p.get("abstract", ""),
            times_cited=p.get("times_cited", 0),
            impact_factor=p.get("impact_factor", 0.0),
            quartile=p.get("quartile", ""),
            library_citations=p.get("library_citations", 0),
            paper_rank=p.get("paper_rank", 0.0),
            relevance_score=rerank_score,
            final_score=final,
            match_source="funnel",
        ))

    results.sort(key=lambda r: r.final_score, reverse=True)
    delivered = results[:settings.top_k_deliver]
    logger.info("L3 deliver: %d → %d", len(ranked), len(delivered))
    return delivered


def search_funnel(
    query: str,
    store: "LitStore",
    vector_index: "VectorIndex | None",
    settings: "LitSettings",
    reranker_api_key: str | None = None,
) -> list[SearchResult]:
    """三层漏斗检索主入口。

    Args:
        query: 用户检索词
        store: LitStore 实例
        vector_index: VectorIndex 实例（可为 None，退化为纯关键词）
        settings: LitSettings
        reranker_api_key: 覆盖 settings.reranker_api_key（可选）

    Returns:
        按 final_score 降序的 SearchResult 列表
    """
    if not query or not query.strip():
        return []

    api_key = reranker_api_key if reranker_api_key is not None else settings.reranker_api_key

    candidates = _l1_coarse_recall(query, store, vector_index, settings)
    if not candidates:
        return []

    ranked = _l2_rerank(query, candidates, settings, api_key)
    if not ranked:
        ranked = [(doi, 0.0) for doi in list(candidates.keys())[:settings.top_k_rerank]]

    return _l3_deliver(ranked, candidates, settings)
