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
    if rules.get("non_wos_record"):
        # 非真正 WoS 记录：来源非 wos，或缺摘要的空存根（WoS 流程最终无法补全者）
        conditions.append(
            "(source_main NOT LIKE 'wos%' OR abstract IS NULL OR abstract = '')"
        )
    return conditions, params


def _scope_cond(rules: dict[str, Any]) -> str:
    """作用范围条件：默认仅参考文献；include_mains=True 时含主文献（危险 opt-in）。"""
    return "1=1" if rules.get("include_mains") else "is_reference=1"


def _guarded_where(rules: dict[str, Any]) -> tuple[str, list] | None:
    """带作用范围的完整 WHERE 子句；无有效规则返回 None。

    默认形如 ``is_reference=1 AND (cond1 OR cond2 ...)``——只清洗参考文献，
    主文献永远保留；仅当显式 include_mains 时才放开主文献。
    """
    conditions, params = _rule_conditions(rules)
    if not conditions:
        return None
    return f"{_scope_cond(rules)} AND (" + " OR ".join(conditions) + ")", params


def attach_journal_metrics(store: LitStore, journals_db_path: Path | str) -> dict:
    """关联期刊指标（影响因子、分区）到 papers 表。

    算法：ATTACH JCR 库 → 按期刊名聚合取最新年份到临时表（建唯一索引）→
    单条 ``UPDATE ... WHERE EXISTS`` 连接完成全部回写，O(N log M)，
    50 万篇规模仍为秒级。（旧实现逐期刊循环 UPDATE、每次全表扫描，
    期刊数×文献数 复杂度，大规模下不可用。）

    Args:
        store: LitStore 实例
        journals_db_path: journals.db 路径

    Returns:
        {"matched": int, "unmatched": int, "total": int, "updated_rows": int}
    """
    journals_db_path = Path(journals_db_path)
    if not journals_db_path.exists():
        raise FileNotFoundError(f"journals.db not found: {journals_db_path}")

    with store._conn() as conn:
        conn.execute("ATTACH ? AS jcrdb", (str(journals_db_path),))
        try:
            conn.execute("""
                CREATE TEMP TABLE jcr_latest AS
                SELECT jname, jif, quartile FROM (
                    SELECT UPPER(journal_name) AS jname, jif, quartile,
                           ROW_NUMBER() OVER (PARTITION BY UPPER(journal_name)
                                              ORDER BY year DESC) AS rn
                    FROM jcrdb.jcr
                ) WHERE rn = 1
            """)
            conn.execute(
                "CREATE UNIQUE INDEX jcr_latest_jname ON jcr_latest(jname)")
            conn.execute("""
                UPDATE papers SET
                  impact_factor = (SELECT m.jif FROM jcr_latest m
                                   WHERE m.jname = UPPER(papers.journal)),
                  quartile      = (SELECT m.quartile FROM jcr_latest m
                                   WHERE m.jname = UPPER(papers.journal))
                WHERE papers.journal != ''
                  AND EXISTS (SELECT 1 FROM jcr_latest m
                              WHERE m.jname = UPPER(papers.journal))
            """)
            updated = conn.execute("SELECT changes()").fetchone()[0]
            total = conn.execute(
                "SELECT COUNT(*) FROM (SELECT DISTINCT UPPER(journal) j "
                "FROM papers WHERE journal != '')").fetchone()[0]
            matched = conn.execute(
                "SELECT COUNT(*) FROM (SELECT DISTINCT UPPER(journal) j "
                "FROM papers WHERE journal != '') "
                "WHERE j IN (SELECT jname FROM jcr_latest)").fetchone()[0]
        finally:
            conn.execute("DETACH jcrdb")

    logger.info("attach_journal_metrics: %d/%d journals matched, %d rows updated",
                matched, total, updated)
    return {"matched": matched, "unmatched": total - matched, "total": total,
            "updated_rows": updated}


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
                "non_wos_record": True,
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
                "non_wos_filter": int,
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

        # 分项统计（与 _guarded_where 同范围：默认仅参考文献）
        scope = _scope_cond(rules)
        breakdown = {}
        if "year" in rules and "min" in rules["year"]:
            breakdown["year_filter"] = conn.execute(
                f"SELECT COUNT(*) FROM papers WHERE {scope} "
                "AND CAST(year AS INTEGER) < ?",
                (rules["year"]["min"],)).fetchone()[0]
        if "impact_factor" in rules and "min" in rules["impact_factor"]:
            breakdown["if_filter"] = conn.execute(
                f"SELECT COUNT(*) FROM papers WHERE {scope} "
                "AND impact_factor < ?",
                (rules["impact_factor"]["min"],)).fetchone()[0]
        if "quartile" in rules and "keep" in rules["quartile"]:
            keep_list = rules["quartile"]["keep"]
            placeholders = ",".join(["?"] * len(keep_list))
            breakdown["quartile_filter"] = conn.execute(
                f"SELECT COUNT(*) FROM papers WHERE {scope} "
                f"AND quartile NOT IN ({placeholders})",
                keep_list).fetchone()[0]
        if "library_citations" in rules and "min" in rules["library_citations"]:
            breakdown["citation_filter"] = conn.execute(
                f"SELECT COUNT(*) FROM papers WHERE {scope} "
                "AND library_citations < ?",
                (rules["library_citations"]["min"],)).fetchone()[0]
        if rules.get("non_wos_record"):
            breakdown["non_wos_filter"] = conn.execute(
                f"SELECT COUNT(*) FROM papers WHERE {scope} "
                "AND (source_main NOT LIKE 'wos%' OR abstract IS NULL OR abstract = '')"
            ).fetchone()[0]

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
                "source_main": row["source_main"],
                "abstract_missing": not row["abstract"],
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
            # FTS 无删除触发器，须同步清理，否则残留孤儿索引行
            conn.execute(f"""
                DELETE FROM papers_fts
                WHERE doi IN (SELECT doi FROM papers WHERE {where_clause})
            """, params)
            conn.execute(f"DELETE FROM papers WHERE {where_clause}", params)
        elif mode == "mark":
            # is_cleaned 列由 _SCHEMA 声明
            conn.execute(f"UPDATE papers SET is_cleaned = 1 WHERE {where_clause}", params)
        else:
            raise ValueError(f"Invalid mode: {mode}")

        remaining = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]

    logger.info("execute_cleaning: removed %d, remaining %d, mode=%s", to_remove, remaining, mode)
    return {"removed": to_remove, "remaining": remaining, "mode": mode}
