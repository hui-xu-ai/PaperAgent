# -*- coding: utf-8 -*-
"""主题编译（topic_cache 表）。

按 WoS categories / research areas 聚合文献，
统计每主题的论文数、平均引用、平均 PaperRank、代表性 DOI。
结果存入 topic_cache 表，供前端主题浏览。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..db import LitStore

logger = logging.getLogger(__name__)


def _slugify(text: str) -> str:
    s = text.lower().strip()
    s = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", s)
    return s.strip("-")[:80] or "unknown"


def compile_topics(store: "LitStore", field: str = "wos_categories",
                   min_papers: int = 2) -> dict:
    """按指定字段聚合文献，编译主题。

    Args:
        store: LitStore
        field: 聚合字段（wos_categories / research_areas / keywords）
        min_papers: 主题最少论文数（低于则跳过）

    Returns:
        {"topics": int, "papers_covered": int, "field": str}
    """
    valid_fields = {"wos_categories", "research_areas", "keywords"}
    if field not in valid_fields:
        raise ValueError(f"field must be one of {valid_fields}")

    json_col = f"{field}_json"
    with store._conn() as conn:
        rows = conn.execute(
            f"SELECT doi, {json_col} AS vals, times_cited, paper_rank "
            "FROM papers WHERE is_reference=0"
        ).fetchall()

    topic_map: dict[str, dict] = {}
    for row in rows:
        try:
            categories = json.loads(row["vals"]) if row["vals"] else []
        except (json.JSONDecodeError, TypeError):
            categories = []
        if not categories:
            continue

        doi = row["doi"]
        cited = row["times_cited"] or 0
        rank = row["paper_rank"] or 0.0

        for cat in categories:
            slug = _slugify(cat)
            if slug not in topic_map:
                topic_map[slug] = {
                    "name": cat,
                    "dois": [],
                    "total_cited": 0,
                    "total_rank": 0.0,
                }
            topic_map[slug]["dois"].append(doi)
            topic_map[slug]["total_cited"] += cited
            topic_map[slug]["total_rank"] += rank

    now = datetime.now().isoformat(timespec="seconds")
    topics_written = 0
    papers_covered = set()

    with store._conn() as conn:
        for slug, info in topic_map.items():
            if len(info["dois"]) < min_papers:
                continue

            n = len(info["dois"])
            avg_cited = info["total_cited"] / n
            avg_rank = info["total_rank"] / n

            summary = (f"{n} papers, avg cited {avg_cited:.1f}, "
                       f"avg rank {avg_rank:.4f}")

            conn.execute("""
                INSERT OR REPLACE INTO topic_cache
                (topic_slug, topic_name, summary, paper_dois_json,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, 
                    COALESCE((SELECT created_at FROM topic_cache WHERE topic_slug=?), ?),
                    ?)
            """, (slug, info["name"], summary,
                  json.dumps(info["dois"], ensure_ascii=False),
                  slug, now, now))

            topics_written += 1
            papers_covered.update(info["dois"])

    logger.info("compiled %d topics from %d papers (field=%s)",
                topics_written, len(papers_covered), field)
    return {
        "topics": topics_written,
        "papers_covered": len(papers_covered),
        "field": field,
    }


def list_topics(store: "LitStore", limit: int = 50,
                offset: int = 0) -> list[dict]:
    """列出所有主题（按论文数降序）。"""
    with store._conn() as conn:
        rows = conn.execute("""
            SELECT topic_slug, topic_name, summary, paper_dois_json,
                   created_at, updated_at
            FROM topic_cache
            ORDER BY json_array_length(paper_dois_json) DESC
            LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()

    results = []
    for r in rows:
        try:
            dois = json.loads(r["paper_dois_json"])
        except (json.JSONDecodeError, TypeError):
            dois = []
        results.append({
            "slug": r["topic_slug"],
            "name": r["topic_name"],
            "summary": r["summary"],
            "paper_count": len(dois),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        })
    return results


def get_topic(store: "LitStore", slug: str) -> dict | None:
    """获取单个主题详情。"""
    with store._conn() as conn:
        row = conn.execute(
            "SELECT * FROM topic_cache WHERE topic_slug=?", (slug,)
        ).fetchone()
    if row is None:
        return None

    try:
        dois = json.loads(row["paper_dois_json"])
    except (json.JSONDecodeError, TypeError):
        dois = []

    return {
        "slug": row["topic_slug"],
        "name": row["topic_name"],
        "summary": row["summary"],
        "paper_dois": dois,
        "paper_count": len(dois),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
