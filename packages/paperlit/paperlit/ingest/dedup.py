# -*- coding: utf-8 -*-
"""三级去重：DOI 精确 → title+year 模糊 → author+year+journal 兜底。"""
from __future__ import annotations

import logging
import re
import unicodedata

from ..db import LitStore
from ..models import Paper

logger = logging.getLogger(__name__)


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


def deduplicate(papers: list[Paper], store: LitStore) -> tuple[list[Paper], int]:
    """三级去重。

    Returns:
        (unique_papers, duplicate_count)
    """
    seen_dois: set[str] = set()
    seen_title_year: set[str] = set()
    seen_ayj: set[str] = set()
    unique: list[Paper] = []
    dup_count = 0

    for paper in papers:
        doi_norm = _normalize_doi(paper.doi) if paper.doi else ""

        if doi_norm:
            if doi_norm in seen_dois:
                dup_count += 1
                continue
            if store.paper_exists(doi_norm):
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
