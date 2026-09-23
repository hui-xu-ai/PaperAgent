# -*- coding: utf-8 -*-
"""0005：`papers_meta` 补 8 个派生列（AI 评分 2 个 + paperlit 元数据 6 个）。

背景（2026-09-23）：这两组列（2026-09-19 起由 L1 编译与 paperlit 清洗写入）此前住在
`db.py::init_schema` 里做**内联 ADD COLUMN 自愈**，违反本项目硬规则「表结构变更只允许在
迁移层」（`docs/VERSIONING.md` §3；守卫 `backend/tests/test_version_contract.py::
test_alter_table_only_in_migration_layer`）。这笔债一直登记在 `docs/COMPAT-REGISTER.md` A1，
**移除条件写的就是本版 v1.4.0**。它与 0004 是同一形状的隐患——存量库缺列却没有迁移，只靠
自愈兜着；0004 那次就真炸过（`messages has no column named reasoning_content`）。

幂等：表不存在（全新安装，`PAPERS_META_DDL` 已含全部列）或列已存在 → 不动。
**不重建表**：这 8 列都是纯增量（带默认值或可空），`ADD COLUMN` 比 0003 的"搬行换主键"安全得多。
schema 运行时解析（五库布局下 `papers_meta` 在 `biblio`；单库测试里在 `main`）——与 0003 同法，
**每份迁移自带这份小助手**（迁移是冻结的历史，不该跟着 `db.py` 一起漂）。
"""
from __future__ import annotations

import sqlite3

VERSION = 4
NAME = "0005_meta_score_lit_cols"

# 列 → DDL 后缀。**必须与 `db.py::PAPERS_META_DDL` 的当前格式逐字一致**，
# 否则新老库会长出不同类型的同名列。
_COLS: dict[str, str] = {
    "ai_value_score": "REAL",              # AI 价值评分 0-5（L1 编译产出）
    "topic_score": "REAL",                 # 主题匹配评分 0-1（L1 编译产出）
    "paper_rank": "REAL",                  # PaperRank（paperlit）
    "cocitation_cluster": "INTEGER",       # 共被引聚类 ID（paperlit）
    "impact_factor": "REAL",               # 期刊影响因子（paperlit 清洗填充）
    "quartile": "TEXT DEFAULT ''",         # JCR 分区 Q1-Q4（paperlit）
    "library_citations": "INTEGER DEFAULT 0",   # 库内被引次数（paperlit）
    "source_main": "TEXT DEFAULT ''",       # 主数据来源（wos/openalex/crossref/…）
}


def _schema_of(conn, table: str) -> str | None:
    for s in [r[1] for r in conn.execute("PRAGMA database_list")] or ["main"]:
        try:
            if conn.execute(f"SELECT 1 FROM {s}.sqlite_master WHERE type='table' AND name=?",
                            (table,)).fetchone():
                return s
        except sqlite3.Error:
            continue
    return None


def _cols(conn, schema: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA {schema}.table_info(papers_meta)")}


def up(conn) -> None:
    schema = _schema_of(conn, "papers_meta")
    if schema is None:
        return
    have = _cols(conn, schema)
    for col, ddl in _COLS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE {schema}.papers_meta ADD COLUMN {col} {ddl}")


def verify(conn) -> list[str]:
    schema = _schema_of(conn, "papers_meta")
    if schema is None:
        return []
    missing = set(_COLS) - _cols(conn, schema)
    if missing:
        return [f"papers_meta 缺列: {sorted(missing)}"]
    return []
