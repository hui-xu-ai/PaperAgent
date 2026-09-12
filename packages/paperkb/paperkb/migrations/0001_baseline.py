# -*- coding: utf-8 -*-
"""0001 基线迁移：把历史上"每次启动试探式补列"的内联迁移收编为一条编号迁移。

来源（原先散落在两处，属于 `docs/COMPAT-REGISTER.md` 的 A3/A4）：
  · `backend/app/services/store.py::_init_db` —— sessions.kind/mode/title、papers.run_id/
    pipeline_mode/md_template/pdf_md5/parse_source/pdf_name、sessions.paper_id 去 NOT NULL；
  · `packages/paperkb/paperkb/db.py::init_schema` —— papers_meta.journal_override/kind/
    corresponding_json、meta_fts 内容列迁移（`_migrate_meta_pk_to_rid` 仍在 db.py，由本迁移调用）。

设计：**幂等**（有列就跳过）、**跨库安全**（papers/papers_meta 在 biblio；sessions 在 chat；
settings/tasks 在 system → 用 `PRAGMA <schema>.table_info` 显式限定），
`verify()` 检查关键列是否齐（不齐 = 迁移没生效，必须报错而不是静默继续）。
VERSION=1 ⇒ 只对"data_format < 1 或缺失"的数据有意义；新装/已是最新时零开销。
"""
from __future__ import annotations

import sqlite3

VERSION = 1
NAME = "0001_baseline"

# (schema, table, column, ddl 片段)
_COLUMNS = [
    ("chat", "sessions", "kind", "TEXT DEFAULT 'paper'"),
    ("chat", "sessions", "mode", "TEXT DEFAULT ''"),
    ("chat", "sessions", "title", "TEXT DEFAULT ''"),
    ("biblio", "papers", "run_id", "TEXT DEFAULT ''"),
    ("biblio", "papers", "pipeline_mode", "TEXT DEFAULT 'full'"),
    ("biblio", "papers", "md_template", "TEXT DEFAULT ''"),
    ("biblio", "papers", "pdf_md5", "TEXT DEFAULT ''"),
    ("biblio", "papers", "parse_source", "TEXT DEFAULT ''"),
    ("biblio", "papers", "pdf_name", "TEXT DEFAULT ''"),
    ("biblio", "papers_meta", "journal_override", "TEXT DEFAULT ''"),
    ("biblio", "papers_meta", "kind", "TEXT DEFAULT ''"),
    ("biblio", "papers_meta", "corresponding_json", "TEXT DEFAULT '[]'"),
]


def _schemas(conn) -> list[str]:
    """已挂载的 schema 名（main + 附加库；顺序即 SQLite 的解析顺序）"""
    out = []
    try:
        for r in conn.execute("PRAGMA database_list"):
            out.append(r[1])
    except sqlite3.Error:
        pass
    return out or ["main"]


def _find_schema(conn, table: str) -> str | None:
    """表在哪个 schema（跨库布局下 papers 在 biblio、sessions 在 chat；单库测试里在 main）。

    **不硬编码库名**——这样迁移在单文件库（测试/离线工具）与五库布局下都能跑。
    """
    for s in _schemas(conn):
        try:
            row = conn.execute(f"SELECT 1 FROM {s}.sqlite_master WHERE type='table' "
                               f"AND name=?", (table,)).fetchone()
        except sqlite3.Error:
            continue
        if row is not None:
            return s
    return None


def _cols(conn, schema: str, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA {schema}.table_info({table})")}
    except sqlite3.Error:
        return set()


# 逻辑表 → 需要补的列（库名运行时解析，不写死）
_COLUMNS = [
    ("sessions", "kind", "TEXT DEFAULT 'paper'"),
    ("sessions", "mode", "TEXT DEFAULT ''"),
    ("sessions", "title", "TEXT DEFAULT ''"),
    ("papers", "run_id", "TEXT DEFAULT ''"),
    ("papers", "pipeline_mode", "TEXT DEFAULT 'full'"),
    ("papers", "md_template", "TEXT DEFAULT ''"),
    ("papers", "pdf_md5", "TEXT DEFAULT ''"),
    ("papers", "parse_source", "TEXT DEFAULT ''"),
    ("papers", "pdf_name", "TEXT DEFAULT ''"),
    ("papers_meta", "journal_override", "TEXT DEFAULT ''"),
    ("papers_meta", "kind", "TEXT DEFAULT ''"),
    ("papers_meta", "corresponding_json", "TEXT DEFAULT '[]'"),
]


def up(conn) -> None:
    for table, column, ddl in _COLUMNS:
        schema = _find_schema(conn, table)
        if schema is None:
            continue                      # 该库还没被创建（首次启动）→ 建表 DDL 已是最新
        if column in _cols(conn, schema, table):
            continue
        conn.execute(f"ALTER TABLE {schema}.{table} ADD COLUMN {column} {ddl}")

    # sessions.paper_id 的历史 NOT NULL 约束（多库布局后跨库 FK 不合法）→ 重建表
    schema = _find_schema(conn, "sessions")
    if schema:
        info = {r[1]: r for r in conn.execute(f"PRAGMA {schema}.table_info(sessions)")}
        if info.get("paper_id") and info["paper_id"][3]:
            conn.execute(f"""CREATE TABLE IF NOT EXISTS {schema}.sessions_new (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                paper_id INTEGER,
                                kind TEXT DEFAULT 'paper',
                                created_at TEXT)""")
            # 只搬老表**真实存在**的列（极端老库可能缺 created_at）
            have = {c for c in ("id", "paper_id", "kind", "created_at")
                    if c in _cols(conn, schema, "sessions")}
            sel = ", ".join(("COALESCE(kind,'paper')" if c == "kind" else c) for c in sorted(have))
            conn.execute(f"INSERT INTO {schema}.sessions_new({', '.join(sorted(have))}) "
                         f"SELECT {sel} FROM {schema}.sessions")
            conn.execute(f"DROP TABLE {schema}.sessions")
            conn.execute(f"ALTER TABLE {schema}.sessions_new RENAME TO sessions")
            for col, ddl in (("mode", "TEXT DEFAULT ''"), ("title", "TEXT DEFAULT ''")):
                conn.execute(f"ALTER TABLE {schema}.sessions ADD COLUMN {col} {ddl}")


def verify(conn) -> list[str]:
    """关键列必须存在（缺失 = 迁移没生效）。"""
    problems: list[str] = []
    for table, column, _ in _COLUMNS:
        schema = _find_schema(conn, table)
        if schema is None:
            continue
        if column not in _cols(conn, schema, table):
            problems.append(f"{schema}.{table}.{column} 缺失")
    return problems
