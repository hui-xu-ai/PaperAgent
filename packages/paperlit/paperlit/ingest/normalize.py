# -*- coding: utf-8 -*-
"""期刊名规范化：缩写 → 全称。

策略：对每种唯一期刊名取一个代表 DOI，经 OpenAlex 获取全称，
然后批量 UPDATE 所有同名记录。~1286 种唯一期刊 ≈ 2 分钟。
"""
from __future__ import annotations

import logging
import time

from ..db import LitStore
from ..sources import openalex

logger = logging.getLogger(__name__)


def get_unique_journals(store: LitStore) -> list[dict]:
    """列出所有唯一期刊名及其文献数。

    Returns:
        [{"journal": str, "count": int, "sample_doi": str}, ...]
        按 count 降序。
    """
    with store._conn() as conn:
        rows = conn.execute("""
            SELECT journal, COUNT(*) AS cnt
            FROM papers
            WHERE journal != ''
            GROUP BY journal
            ORDER BY cnt DESC
        """).fetchall()

        result = []
        for r in rows:
            doi_row = conn.execute(
                "SELECT doi FROM papers WHERE journal = ? AND doi != '' "
                "AND LENGTH(doi) < 100 LIMIT 1",
                (r["journal"],)
            ).fetchone()
            result.append({
                "journal": r["journal"],
                "count": r["cnt"],
                "sample_doi": doi_row["doi"] if doi_row else "",
            })
    return result


def _resolve_journal_name(journal_name: str, sample_doi: str,
                          rate_limit: float = 10.0) -> str:
    """经 OpenAlex 解析单个期刊名 → 全称。失败返回空串。"""
    if not sample_doi:
        return ""
    data = openalex.fetch_one(sample_doi)
    if not data:
        return ""
    resolved = data.get("journal", "").strip()
    if resolved and resolved.upper() != journal_name.upper():
        return resolved
    return ""


def normalize_journals(store: LitStore, rate_limit: float = 5.0,
                       max_retries: int = 2, journal_mapper=None,
                       progress_cb=None) -> dict:
    """批量规范化所有期刊名为全称。

    已有映射（journal_mapper 缓存）的期刊直接复用、不走网络——重跑代价低、可中断续跑。

    Args:
        store: LitStore 实例
        rate_limit: 每秒请求数（OpenAlex polite pool 限制 10/s）
        max_retries: 失败重试次数
        journal_mapper: 期刊映射器（可选，用于保存映射结果）
        progress_cb: 可选回调 (current, total, journal_name)

    Returns:
        {"unique": int, "resolved": int, "updated": int,
         "unchanged": int, "failed": int,
         "mapping": {old_name: new_name, ...}}
    """
    journals = get_unique_journals(store)
    total = len(journals)
    mapping: dict[str, str] = {}
    newly_resolved: dict[str, str] = {}
    resolved_count = 0
    failed_count = 0

    for i, entry in enumerate(journals):
        old_name = entry["journal"]
        sample_doi = entry["sample_doi"]

        cached = journal_mapper.lookup(old_name) if journal_mapper else None
        if cached:
            mapping[old_name] = cached
            if cached != old_name:
                resolved_count += 1
            if progress_cb:
                progress_cb(i + 1, total, old_name)
            continue

        if not sample_doi:
            failed_count += 1
            mapping[old_name] = old_name
            if progress_cb:
                progress_cb(i + 1, total, old_name)
            continue

        new_name = ""
        for attempt in range(max_retries + 1):
            new_name = _resolve_journal_name(old_name, sample_doi)
            if new_name:
                break
            if attempt < max_retries:
                time.sleep(2 ** attempt)

        if new_name:
            mapping[old_name] = new_name
            newly_resolved[old_name] = new_name
            resolved_count += 1
        else:
            mapping[old_name] = old_name
            failed_count += 1

        if progress_cb:
            progress_cb(i + 1, total, old_name)
        time.sleep(1.0 / rate_limit)

    real = {k: v for k, v in mapping.items() if k != v}
    unchanged_count = len(mapping) - len(real)
    with store._conn() as conn:
        if real:
            conn.execute("CREATE TEMP TABLE jmap(old TEXT PRIMARY KEY, new TEXT NOT NULL)")
            conn.executemany("INSERT INTO jmap(old, new) VALUES (?,?)",
                             list(real.items()))
            cur = conn.execute("""
                UPDATE papers SET journal =
                  (SELECT m.new FROM jmap m WHERE m.old = papers.journal)
                WHERE EXISTS (SELECT 1 FROM jmap m WHERE m.old = papers.journal)
            """)
            updated_count = cur.rowcount
        else:
            updated_count = 0

    logger.info("normalize_journals: %d unique, %d resolved, %d updated, "
                "%d unchanged, %d failed",
                total, resolved_count, updated_count,
                unchanged_count, failed_count)

    # 保存映射到 journal_mapper（如果有）——仅保存本次新解析的，避免重跑时重复写
    saved_mappings = 0
    if journal_mapper and newly_resolved:
        saved_mappings = journal_mapper.bulk_add_mappings(newly_resolved, source="openalex")
        logger.info("Saved %d journal mappings to database", saved_mappings)

    return {
        "unique": total,
        "resolved": resolved_count,
        "updated": updated_count,
        "unchanged": unchanged_count,
        "failed": failed_count,
        "saved_mappings": saved_mappings,
        "mapping": real,
    }
