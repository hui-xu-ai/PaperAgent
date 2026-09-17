# -*- coding: utf-8 -*-
"""批量元数据补全服务（多源编排 + 来源标签）。

信任链：WoS bib（原始导入）> OpenAlex（批量补全主力）> CrossRef（兜底）> Semantic Scholar（补充）。
每条元数据字段记录来源（source_* 字段），WoS 值不被覆盖。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ..db import LitStore
from ..models import Paper
from ..sources import openalex, crossref, semantic_scholar

logger = logging.getLogger(__name__)

_FILLABLE_FIELDS = (
    "title", "abstract", "journal", "year", "issn",
    "times_cited",
)
_LIST_FIELDS = (
    "authors", "affiliations", "corresponding", "keywords", "research_areas",
)


def _merge_field(current: Any, incoming: Any, source_label: str,
                 paper: Paper, field: str) -> bool:
    """只填空字段，返回是否补了。"""
    if field in _LIST_FIELDS:
        cur = getattr(paper, field, []) or []
        if not cur and incoming:
            setattr(paper, field, list(incoming))
            if field == "authors":
                paper.source_authors = source_label
            return True
        return False

    cur = getattr(paper, field, "") or ""
    if field == "times_cited":
        if int(cur or 0) <= 0 and int(incoming or 0) > 0:
            setattr(paper, field, int(incoming))
            return True
        return False

    if not str(cur).strip() and incoming:
        setattr(paper, field, str(incoming).strip())
        if field == "abstract":
            paper.source_abstract = source_label
        return True
    return False


def enrich_one(doi: str, store: LitStore,
               ss_api_key: str = "") -> dict:
    """补全单篇文献元数据。

    Returns:
        {"doi": str, "ok": bool, "filled": [str], "source": str}
    """
    doi = doi.strip()
    if not doi:
        return {"doi": doi, "ok": False, "reason": "no_doi"}

    paper = store.get_paper(doi)
    if paper is None:
        return {"doi": doi, "ok": False, "reason": "not_in_db"}

    if paper.is_enriched:
        return {"doi": doi, "ok": True, "filled": [], "source": "cached"}

    filled: list[str] = []

    oa = openalex.fetch_one(doi)
    if oa:
        for f in _FILLABLE_FIELDS:
            if _merge_field(None, oa.get(f), "openalex", paper, f):
                filled.append(f)
        for f in _LIST_FIELDS:
            if _merge_field(None, oa.get(f), "openalex", paper, f):
                filled.append(f)

    if not paper.title or not paper.abstract:
        cr = crossref.fetch_one(doi)
        if cr:
            for f in _FILLABLE_FIELDS:
                if _merge_field(None, cr.get(f), "crossref", paper, f):
                    if f not in filled:
                        filled.append(f)
            for f in _LIST_FIELDS:
                if _merge_field(None, cr.get(f), "crossref", paper, f):
                    if f not in filled:
                        filled.append(f)

    if not paper.source_main or paper.source_main == "wos_ref":
        if oa:
            paper.source_main = "openalex"
        elif crossref.fetch_one(doi):
            paper.source_main = "crossref"

    if filled:
        paper.is_enriched = True
        paper.enriched_at = datetime.now().isoformat(timespec="seconds")
        store.upsert_paper(paper)

    return {
        "doi": doi,
        "ok": bool(filled),
        "filled": filled,
        "source": oa.get("source", "none") if oa else "none",
    }


def enrich_batch(dois: list[str], store: LitStore,
                 ss_api_key: str = "",
                 rate_limit: float = 5.0) -> dict:
    """批量补全（OpenAlex 批量查 → CrossRef 逐条兜底）。

    Returns:
        {"total": int, "enriched": int, "already_done": int,
         "failed": int, "sources": {"openalex": int, "crossref": int}}
    """
    clean = [d.strip() for d in dois if d and d.strip()]
    if not clean:
        return {"total": 0, "enriched": 0, "already_done": 0, "failed": 0}

    to_enrich: list[str] = []
    already = 0
    for d in clean:
        p = store.get_paper(d)
        if p is None:
            continue
        if p.is_enriched:
            already += 1
        else:
            to_enrich.append(d)

    if not to_enrich:
        return {"total": len(clean), "enriched": 0,
                "already_done": already, "failed": 0}

    oa_results = openalex.fetch_batch(to_enrich, rate_limit=rate_limit)
    logger.info("OpenAlex batch: %d/%d found", len(oa_results), len(to_enrich))

    enriched = 0
    source_counts: dict[str, int] = {"openalex": 0, "crossref": 0}

    for doi_lower in to_enrich:
        doi_key = doi_lower.lower()
        paper = store.get_paper(doi_lower)
        if paper is None or paper.is_enriched:
            continue

        filled: list[str] = []

        if doi_key in oa_results:
            oa = oa_results[doi_key]
            for f in _FILLABLE_FIELDS:
                if _merge_field(None, oa.get(f), "openalex", paper, f):
                    filled.append(f)
            for f in _LIST_FIELDS:
                if _merge_field(None, oa.get(f), "openalex", paper, f):
                    filled.append(f)
            if filled:
                source_counts["openalex"] += 1

        needs_crossref = not paper.title or not paper.abstract
        if needs_crossref:
            cr = crossref.fetch_one(doi_lower)
            if cr:
                for f in _FILLABLE_FIELDS:
                    if _merge_field(None, cr.get(f), "crossref", paper, f):
                        if f not in filled:
                            filled.append(f)
                for f in _LIST_FIELDS:
                    if _merge_field(None, cr.get(f), "crossref", paper, f):
                        if f not in filled:
                            filled.append(f)
                if filled and not source_counts.get("openalex"):
                    source_counts["crossref"] += 1

        if filled:
            paper.is_enriched = True
            paper.enriched_at = datetime.now().isoformat(timespec="seconds")
            if not paper.source_main or paper.source_main == "wos_ref":
                paper.source_main = "openalex" if doi_key in oa_results else "crossref"
            store.upsert_paper(paper)
            enriched += 1

    result = {
        "total": len(clean),
        "enriched": enriched,
        "already_done": already,
        "failed": len(to_enrich) - enriched,
        "sources": source_counts,
    }
    logger.info("enrich_batch: %s", result)
    return result


def enrich_all_pending(store: LitStore, batch_size: int = 100,
                       ss_api_key: str = "") -> dict:
    """补全所有待处理文献（分批进行）。"""
    total_enriched = 0
    total_failed = 0
    round_num = 0

    while True:
        dois = store.get_unenriched_dois(limit=batch_size)
        if not dois:
            break
        round_num += 1
        result = enrich_batch(dois, store, ss_api_key=ss_api_key)
        total_enriched += result["enriched"]
        total_failed += result["failed"]
        logger.info("enrich round %d: %d enriched, %d failed",
                     round_num, result["enriched"], result["failed"])
        if result["enriched"] == 0:
            break

    return {"rounds": round_num, "enriched": total_enriched, "failed": total_failed}
