# -*- coding: utf-8 -*-
"""文献清洗：基于现有数据（year, journal, library_citations, impact_factor, quartile）的初步清洗。

清洗流程：
1. 关联期刊指标（journals.db/jcr）→ impact_factor, quartile
2. 计算库内引用（citations 表）→ library_citations
3. 预览清洗结果（不实际删除）
4. 执行清洗（删除或标记）
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from ..db import LitStore

logger = logging.getLogger(__name__)


def _rule_conditions(rules: dict[str, Any]) -> tuple[list[str], list]:
    """把清洗规则翻译成 OR 连接的 SQL 条件 + 参数（命中任一即「待移除」）。

    返回 ([], []) 表示无有效规则。条件**不含**主文献保护，由调用方包一层
    ``is_reference=1 AND (...)``——主文献（用户导入的核心论文，is_reference=0）
    在库内通常 library_citations=0，绝不可被清洗误删。
    """
    conditions: list[str] = []
    params: list = []
    if "year" in rules and "min" in rules["year"]:
        conditions.append("CAST(year AS INTEGER) < ?")
        params.append(rules["year"]["min"])
    if "impact_factor" in rules and "min" in rules["impact_factor"]:
        conditions.append("impact_factor < ?")
        params.append(rules["impact_factor"]["min"])
    if "quartile" in rules and "keep" in rules["quartile"]:
        keep_list = rules["quartile"]["keep"]
        placeholders = ",".join(["?"] * len(keep_list))
        conditions.append(f"quartile NOT IN ({placeholders})")
        params.extend(keep_list)
    if "library_citations" in rules and "min" in rules["library_citations"]:
        conditions.append("library_citations < ?")
        params.append(rules["library_citations"]["min"])
    return conditions, params


def _guarded_where(rules: dict[str, Any]) -> tuple[str, list] | None:
    """带主文献保护的完整 WHERE 子句；无有效规则返回 None。

    形如 ``is_reference=1 AND (cond1 OR cond2 ...)``——只清洗参考文献，
    主文献永远保留。
    """
    conditions, params = _rule_conditions(rules)
    if not conditions:
        return None
    return "is_reference=1 AND (" + " OR ".join(conditions) + ")", params


def attach_journal_metrics(store: LitStore, journals_db_path: Path | str) -> dict:
    """关联期刊指标（影响因子、分区）到 papers 表。

    Args:
        store: LitStore 实例
        journals_db_path: journals.db 路径

    Returns:
        {"matched": int, "unmatched": int, "total": int}
    """
    journals_db_path = Path(journals_db_path)
    if not journals_db_path.exists():
        raise FileNotFoundError(f"journals.db not found: {journals_db_path}")

    # 读取 JCR 数据（取最新年份）
    jcr_conn = sqlite3.connect(journals_db_path)
    jcr_data = {}
    try:
        rows = jcr_conn.execute("""
            SELECT journal_name, jif, quartile, year
            FROM jcr
            ORDER BY year DESC
        """).fetchall()
        for journal_name, jif, quartile, year in rows:
            # 只保留每个期刊的最新记录
            if journal_name not in jcr_data:
                jcr_data[journal_name.upper()] = {
                    "jif": jif,
                    "quartile": quartile,
                    "year": year,
                }
    finally:
        jcr_conn.close()

    logger.info("Loaded %d journal metrics from JCR", len(jcr_data))

    # 更新 papers 表（impact_factor/quartile 列由 _SCHEMA 声明）
    matched = 0
    unmatched = 0
    with store._conn() as conn:
        # 获取所有唯一期刊名
        journals = conn.execute(
            "SELECT DISTINCT journal FROM papers WHERE journal != ''"
        ).fetchall()

        for (journal,) in journals:
            journal_upper = journal.upper()
            metrics = jcr_data.get(journal_upper)
            if metrics:
                conn.execute(
                    "UPDATE papers SET impact_factor = ?, quartile = ? WHERE journal = ?",
                    (metrics["jif"], metrics["quartile"], journal),
                )
                matched += 1
            else:
                unmatched += 1

    logger.info("attach_journal_metrics: %d matched, %d unmatched", matched, unmatched)
    return {"matched": matched, "unmatched": unmatched, "total": matched + unmatched}


def compute_library_citations(store: LitStore) -> dict:
    """计算每篇文献在库内被引用的次数。

    Returns:
        {"computed": int, "with_citations": int}
    """
    with store._conn() as conn:
        # 重置所有计数
        conn.execute("UPDATE papers SET library_citations = 0")

        # 从 citations 表统计
        citation_counts = conn.execute("""
            SELECT cited_doi, COUNT(*) as cnt
            FROM citations
            GROUP BY cited_doi
        """).fetchall()

        computed = 0
        with_citations = 0
        for cited_doi, cnt in citation_counts:
            conn.execute(
                "UPDATE papers SET library_citations = ? WHERE doi = ?",
                (cnt, cited_doi),
            )
            computed += 1
            if cnt > 0:
                with_citations += 1

    logger.info("compute_library_citations: %d papers with citations", with_citations)
    return {"computed": computed, "with_citations": with_citations}


def preview_cleaning(store: LitStore, rules: dict[str, Any]) -> dict:
    """预览清洗结果（不实际删除）。

    Args:
        store: LitStore 实例
        rules: 清洗规则，例如：
            {
                "year": {"min": 2015},
                "impact_factor": {"min": 3.0},
                "quartile": {"keep": ["Q1", "Q2"]},
                "library_citations": {"min": 2},
            }

    Returns:
        {
            "total": int,
            "to_remove": int,
            "to_keep": int,
            "breakdown": {
                "year_filter": int,
                "if_filter": int,
                "quartile_filter": int,
                "citation_filter": int,
            },
            "samples": [{"doi": str, "title": str, "year": str, ...}, ...]
        }
    """
    guarded = _guarded_where(rules)
    with store._conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]

        if guarded is None:
            return {
                "total": total,
                "to_remove": 0,
                "to_keep": total,
                "breakdown": {},
                "samples": [],
            }

        where_clause, params = guarded
        to_remove_rows = conn.execute(
            f"SELECT * FROM papers WHERE {where_clause}", params).fetchall()
        to_remove = len(to_remove_rows)
        to_keep = total - to_remove

        # 分项统计（同样只数参考文献，主文献恒保留）
        breakdown = {}
        if "year" in rules and "min" in rules["year"]:
            breakdown["year_filter"] = conn.execute(
                "SELECT COUNT(*) FROM papers WHERE is_reference=1 "
                "AND CAST(year AS INTEGER) < ?",
                (rules["year"]["min"],)).fetchone()[0]
        if "impact_factor" in rules and "min" in rules["impact_factor"]:
            breakdown["if_filter"] = conn.execute(
                "SELECT COUNT(*) FROM papers WHERE is_reference=1 "
                "AND impact_factor < ?",
                (rules["impact_factor"]["min"],)).fetchone()[0]
        if "quartile" in rules and "keep" in rules["quartile"]:
            keep_list = rules["quartile"]["keep"]
            placeholders = ",".join(["?"] * len(keep_list))
            breakdown["quartile_filter"] = conn.execute(
                f"SELECT COUNT(*) FROM papers WHERE is_reference=1 "
                f"AND quartile NOT IN ({placeholders})",
                keep_list).fetchone()[0]
        if "library_citations" in rules and "min" in rules["library_citations"]:
            breakdown["citation_filter"] = conn.execute(
                "SELECT COUNT(*) FROM papers WHERE is_reference=1 "
                "AND library_citations < ?",
                (rules["library_citations"]["min"],)).fetchone()[0]

        # 样本（前 20 条）
        samples = []
        for row in to_remove_rows[:20]:
            samples.append({
                "doi": row["doi"],
                "title": row["title"][:60] if row["title"] else "",
                "year": row["year"],
                "journal": row["journal"],
                "impact_factor": row["impact_factor"],
                "quartile": row["quartile"],
                "library_citations": row["library_citations"],
            })

    return {
        "total": total,
        "to_remove": to_remove,
        "to_keep": to_keep,
        "breakdown": breakdown,
        "samples": samples,
    }


def execute_cleaning(store: LitStore, rules: dict[str, Any], mode: str = "delete") -> dict:
    """执行清洗（删除或标记文献）。

    Args:
        store: LitStore 实例
        rules: 清洗规则（同 preview_cleaning）
        mode: "delete"（删除）或 "mark"（标记 is_cleaned=1）

    Returns:
        {"removed": int, "remaining": int, "mode": str}
    """
    preview = preview_cleaning(store, rules)
    to_remove = preview["to_remove"]

    if to_remove == 0:
        return {"removed": 0, "remaining": preview["total"], "mode": mode}

    guarded = _guarded_where(rules)
    if guarded is None:
        return {"removed": 0, "remaining": preview["total"], "mode": mode}
    where_clause, params = guarded

    with store._conn() as conn:
        if mode == "delete":
            # 先删引用边再删论文：否则 papers 已空，子查询匹配不到 → 残留孤立 citations。
            conn.execute(f"""
                DELETE FROM citations
                WHERE citing_doi IN (SELECT doi FROM papers WHERE {where_clause})
                   OR cited_doi IN (SELECT doi FROM papers WHERE {where_clause})
            """, params + params)
            conn.execute(f"DELETE FROM papers WHERE {where_clause}", params)
        elif mode == "mark":
            # is_cleaned 列由 _SCHEMA 声明
            conn.execute(f"UPDATE papers SET is_cleaned = 1 WHERE {where_clause}", params)
        else:
            raise ValueError(f"Invalid mode: {mode}")

        remaining = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]

    logger.info("execute_cleaning: removed %d, remaining %d, mode=%s", to_remove, remaining, mode)
    return {"removed": to_remove, "remaining": remaining, "mode": mode}
