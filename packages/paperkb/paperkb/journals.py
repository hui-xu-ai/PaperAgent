# -*- coding: utf-8 -*-
"""期刊指标库（journals.db，★独立 SQLite 文件）。

- 与主库解耦：用户重新提供 JCR 表格 = 整体更新（upsert），换年份表格可整体替换
- jcr 表：JCR 影响因子/分区（WOS 体系）；cas 表：中科院分区（含 Top/OA）
- 查询：journals_for(journal_name, year) 名称规范化匹配（大小写/全半角）
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path

from .config import Roots

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jcr (
    journal_name TEXT NOT NULL,
    jif REAL DEFAULT 0,
    quartile TEXT DEFAULT '',
    jif_rank TEXT DEFAULT '',
    zone_2023 TEXT DEFAULT '',
    total_citation INTEGER DEFAULT 0,
    category TEXT DEFAULT '',
    issn TEXT DEFAULT '',
    eissn TEXT DEFAULT '',
    year INTEGER DEFAULT 0,
    updated_at TEXT DEFAULT '',
    PRIMARY KEY (journal_name, year)
);
CREATE INDEX IF NOT EXISTS idx_jcr_issn ON jcr(issn);
CREATE TABLE IF NOT EXISTS cas (
    journal_name TEXT NOT NULL,
    zone INTEGER DEFAULT 0,
    is_top TEXT DEFAULT '',
    is_oa TEXT DEFAULT '',
    year INTEGER DEFAULT 0,
    updated_at TEXT DEFAULT '',
    PRIMARY KEY (journal_name, year)
);
"""


def norm_journal_name(name: str) -> str:
    """期刊名规范化（匹配用）：大写 + 去空白/标点差异。"""
    if not name:
        return ""
    s = name.upper()
    s = re.sub(r"[\s\-_.,;:'\"()&/]+", "", s)
    return s


def _to_float(v) -> float:
    """容错转 float：'<0.1'（JCR 极低 IF）、'-'、空、千分位逗号。"""
    try:
        s = str(v).strip().replace(",", "")
        if not s or s in ("-", "—"):
            return 0.0
        if s.startswith("<"):
            return 0.0
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def _to_int(v) -> int:
    try:
        s = str(v).strip().replace(",", "")
        if not s or s in ("-", "—"):
            return 0
        return int(float(s))
    except (TypeError, ValueError):
        return 0


class JournalsDB:
    """独立期刊库访问（数据层）。"""

    def __init__(self, roots: Roots):
        self.db_path = roots.journals_db

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def init_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    # ---------------------------------------------------------- upsert
    def upsert_jcr(self, rows: list[dict]) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        with self._conn() as conn:
            for r in rows:
                conn.execute(
                    """INSERT OR REPLACE INTO jcr(
                        journal_name,jif,quartile,jif_rank,zone_2023,total_citation,
                        category,issn,eissn,year,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (r.get("journal_name", ""), _to_float(r.get("jif")),
                     r.get("quartile", ""), r.get("jif_rank", ""),
                     r.get("zone_2023", ""), _to_int(r.get("total_citation")),
                     r.get("category", ""), r.get("issn", ""), r.get("eissn", ""),
                     _to_int(r.get("year")), now))
        return len(rows)

    def upsert_cas(self, rows: list[dict]) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        with self._conn() as conn:
            for r in rows:
                conn.execute(
                    """INSERT OR REPLACE INTO cas(
                        journal_name,zone,is_top,is_oa,year,updated_at)
                       VALUES(?,?,?,?,?,?)""",
                    (r.get("journal_name", ""), _to_int(r.get("zone")),
                     r.get("is_top", ""), r.get("is_oa", ""),
                     _to_int(r.get("year")), now))
        return len(rows)

    # ---------------------------------------------------------- 查询
    def lookup_issn(self, issn: str, eissn: str = "") -> dict | None:
        """按 ISSN/eISSN 精确匹配（bib 带 ISSN → 首选键，期刊名缩写问题绕开）。"""
        issn = (issn or "").strip().upper()
        eissn = (eissn or "").strip().upper()
        if not issn and not eissn:
            return None
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM jcr ORDER BY year DESC").fetchall()
        for r in rows:
            if issn and str(r["issn"] or "").strip().upper() == issn:
                return self._with_cas(dict(r))
            if eissn and str(r["eissn"] or "").strip().upper() == eissn:
                return self._with_cas(dict(r))
        return None

    def lookup(self, journal_name: str, year: int | None = None) -> dict | None:
        """按期刊名（规范化匹配）查最新年份指标：{jcr: {...}|None, cas: {...}|None}。"""
        norm = norm_journal_name(journal_name)
        if not norm:
            return None
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM jcr ORDER BY year DESC").fetchall()
        jcr_rows = [r for r in rows if norm_journal_name(r["journal_name"]) == norm]
        if not jcr_rows:
            return None
        return self._with_cas(dict(jcr_rows[0]), year=year)

    def _with_cas(self, jcr_row: dict, year: int | None = None) -> dict:
        """jcr 行 + 关联 cas 行 → 结果 dict（year 指定则取对应年份）。"""
        with self._conn() as conn:
            cas_rows = conn.execute(
                "SELECT * FROM cas WHERE journal_name=? ORDER BY year DESC",
                (jcr_row["journal_name"],)).fetchall()
        result: dict = {"jcr": jcr_row, "cas": None}
        for r in cas_rows:
            if year is None or r["year"] == year:
                result["cas"] = dict(r)
                break
        if result["cas"] is None and cas_rows:
            result["cas"] = dict(cas_rows[0])
        return result

    def stats(self) -> dict:
        with self._conn() as conn:
            j = conn.execute("SELECT COUNT(*) c, COUNT(DISTINCT year) y FROM jcr").fetchone()
            c = conn.execute("SELECT COUNT(*) c, COUNT(DISTINCT year) y FROM cas").fetchone()
        return {"jcr_rows": j["c"], "jcr_years": j["y"],
                "cas_rows": c["c"], "cas_years": c["y"]}
