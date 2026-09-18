# -*- coding: utf-8 -*-
"""三级去重：DOI 精确 → title+year 模糊 → author+year+journal 兜底。

DOI 命中库内已有记录时不直接丢弃：对**空字段**做只补不覆的合并
（典型场景：库内是缺摘要的引用存根，WoS bib 带回完整记录）。
"""
from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime

from ..db import LitStore
from ..models import Paper

logger = logging.getLogger(__name__)

_FILL_SCALARS = ("title", "abstract", "journal", "year", "issn", "eissn")
_FILL_LISTS = ("authors", "affiliations", "corresponding", "keywords",
               "research_areas", "wos_categories", "references")


def _fill_missing(existing: Paper, incoming: Paper) -> bool:
    """把 incoming 的非空值只写入 existing 的空字段，返回是否有更新。

    已有非空值一律不覆盖（信任级：库内既有数据 ≥ 本次 bib 的补充位）。
    发生补全时来源升级为本次 bib（WoS 为最高信任级），数据完整则置 is_enriched。
    """
    changed = False
    for f in _FILL_SCALARS:
        if not getattr(existing, f) and getattr(incoming, f):
            setattr(existing, f, getattr(incoming, f))
            changed = True
    for f in _FILL_LISTS:
        cur = getattr(existing, f) or []
        inc = getattr(incoming, f) or []
        if not cur and inc:
            setattr(existing, f, inc)
            changed = True
        elif (f == "authors"
              and len(cur) <= 1 and not existing.source_authors
              and len(inc) > len(cur)):
            # WoS 参考文献存根只留第一作者缩写（["Yu CH"]）；主记录的完整作者列表应升级它
            setattr(existing, f, inc)
            changed = True
    if not existing.times_cited and incoming.times_cited:
        existing.times_cited = incoming.times_cited
        changed = True
    if changed:
        existing.source_main = incoming.source_main or existing.source_main
        existing.source_file = incoming.source_file or existing.source_file
        if existing.title and existing.abstract and not existing.is_enriched:
            existing.is_enriched = True
            existing.enriched_at = datetime.now().isoformat(timespec="seconds")
    return changed


def _normalize_doi(doi: str) -> str:
    doi = doi.strip().lower()
    doi = re.sub(r"^https?://doi\.org/", "", doi)
    doi = re.sub(r"^doi:\s*", "", doi)
    return doi.rstrip(".")


def _normalize_title(title: str) -> str:
    title = unicodedata.normalize("NFKD", title)
    title = title.lower()
    title = re.sub(r"[^a-z0-9\u4e00-\u9fff\s]", "", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title


def _title_year_key(paper: Paper) -> str:
    return f"{_normalize_title(paper.title)}|{paper.year}"


def _author_year_journal_key(paper: Paper) -> str:
    first_author = paper.authors[0] if paper.authors else ""
    first_author = first_author.split(",")[0].strip().lower()
    return f"{first_author}|{paper.year}|{paper.journal.lower().strip()}"


def deduplicate(papers: list[Paper], store: LitStore,
                progress_cb=None) -> tuple[list[Paper], int]:
    """三级去重。

    Args:
        progress_cb: 可选回调 (current, total, doi, phase)。判重/补字段是导入的
            耗时大头（尤其补全文件 DOI 多已存在），必须报进度否则前端长期停 0。

    Returns:
        (unique_papers, duplicate_count)
    """
    seen_dois: set[str] = set()
    seen_title_year: set[str] = set()
    seen_ayj: set[str] = set()
    unique: list[Paper] = []
    dup_count = 0
    total = len(papers)

    for i, paper in enumerate(papers):
        if progress_cb:
            progress_cb(i + 1, total, paper.doi, "判重与补全")
        doi_norm = _normalize_doi(paper.doi) if paper.doi else ""

        if doi_norm:
            if doi_norm in seen_dois:
                dup_count += 1
                continue
            existing = store.get_paper(doi_norm)
            if existing is not None:
                # 命中已有记录：只补空字段（存根 ← WoS 完整记录），不计入 unique
                if _fill_missing(existing, paper):
                    store.upsert_paper(existing)
                dup_count += 1
                continue
            seen_dois.add(doi_norm)
            paper.doi = doi_norm

        ty_key = _title_year_key(paper)
        if paper.title and ty_key in seen_title_year:
            dup_count += 1
            continue
        if paper.title:
            seen_title_year.add(ty_key)

        ayj_key = _author_year_journal_key(paper)
        if paper.authors and ayj_key in seen_ayj:
            dup_count += 1
            continue
        if paper.authors:
            seen_ayj.add(ayj_key)

        unique.append(paper)

    logger.info("dedup: %d input -> %d unique, %d duplicates",
                len(papers), len(unique), dup_count)
    return unique, dup_count
