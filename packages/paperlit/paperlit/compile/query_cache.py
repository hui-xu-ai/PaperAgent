# -*- coding: utf-8 -*-
"""查询缓存（query_cache 表）。

对检索 query 做哈希，命中则直接返回缓存的 DOI 列表，
避免重复执行漏斗管线（尤其 reranker 开销大）。

缓存键：query 文本的 SHA-256 前 16 位
TTL：默认 24 小时，过期自动失效
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..db import LitStore

logger = logging.getLogger(__name__)

_DEFAULT_TTL_HOURS = 24


def _hash_query(query: str) -> str:
    return hashlib.sha256(query.strip().lower().encode()).hexdigest()[:16]


def cache_get(store: "LitStore", query: str,
              ttl_hours: float = _DEFAULT_TTL_HOURS) -> list[str] | None:
    """查询缓存。命中返回 DOI 列表，未中或过期返回 None。"""
    qh = _hash_query(query)
    with store._conn() as conn:
        row = conn.execute(
            "SELECT result_dois_json, created_at FROM query_cache "
            "WHERE query_hash=?", (qh,)
        ).fetchone()

    if row is None:
        return None

    created_at = row["created_at"]
    if created_at:
        try:
            created = datetime.fromisoformat(created_at)
            if datetime.now() - created > timedelta(hours=ttl_hours):
                return None
        except (ValueError, TypeError):
            pass

    try:
        return json.loads(row["result_dois_json"])
    except (json.JSONDecodeError, TypeError):
        return None


def cache_set(store: "LitStore", query: str, result_dois: list[str],
              query_expanded: str = "") -> None:
    """写入缓存。"""
    qh = _hash_query(query)
    now = datetime.now().isoformat(timespec="seconds")
    with store._conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO query_cache
            (query_hash, query, query_expanded, result_dois_json, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (qh, query, query_expanded,
              json.dumps(result_dois, ensure_ascii=False), now))


def cache_clear(store: "LitStore") -> int:
    """清空所有缓存。返回清除条数。"""
    with store._conn() as conn:
        count = conn.execute("SELECT COUNT(*) FROM query_cache").fetchone()[0]
        conn.execute("DELETE FROM query_cache")
    return count


def cache_stats(store: "LitStore") -> dict:
    """缓存统计。"""
    with store._conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM query_cache").fetchone()[0]
    return {"total_entries": total}
