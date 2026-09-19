# -*- coding: utf-8 -*-
"""期刊指标库（journals.db，★独立 SQLite 文件）。

- 与主库解耦：用户重新提供 JCR 表格 = 整体更新（upsert），换年份表格可整体替换
- jcr 表：JCR 影响因子/分区（WOS 体系）；cas 表：中科院分区（含 Top/OA）
- 查询：journals_for(journal_name, year) 名称规范化匹配（大小写/全半角）
"""
from __future__ import annotations

import re
import sqlite3
import threading
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
    """独立期刊库访问（数据层）。

    2026-09-19 性能修复：jcr 表 22k+ 行，旧 `lookup`/`lookup_issn` 每次
    `SELECT * FROM jcr ORDER BY year DESC` 把全表拉进 Python 逐行 `norm_journal_name`
    （0.12-0.2s/次、每次新建连接、`_with_cas` 再开一条）；`kb/list` 对每篇做 ≤3 次
    期刊匹配 ⇒ 知识库列表请求被拖到分钟级（用户报"切知识库 Tab 卡 2 分钟"）。
    改为**进程内缓存 + 预建索引**（norm_name / issn / eissn / cas_by_name），
    单例 `_journals` 生命周期内只全表加载一次，之后每次查询 O(1) 字典命中；
    upsert（用户更新 JCR/CAS 表）时失效缓存。多线程惰性加载用锁保护。
    """

    def __init__(self, roots: Roots):
        self.db_path = roots.journals_db
        self._cache: dict | None = None
        self._cache_lock = threading.Lock()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def init_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    # ---------------------------------------------------------- 缓存
    def invalidate_cache(self) -> None:
        """失效内存缓存（upsert 更新 JCR/CAS 表后调用）。"""
        with self._cache_lock:
            self._cache = None

    def _ensure_cache(self) -> dict:
        """惰性加载 jcr/cas 全表 → 预建索引（进程内仅一次；线程安全）。"""
        with self._cache_lock:
            if self._cache is not None:
                return self._cache
            jcr_by_name: dict[str, list[dict]] = {}
            jcr_by_issn: dict[str, dict] = {}
            jcr_by_eissn: dict[str, dict] = {}
            cas_by_name: dict[str, list[dict]] = {}
            try:
                with self._conn() as conn:
                    jcr_rows = conn.execute(
                        "SELECT * FROM jcr ORDER BY year DESC").fetchall()
                    cas_rows = conn.execute(
                        "SELECT * FROM cas ORDER BY year DESC").fetchall()
            except sqlite3.Error:
                jcr_rows, cas_rows = [], []
            for r in jcr_rows:
                d = dict(r)
                nm = norm_journal_name(d.get("journal_name", ""))
                if nm:
                    jcr_by_name.setdefault(nm, []).append(d)   # 已按 year DESC 排序
                issn = str(d.get("issn") or "").strip().upper()
                eissn = str(d.get("eissn") or "").strip().upper()
                if issn:
                    jcr_by_issn.setdefault(issn, d)            # 首条=最新年
                if eissn:
                    jcr_by_eissn.setdefault(eissn, d)
            for r in cas_rows:
                d = dict(r)
                cas_by_name.setdefault(d.get("journal_name", ""), []).append(d)
            self._cache = {"jcr_by_name": jcr_by_name, "jcr_by_issn": jcr_by_issn,
                           "jcr_by_eissn": jcr_by_eissn, "cas_by_name": cas_by_name}
            return self._cache

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
        self.invalidate_cache()
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
        self.invalidate_cache()
        return len(rows)

    # ---------------------------------------------------------- 查询
    def lookup_issn(self, issn: str, eissn: str = "") -> dict | None:
        """按 ISSN/eISSN 精确匹配（bib 带 ISSN → 首选键，期刊名缩写问题绕开）。"""
        issn = (issn or "").strip().upper()
        eissn = (eissn or "").strip().upper()
        if not issn and not eissn:
            return None
        c = self._ensure_cache()
        row = (issn and c["jcr_by_issn"].get(issn)) or \
              (eissn and c["jcr_by_eissn"].get(eissn))
        return self._with_cas(dict(row)) if row else None

    def lookup(self, journal_name: str, year: int | None = None) -> dict | None:
        """按期刊名（规范化匹配）查最新年份指标：{jcr: {...}|None, cas: {...}|None}。"""
        norm = norm_journal_name(journal_name)
        if not norm:
            return None
        rows = self._ensure_cache()["jcr_by_name"].get(norm)
        if not rows:
            return None
        return self._with_cas(dict(rows[0]), year=year)   # rows 已按 year DESC

    def _with_cas(self, jcr_row: dict, year: int | None = None) -> dict:
        """jcr 行 + 关联 cas 行 → 结果 dict（year 指定则取对应年份）。"""
        cas_rows = self._ensure_cache()["cas_by_name"].get(jcr_row["journal_name"], [])
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
