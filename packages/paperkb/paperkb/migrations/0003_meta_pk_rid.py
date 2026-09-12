# -*- coding: utf-8 -*-
"""0003：`papers_meta` 主键 `doi → rid`（从 `db.py::_migrate_meta_pk_to_rid` 收编）。

为什么要换（P0-B，2026-09-11）：doi 作主键时"无 DOI 资料"（中文文献/书/学位论文）无法入库——
第二行 `doi=''` 就撞主键；换 rid 后 doi 只是外部标识之一（登记进 `identifiers`）。

实现：行数据在 Python 侧搬运（表小；rid 需按规则计算）。**schema 运行时解析**
（五库布局下 papers_meta 在 biblio；单库测试里在 main）。幂等：已有 `rid` 列即跳过。
注意：为保持迁移 runner 的**事务完整性**，这里**不用** `executescript`（它会隐式提交），
而是显式 `CREATE TABLE <schema>.papers_meta`。
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime

from ..db import _META_COLS, PAPERS_META_DDL
from ..doi import is_doi, make_rid

VERSION = 2      # 与 0002 同为 "data_format 1 → 2" 的步骤（一个格式可含多条迁移；
                 # runner 按 VERSION 升序、同版本内按文件名顺序执行）
NAME = "0003_meta_pk_rid"


def _schema_of(conn, table: str) -> str | None:
    for s in [r[1] for r in conn.execute("PRAGMA database_list")] or ["main"]:
        try:
            if conn.execute(f"SELECT 1 FROM {s}.sqlite_master WHERE type='table' AND name=?",
                            (table,)).fetchone():
                return s
        except sqlite3.Error:
            continue
    return None


def up(conn) -> None:
    schema = _schema_of(conn, "papers_meta")
    if schema is None:
        return
    cols = {r[1] for r in conn.execute(f"PRAGMA {schema}.table_info(papers_meta)")}
    if "rid" in cols:
        return                                   # 已是新格式
    rows = [dict(r) for r in conn.execute(f"SELECT * FROM {schema}.papers_meta").fetchall()]
    conn.execute(f"DROP TABLE {schema}.papers_meta")
    conn.execute(PAPERS_META_DDL.replace("CREATE TABLE IF NOT EXISTS papers_meta",
                                         f"CREATE TABLE IF NOT EXISTS {schema}.papers_meta"))
    conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {schema}.idx_meta_doi "
                 f"ON papers_meta(doi) WHERE doi <> ''")
    now = datetime.now().isoformat(timespec="seconds")
    for row in rows:
        doi = (row.get("doi") or "").strip()
        if is_doi(doi):
            rid = make_rid("paper", doi=doi)
        else:
            rid = make_rid("paper", report_no=row.get("wos_id") or "",
                           fingerprint=hashlib.md5(
                               ((row.get("title") or "") + "|" + (row.get("year") or ""))
                               .encode("utf-8")).hexdigest())
        vals = {c: row.get(c) for c in _META_COLS if c != "rid"}
        vals["rid"] = rid
        vals["doi"] = doi
        keys = list(vals)
        conn.execute("INSERT OR REPLACE INTO %s.papers_meta(%s) VALUES(%s)"
                     % (schema, ",".join(keys), ",".join("?" * len(keys))),
                     [vals[k] for k in keys])
        if is_doi(doi) and _schema_of(conn, "identifiers"):
            ids = _schema_of(conn, "identifiers")
            conn.execute(f"INSERT OR REPLACE INTO {ids}.identifiers"
                         f"(kind,value,rid,created_at) VALUES('doi',?,?,?)", (doi, rid, now))


def verify(conn) -> list[str]:
    schema = _schema_of(conn, "papers_meta")
    if schema is None:
        return []
    cols = {r[1] for r in conn.execute(f"PRAGMA {schema}.table_info(papers_meta)")}
    return [] if "rid" in cols else ["papers_meta 仍以 doi 为主键（缺 rid 列）"]
