# -*- coding: utf-8 -*-
"""★ paperlit 唯一门面（facade）：backend/agent 只经本模块调用。

对外接口（P1 + P2 + P3 + P4 + P5 + P6 + P7）：
    init_lit / ingest_bib / ingest_bib_dir / paper_count / get_paper
    list_papers / citation_count / search_keyword
    enrich_pending / enrich_batch / enrich_one / unenriched_count
    compute_paper_rank / top_papers / compute_clusters / cluster_papers
    build_vector_index / vector_search / vector_index_size
    search  ★ 三层漏斗检索（L1 粗筛 → L2 rerank → L3 交付）
    cached_search / cache_clear / cache_stats  ★ 查询缓存
    compile_topics / list_topics / get_topic  ★ 主题编译
"""
from __future__ import annotations

import logging
from pathlib import Path

from .config import LitSettings, Roots
from .db import LitStore
from .ingest.bib_ingest import parse_bib_file, ingest_papers
from .ingest.dedup import deduplicate
from .models import Paper, SearchResult

__all__ = [
    "init_lit", "ingest_bib", "ingest_bib_dir",
    "paper_count", "get_paper", "list_papers",
    "citation_count", "ranked_count", "search_keyword",
    "enrich_pending", "enrich_batch", "enrich_one", "unenriched_count",
    "normalize_journals", "get_unique_journals",
    "get_journal_mapper_stats", "add_journal_mapping",
    "attach_journal_metrics", "compute_library_citations",
    "preview_cleaning", "execute_cleaning",
    "compute_paper_rank", "top_papers",
    "compute_clusters", "cluster_papers",
    "graph_network", "graph_filter_facets", "graph_neighbors", "graph_node_detail",
    "graph_cluster_stats",
    "build_vector_index", "vector_search", "vector_index_size",
    "search",
    "cached_search", "cache_clear", "cache_stats",
    "compile_topics", "list_topics", "get_topic",
]

logger = logging.getLogger(__name__)

_store: LitStore | None = None
_settings: LitSettings = LitSettings()
_vector_index = None
_journal_mapper = None


def init_lit(roots: Roots, settings: LitSettings | None = None) -> dict:
    """初始化文献检索库（创建 schema、返回目录状态）。"""
    global _store, _settings, _journal_mapper
    if settings is not None:
        _settings = settings
    roots.ensure()
    _store = LitStore(roots)
    _store.init_schema()

    # 初始化期刊映射器
    from .ingest.journal_mapper import JournalMapper
    _journal_mapper = JournalMapper(roots.journals_db)

    stats = _store.stats()
    logger.info("paperlit initialized: %s", stats)
    return stats


def _require_store() -> LitStore:
    if _store is None:
        raise RuntimeError("paperlit not initialized; call init_lit() first")
    return _store


def ingest_bib(bib_path: str | Path, source_file: str = "",
               import_refs: bool = True, min_year: int | None = None,
               max_year: int | None = None, progress_cb=None) -> dict:
    """导入单个 WoS bib 文件。

    Args:
        import_refs: 是否导入参考文献（默认导入）
        min_year: 最小年份（过滤早于此年份的文献）
        max_year: 最大年份（过滤晚于此年份的文献）
        progress_cb: 可选回调 (current, total, doi) 用于进度显示

    Returns:
        {"parsed": int, "deduped": int, "new_papers": int,
         "new_refs": int, "new_citations": int, "duplicates_skipped": int,
         "filtered_by_year": int}
    """
    store = _require_store()
    if not source_file:
        source_file = Path(bib_path).name

    raw_papers = parse_bib_file(bib_path, source_file=source_file,
                                journal_mapper=_journal_mapper,
                                progress_cb=progress_cb)
    # 年份过滤
    filtered_count = 0
    if min_year is not None or max_year is not None:
        filtered = []
        for p in raw_papers:
            if p.year is None:
                filtered.append(p)  # 无年份保留
                continue
            if min_year is not None and p.year < min_year:
                filtered_count += 1
                continue
            if max_year is not None and p.year > max_year:
                filtered_count += 1
                continue
            filtered.append(p)
        raw_papers = filtered
    unique_papers, dup_count = deduplicate(raw_papers, store,
                                           progress_cb=progress_cb)
    result = ingest_papers(unique_papers, store, source_file=source_file,
                           import_refs=import_refs, progress_cb=progress_cb)
    result["parsed"] = len(raw_papers) + filtered_count
    result["duplicates_skipped"] = dup_count
    result["filtered_by_year"] = filtered_count
    return result


def ingest_bib_dir(dir_path: str | Path, import_refs: bool = True,
                   min_year: int | None = None, max_year: int | None = None) -> dict:
    """批量导入目录下所有 .bib 文件。"""
    dir_path = Path(dir_path)
    total = {"parsed": 0, "deduped": 0, "new_papers": 0,
             "new_refs": 0, "new_citations": 0, "duplicates_skipped": 0,
             "filtered_by_year": 0, "files": 0}
    for bib_file in sorted(dir_path.glob("*.bib")):
        result = ingest_bib(bib_file, import_refs=import_refs,
                            min_year=min_year, max_year=max_year)
        for k in total:
            if k != "files":
                total[k] += result.get(k, 0)
        total["files"] += 1
        logger.info("ingested %s: %s", bib_file.name, result)
    return total


def paper_count() -> int:
    """返回库中总文献数（含参考文献）。"""
    store = _require_store()
    return store.paper_count()


def get_paper(doi: str) -> Paper | None:
    """按 DOI 查询单篇文献。"""
    store = _require_store()
    return store.get_paper(doi)


def list_papers(offset: int = 0, limit: int = 20,
                is_reference: bool | None = None) -> list[Paper]:
    """分页列出文献。"""
    store = _require_store()
    return store.list_papers(offset=offset, limit=limit,
                             is_reference=is_reference)


def citation_count() -> int:
    """返回引用关系总数。"""
    store = _require_store()
    return store.citation_count()


def ranked_count() -> int:
    """返回已计算 PaperRank 的文献数。"""
    store = _require_store()
    return store.ranked_count()


def search_keyword(query: str, limit: int = 20) -> list[SearchResult]:
    """关键词检索（FTS5）。"""
    store = _require_store()
    return store.search_fts(query, limit=limit)


# ---- P2: 元数据补全 ----

def enrich_pending(batch_size: int = 100) -> dict:
    """补全所有待处理文献的元数据（OpenAlex 批量 + CrossRef 兜底）。

    Returns:
        {"rounds": int, "enriched": int, "failed": int}
    """
    from .ingest.enrich import enrich_all_pending
    store = _require_store()
    return enrich_all_pending(store, batch_size=batch_size,
                              journal_mapper=_journal_mapper)


def enrich_batch(dois: list[str]) -> dict:
    """补全指定 DOI 列表的元数据。

    Returns:
        {"total": int, "enriched": int, "already_done": int,
         "failed": int, "sources": {"openalex": int, "crossref": int}}
    """
    from .ingest.enrich import enrich_batch as _enrich_batch
    store = _require_store()
    return _enrich_batch(dois, store, rate_limit=_settings.enrich_rate_limit,
                         journal_mapper=_journal_mapper)


def enrich_one(doi: str) -> dict:
    """补全单篇文献元数据。

    Returns:
        {"doi": str, "ok": bool, "filled": [str], "source": str}
    """
    from .ingest.enrich import enrich_one as _enrich_one
    store = _require_store()
    return _enrich_one(doi, store, journal_mapper=_journal_mapper)


def unenriched_count() -> int:
    """返回待补全元数据的文献数。"""
    store = _require_store()
    dois = store.get_unenriched_dois(limit=99999)
    return len(dois)


def pending_dois(include_non_wos: bool = True) -> list[str]:
    """返回需要 WoS 补全的 DOI：未补全，或来源非 WoS。"""
    store = _require_store()
    return store.get_pending_dois(include_non_wos=include_non_wos)


def all_dois() -> list[str]:
    """返回文献库全部 DOI。"""
    store = _require_store()
    return store.get_all_dois()


# ---- 期刊名规范化 ----

def normalize_journals(rate_limit: float = 10.0, progress_cb=None) -> dict:
    """批量规范化期刊名（缩写 → 全称，经 OpenAlex 解析）。

    Returns:
        {"unique": int, "resolved": int, "updated": int,
         "unchanged": int, "failed": int, "saved_mappings": int,
         "mapping": {old: new, ...}}
    """
    from .ingest.normalize import normalize_journals as _normalize
    store = _require_store()
    return _normalize(store, rate_limit=rate_limit, journal_mapper=_journal_mapper,
                      progress_cb=progress_cb)


def get_unique_journals() -> list[dict]:
    """列出所有唯一期刊名及文献数。"""
    from .ingest.normalize import get_unique_journals as _get
    store = _require_store()
    return _get(store)


def get_journal_mapper_stats() -> dict:
    """获取期刊映射表统计。"""
    if _journal_mapper is None:
        return {"total": 0, "cache_size": 0, "by_source": {}}
    return _journal_mapper.get_stats()


def if_covered_count() -> int:
    """已关联影响因子的文献数。"""
    store = _require_store()
    return store.if_covered_count()


def add_journal_mapping(abbreviation: str, full_name: str,
                        source: str = "manual") -> bool:
    """添加期刊映射。"""
    if _journal_mapper is None:
        raise RuntimeError("journal_mapper not initialized")
    return _journal_mapper.add_mapping(abbreviation, full_name, source)


# ---- P3: 引用图谱 ----

def compute_paper_rank(damping: float = 0.85) -> dict:
    """计算所有文献的 PaperRank（类 PageRank）。

    Returns:
        {"computed": int, "iterations": int, "converged": bool}
    """
    from .graph import compute_paper_rank as _compute
    store = _require_store()
    return _compute(store, damping=damping)


def top_papers(limit: int = 20) -> list[dict]:
    """获取 PaperRank 最高的文献列表。"""
    from .graph import get_top_papers
    store = _require_store()
    return get_top_papers(store, limit=limit)


def compute_clusters(min_cocitations: int = 2) -> dict:
    """计算共被引聚类。

    Returns:
        {"papers": int, "clusters": int, "method": str}
    """
    from .graph import compute_cocitation_clusters
    store = _require_store()
    return compute_cocitation_clusters(store, min_cocitations=min_cocitations)


def cluster_papers(cluster_id: int) -> list[dict]:
    """获取指定聚类的所有文献。"""
    from .graph import get_cluster_papers
    store = _require_store()
    return get_cluster_papers(store, cluster_id)


# ---- 文献计量图谱（引用网络可视化）----

def graph_network(*, year_min: int | None = None, year_max: int | None = None,
                  min_library_citations: int | None = None,
                  min_times_cited: int | None = None,
                  min_impact_factor: float | None = None,
                  quartiles: list[str] | None = None,
                  cluster: int | None = None,
                  exclude_references: bool = False,
                  in_kb_only: bool = False,
                  sort_by: str = "library_citations",
                  limit: int = 5000,
                  kb_dois: set[str] | None = None,
                  preview: bool = False,
                  citation_source: str = "filtered",
                  exclude_isolated: bool = True) -> dict:
    """导出引用网络（节点 + 边），服务端过滤。见 graph.network.build_network。"""
    from .graph import build_network
    store = _require_store()
    return build_network(
        store, year_min=year_min, year_max=year_max,
        min_library_citations=min_library_citations,
        min_times_cited=min_times_cited,
        min_impact_factor=min_impact_factor,
        quartiles=quartiles, cluster=cluster,
        exclude_references=exclude_references, in_kb_only=in_kb_only,
        sort_by=sort_by, limit=limit, kb_dois=kb_dois,
        preview=preview, citation_source=citation_source,
        exclude_isolated=exclude_isolated,
    )


def graph_filter_facets(*, kb_dois: set[str] | None = None) -> dict:
    """过滤器面板分面信息（取值范围 / 计数 / 聚类列表）。"""
    from .graph import get_filter_facets
    store = _require_store()
    return get_filter_facets(store, kb_dois=kb_dois)


def graph_neighbors(doi: str) -> dict:
    """长按高亮用：引用该文献的（citing）与该文献引用的（cited）DOI。"""
    from .graph import get_neighbors
    store = _require_store()
    return get_neighbors(store, doi)


def graph_node_detail(doi: str) -> dict | None:
    """节点详情（标题/摘要/关键词/作者/期刊/年份/IF/分区/被引 + 度数）。"""
    from .graph import get_node_detail
    store = _require_store()
    return get_node_detail(store, doi)


def graph_cluster_stats() -> list[dict]:
    """聚类统计：id, size, top_keywords。"""
    from .graph import cluster_stats
    store = _require_store()
    return cluster_stats(store)


# ---- P4: 向量索引 ----

def _get_vector_index():
    """获取或创建向量索引实例。"""
    global _vector_index
    if _vector_index is None:
        from .vector import VectorIndex
        store = _require_store()
        _vector_index = VectorIndex(
            dim=_settings.embedding_dim,
            store=store,
            index_dir=_store.roots.faiss_dir,
        )
    return _vector_index


def build_vector_index(api_key: str = "", batch_size: int = 32) -> dict:
    """构建向量索引（为所有有 abstract 的文献生成 embedding）。

    Args:
        api_key: SiliconFlow API key（必须）
        batch_size: 批量编码大小

    Returns:
        {"indexed": int, "skipped": int, "dim": int}
    """
    from .vector import encode_texts
    store = _require_store()

    papers = store.list_papers(offset=0, limit=999999, is_reference=False)
    to_embed = []
    for p in papers:
        text = f"{p.title}. {p.abstract}" if p.abstract else p.title
        if text.strip():
            to_embed.append((p.doi, text))

    if not to_embed:
        return {"indexed": 0, "skipped": 0, "dim": _settings.embedding_dim}

    texts = [t for _, t in to_embed]
    embeddings = encode_texts(texts, model=_settings.embedding_model,
                              api_key=api_key, batch_size=batch_size)

    doi_embeddings = [
        (doi, emb) for (doi, _), emb in zip(to_embed, embeddings)
        if emb and len(emb) == _settings.embedding_dim
    ]

    idx = _get_vector_index()
    indexed = idx.build(doi_embeddings)

    return {
        "indexed": indexed,
        "skipped": len(to_embed) - indexed,
        "dim": _settings.embedding_dim,
    }


def vector_search(query: str, api_key: str = "",
                  top_k: int = 20) -> list[tuple[str, float]]:
    """向量语义检索。

    Args:
        query: 查询文本
        api_key: SiliconFlow API key
        top_k: 返回数量

    Returns:
        [(doi, score), ...] 按相似度降序
    """
    from .vector import encode_query
    if not api_key:
        raise ValueError("SiliconFlow API key 未配置")

    query_vec = encode_query(query, model=_settings.embedding_model,
                             api_key=api_key)
    if not query_vec:
        return []

    idx = _get_vector_index()
    return idx.search(query_vec, top_k=top_k)


def vector_index_size() -> int:
    """返回向量索引中的文献数。"""
    idx = _get_vector_index()
    return idx.size


# ---- P5: 三层漏斗检索 ----

def search(query: str, reranker_api_key: str | None = None) -> list[SearchResult]:
    """三层漏斗检索（L1 粗筛 → L2 rerank → L3 交付）。

    合并向量 + 关键词召回，reranker 精排后按综合分交付。

    Args:
        query: 检索词
        reranker_api_key: 覆盖 settings.reranker_api_key（可选）

    Returns:
        按 final_score 降序的 SearchResult 列表
    """
    from .retrieve.pipeline import search_funnel
    store = _require_store()
    idx = _get_vector_index()
    return search_funnel(
        query=query,
        store=store,
        vector_index=idx,
        settings=_settings,
        reranker_api_key=reranker_api_key,
    )


# ---- P7: 查询缓存 + 主题编译 ----

def cached_search(query: str, reranker_api_key: str | None = None,
                  ttl_hours: float = 24) -> list[SearchResult]:
    """带缓存的检索：命中缓存直接返回，未中则执行漏斗并缓存结果。"""
    from .compile.query_cache import cache_get, cache_set
    store = _require_store()

    cached_dois = cache_get(store, query, ttl_hours=ttl_hours)
    if cached_dois is not None:
        n = len(cached_dois)
        results = []
        for i, doi in enumerate(cached_dois):
            paper = store.get_paper(doi)
            if paper:
                results.append(SearchResult(
                    doi=paper.doi, title=paper.title,
                    authors=paper.authors, year=paper.year,
                    journal=paper.journal,
                    abstract=paper.abstract,
                    times_cited=paper.times_cited,
                    impact_factor=paper.impact_factor,
                    quartile=paper.quartile,
                    library_citations=paper.library_citations,
                    paper_rank=paper.paper_rank,
                    # 缓存按相关度降序存 DOI，用位置还原顺序分（保证"相关度"排序可用）
                    final_score=round((n - i) / n, 6) if n else 0.0,
                    match_source="cache",
                ))
        logger.info("cache hit for '%s': %d results", query[:50], len(results))
        return results

    results = search(query, reranker_api_key=reranker_api_key)
    cache_set(store, query, [r.doi for r in results])
    return results


def cache_clear() -> int:
    """清空查询缓存。"""
    from .compile.query_cache import cache_clear as _clear
    store = _require_store()
    return _clear(store)


def cache_stats() -> dict:
    """查询缓存统计。"""
    from .compile.query_cache import cache_stats as _stats
    store = _require_store()
    return _stats(store)


def compile_topics(field: str = "wos_categories",
                   min_papers: int = 2) -> dict:
    """编译主题（按 WoS categories / research_areas / keywords 聚合）。"""
    from .compile.topic_compiler import compile_topics as _compile
    store = _require_store()
    return _compile(store, field=field, min_papers=min_papers)


def list_topics(limit: int = 50, offset: int = 0) -> list[dict]:
    """列出所有主题（按论文数降序）。"""
    from .compile.topic_compiler import list_topics as _list
    store = _require_store()
    return _list(store, limit=limit, offset=offset)


def get_topic(slug: str) -> dict | None:
    """获取单个主题详情。"""
    from .compile.topic_compiler import get_topic as _get
    store = _require_store()
    return _get(store, slug)


def attach_journal_metrics(journals_db_path: str | Path) -> dict:
    """关联期刊指标（影响因子、分区）到 papers 表。"""
    from .ingest.cleaning import attach_journal_metrics as _attach
    store = _require_store()
    return _attach(store, journals_db_path)


def compute_library_citations() -> dict:
    """计算每篇文献在库内被引用的次数。"""
    from .ingest.cleaning import compute_library_citations as _compute
    store = _require_store()
    return _compute(store)


def preview_cleaning(rules: dict) -> dict:
    """预览清洗结果（不实际删除）。"""
    from .ingest.cleaning import preview_cleaning as _preview
    store = _require_store()
    return _preview(store, rules)


def execute_cleaning(rules: dict, mode: str = "delete") -> dict:
    """执行清洗（删除或标记文献）。"""
    from .ingest.cleaning import execute_cleaning as _execute
    store = _require_store()
    return _execute(store, rules, mode)
