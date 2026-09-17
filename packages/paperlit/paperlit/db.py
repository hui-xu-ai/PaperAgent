# -*- coding: utf-8 -*-
"""文献检索库存储层（lit.db）。

独立 SQLite，与 paperkb 的 biblio.db 通过 DOI 松耦合。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from .config import Roots
from .models import Citation, Paper, SearchResult

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    doi TEXT PRIMARY KEY,
    title TEXT DEFAULT '',
    abstract TEXT DEFAULT '',
    authors_json TEXT DEFAULT '[]',
    affiliations_json TEXT DEFAULT '[]',
    corresponding_json TEXT DEFAULT '[]',
    journal TEXT DEFAULT '',
    year TEXT DEFAULT '',
    issn TEXT DEFAULT '',
    eissn TEXT DEFAULT '',
    keywords_json TEXT DEFAULT '[]',
    research_areas_json TEXT DEFAULT '[]',
    wos_categories_json TEXT DEFAULT '[]',
    times_cited INTEGER DEFAULT 0,
    wos_id TEXT DEFAULT '',
    references_json TEXT DEFAULT '[]',
    source_main TEXT DEFAULT '',
    source_file TEXT DEFAULT '',
    source_abstract TEXT DEFAULT '',
    source_authors TEXT DEFAULT '',
    is_reference INTEGER DEFAULT 0,
    is_enriched INTEGER DEFAULT 0,
    imported_at TEXT DEFAULT '',
    enriched_at TEXT DEFAULT '',
    paper_rank REAL DEFAULT 0.0,
    cocitation_cluster INTEGER DEFAULT 0,
    impact_factor REAL DEFAULT 0,
    quartile TEXT DEFAULT '',
    library_citations INTEGER DEFAULT 0,
    is_cleaned INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_papers_year ON papers(year);
CREATE INDEX IF NOT EXISTS idx_papers_journal ON papers(journal);
CREATE INDEX IF NOT EXISTS idx_papers_is_ref ON papers(is_reference);
CREATE INDEX IF NOT EXISTS idx_papers_source ON papers(source_main);
CREATE INDEX IF NOT EXISTS idx_papers_rank ON papers(paper_rank DESC);

CREATE TABLE IF NOT EXISTS citations (
    citing_doi TEXT NOT NULL,
    cited_doi TEXT NOT NULL,
    source TEXT DEFAULT '',
    cited_brief TEXT DEFAULT '',
    PRIMARY KEY (citing_doi, cited_doi)
);

CREATE INDEX IF NOT EXISTS idx_cit_cited ON citations(cited_doi);
CREATE INDEX IF NOT EXISTS idx_cit_citing ON citations(citing_doi);

CREATE TABLE IF NOT EXISTS paper_ranks (
    doi TEXT PRIMARY KEY,
    paper_rank REAL DEFAULT 0.0,
    in_degree INTEGER DEFAULT 0,
    out_degree INTEGER DEFAULT 0,
    computed_at TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS query_cache (
    query_hash TEXT PRIMARY KEY,
    query TEXT DEFAULT '',
    query_expanded TEXT DEFAULT '',
    result_dois_json TEXT DEFAULT '[]',
    created_at TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS topic_cache (
    topic_slug TEXT PRIMARY KEY,
    topic_name TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    paper_dois_json TEXT DEFAULT '[]',
    created_at TEXT DEFAULT '',
    updated_at TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS embeddings_meta (
    doi TEXT PRIMARY KEY,
    embedding_idx INTEGER DEFAULT -1,
    updated_at TEXT DEFAULT ''
);

CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts USING fts5(
    doi, title, abstract, authors, journal, keywords, year,
    tokenize='trigram'
);

-- 已退役的「渐进式交付会话」审计表（功能 2026-09-18 移除）：清掉旧库残留。
DROP TABLE IF EXISTS search_log;
"""


class LitStore:
    """文献检索库存储。"""

    def __init__(self, roots: Roots):
        self.roots = roots
        self.db_path = roots.lit_db

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    def stats(self) -> dict:
        with self._conn() as conn:
            papers = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
            refs = conn.execute(
                "SELECT COUNT(*) FROM papers WHERE is_reference=1"
            ).fetchone()[0]
            citations = conn.execute(
                "SELECT COUNT(*) FROM citations"
            ).fetchone()[0]
            enriched = conn.execute(
                "SELECT COUNT(*) FROM papers WHERE is_enriched=1"
            ).fetchone()[0]
        return {
            "total_papers": papers,
            "main_papers": papers - refs,
            "reference_papers": refs,
            "citations": citations,
            "enriched": enriched,
            "db_path": str(self.db_path),
        }

    def paper_count(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]

    def citation_count(self) -> int:
        with self._conn() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM citations"
            ).fetchone()[0]

    def ranked_count(self) -> int:
        """已计算 PaperRank（paper_rank>0）的文献数。"""
        with self._conn() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM papers WHERE paper_rank > 0"
            ).fetchone()[0]

    # ---- Paper CRUD ----

    def upsert_paper(self, paper: Paper) -> bool:
        """插入或更新文献。返回 True 表示新增，False 表示更新。"""
        now = datetime.now().isoformat(timespec="seconds")
        if not paper.imported_at:
            paper.imported_at = now

        with self._conn() as conn:
            existing = conn.execute(
                "SELECT doi FROM papers WHERE doi=?", (paper.doi,)
            ).fetchone()
            is_new = existing is None

            conn.execute("""
                INSERT OR REPLACE INTO papers
                (doi, title, abstract, authors_json, affiliations_json,
                 corresponding_json, journal, year, issn, eissn,
                 keywords_json, research_areas_json, wos_categories_json,
                 times_cited, wos_id, references_json,
                 source_main, source_file, source_abstract, source_authors,
                 is_reference, is_enriched, imported_at, enriched_at,
                 paper_rank, cocitation_cluster)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                paper.doi, paper.title, paper.abstract,
                json.dumps(paper.authors, ensure_ascii=False),
                json.dumps(paper.affiliations, ensure_ascii=False),
                json.dumps(paper.corresponding, ensure_ascii=False),
                paper.journal, paper.year, paper.issn, paper.eissn,
                json.dumps(paper.keywords, ensure_ascii=False),
                json.dumps(paper.research_areas, ensure_ascii=False),
                json.dumps(paper.wos_categories, ensure_ascii=False),
                paper.times_cited, paper.wos_id,
                json.dumps([r.model_dump() for r in paper.references],
                           ensure_ascii=False),
                paper.source_main, paper.source_file,
                paper.source_abstract, paper.source_authors,
                int(paper.is_reference), int(paper.is_enriched),
                paper.imported_at, paper.enriched_at,
                paper.paper_rank, paper.cocitation_cluster,
            ))
            self._sync_fts(conn, paper)
        return is_new

    def _sync_fts(self, conn: sqlite3.Connection, paper: Paper) -> None:
        conn.execute(
            "DELETE FROM papers_fts WHERE doi=?", (paper.doi,)
        )
        conn.execute("""
            INSERT INTO papers_fts(doi, title, abstract, authors, journal, keywords, year)
            VALUES (?,?,?,?,?,?,?)
        """, (
            paper.doi, paper.title, paper.abstract,
            " ".join(paper.authors), paper.journal,
            " ".join(paper.keywords), paper.year,
        ))

    def get_paper(self, doi: str) -> Paper | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM papers WHERE doi=?", (doi,)
            ).fetchone()
        if row is None:
            return None
        return self._row_to_paper(row)

    def list_papers(self, offset: int = 0, limit: int = 20,
                    is_reference: bool | None = None) -> list[Paper]:
        with self._conn() as conn:
            if is_reference is not None:
                rows = conn.execute(
                    "SELECT * FROM papers WHERE is_reference=? "
                    "ORDER BY imported_at DESC LIMIT ? OFFSET ?",
                    (int(is_reference), limit, offset)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM papers ORDER BY imported_at DESC "
                    "LIMIT ? OFFSET ?", (limit, offset)
                ).fetchall()
        return [self._row_to_paper(r) for r in rows]

    def paper_exists(self, doi: str) -> bool:
        with self._conn() as conn:
            return conn.execute(
                "SELECT 1 FROM papers WHERE doi=?", (doi,)
            ).fetchone() is not None

    def get_unenriched_dois(self, limit: int = 100) -> list[str]:
        """获取待补全元数据的 DOI 列表（有 DOI 但未补全）。"""
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT doi FROM papers
                WHERE doi != '' AND is_enriched = 0
                ORDER BY paper_rank DESC
                LIMIT ?
            """, (limit,)).fetchall()
        return [r["doi"] for r in rows]

    # ---- Citation CRUD ----

    def upsert_citation(self, cit: Citation) -> bool:
        """插入引用边。返回 True 表示新增。"""
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT 1 FROM citations WHERE citing_doi=? AND cited_doi=?",
                (cit.citing_doi, cit.cited_doi)
            ).fetchone()
            if existing:
                return False
            conn.execute("""
                INSERT INTO citations(citing_doi, cited_doi, source, cited_brief)
                VALUES (?,?,?,?)
            """, (cit.citing_doi, cit.cited_doi, cit.source, cit.cited_brief))
        return True

    def upsert_citations(self, citations: list[Citation]) -> int:
        """批量插入引用边。返回新增数量。"""
        new_count = 0
        with self._conn() as conn:
            for cit in citations:
                existing = conn.execute(
                    "SELECT 1 FROM citations WHERE citing_doi=? AND cited_doi=?",
                    (cit.citing_doi, cit.cited_doi)
                ).fetchone()
                if not existing:
                    conn.execute("""
                        INSERT INTO citations(citing_doi, cited_doi, source, cited_brief)
                        VALUES (?,?,?,?)
                    """, (cit.citing_doi, cit.cited_doi, cit.source,
                          cit.cited_brief))
                    new_count += 1
        return new_count

    def get_citations_for(self, doi: str) -> list[Citation]:
        """获取某篇文献的所有引用边（作为施引文献）。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM citations WHERE citing_doi=?", (doi,)
            ).fetchall()
        return [Citation(
            citing_doi=r["citing_doi"], cited_doi=r["cited_doi"],
            source=r["source"], cited_brief=r["cited_brief"]
        ) for r in rows]

    def get_cited_by(self, doi: str) -> list[str]:
        """获取引用了某篇文献的所有 DOI（被引方）。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT citing_doi FROM citations WHERE cited_doi=?",
                (doi,)
            ).fetchall()
        return [r["citing_doi"] for r in rows]

    # ---- FTS Search ----

    def search_fts(self, query: str, limit: int = 20) -> list[SearchResult]:
        """FTS5 关键词检索（trigram 分词，支持中英文子串匹配）。"""
        safe_q = query.replace('"', '""').strip()
        if not safe_q:
            return []
        fts_query = f'"{safe_q}"'

        with self._conn() as conn:
            fts_rows = conn.execute("""
                SELECT doi, rank
                FROM papers_fts
                WHERE papers_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (fts_query, limit)).fetchall()

            if not fts_rows:
                return []

            dois = [r["doi"] for r in fts_rows]
            placeholders = ",".join("?" for _ in dois)
            papers = conn.execute(
                f"SELECT * FROM papers WHERE doi IN ({placeholders})",
                dois
            ).fetchall()

        paper_map = {r["doi"]: r for r in papers}
        results = []
        for fts_row in fts_rows:
            doi = fts_row["doi"]
            if doi not in paper_map:
                continue
            paper = self._row_to_paper(paper_map[doi])
            results.append(SearchResult(
                doi=paper.doi,
                title=paper.title,
                authors=paper.authors,
                year=paper.year,
                journal=paper.journal,
                abstract=paper.abstract[:300],
                times_cited=paper.times_cited,
                paper_rank=paper.paper_rank,
                match_source="keyword",
            ))
        return results

    # ---- Helpers ----

    def _row_to_paper(self, row: sqlite3.Row) -> Paper:
        d = dict(row)
        return Paper(
            doi=d.get("doi", ""),
            title=d.get("title", ""),
            abstract=d.get("abstract", ""),
            authors=json.loads(d.get("authors_json", "[]")),
            affiliations=json.loads(d.get("affiliations_json", "[]")),
            corresponding=json.loads(d.get("corresponding_json", "[]")),
            journal=d.get("journal", ""),
            year=d.get("year", ""),
            issn=d.get("issn", ""),
            eissn=d.get("eissn", ""),
            keywords=json.loads(d.get("keywords_json", "[]")),
            research_areas=json.loads(d.get("research_areas_json", "[]")),
            wos_categories=json.loads(d.get("wos_categories_json", "[]")),
            times_cited=d.get("times_cited", 0),
            wos_id=d.get("wos_id", ""),
            references=[],
            source_main=d.get("source_main", ""),
            source_file=d.get("source_file", ""),
            source_abstract=d.get("source_abstract", ""),
            source_authors=d.get("source_authors", ""),
            is_reference=bool(d.get("is_reference", 0)),
            is_enriched=bool(d.get("is_enriched", 0)),
            imported_at=d.get("imported_at", ""),
            enriched_at=d.get("enriched_at", ""),
            paper_rank=d.get("paper_rank", 0.0),
            cocitation_cluster=d.get("cocitation_cluster", 0),
            impact_factor=d.get("impact_factor", 0.0),
            quartile=d.get("quartile", ""),
            library_citations=d.get("library_citations", 0),
        )
