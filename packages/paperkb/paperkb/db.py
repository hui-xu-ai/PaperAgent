# -*- coding: utf-8 -*-
"""主库访问层（KBStore）：建表迁移 / papers_meta / citations / compile_jobs / FTS5
+ 查询向量缓存（`QueryVecCache`）+ KB 向量索引存储（`IndexStore`：`data/vector/kb_index.db` + 段文件）。两者都是**派生数据**（可重建）。

独立于 backend store.py（paperkb 可独立测试）；backend 集成时经 api 门面调用。
SQLite WAL 模式；所有路径来自 Roots 注入。
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from .config import Roots
from .doi import is_doi, make_rid
from .models import PaperMeta
from .textseg import boundary_trim, strip_frontmatter

logger = logging.getLogger(__name__)

# papers_meta 的列清单（迁移搬运用；与 _SCHEMA 保持一致）
_META_COLS = (
    "rid", "doi", "title", "abstract", "authors_json", "affiliations_json",
    "corresponding_json",
    "journal", "year", "month", "issn", "eissn", "keywords_json",
    "research_areas_json", "wos_categories_json", "funding", "times_cited",
    "wos_id", "references_json", "source_file", "imported_at", "paper_id",
    "journal_override", "kind", "ai_value_score", "topic_score",
    "paper_rank", "cocitation_cluster", "impact_factor", "quartile",
    "library_citations", "source_main",
)

# papers_meta 建表 DDL 抽成常量：迁移层 `migrations/0003_meta_pk_rid.py` 重建该表时
# 复用同一份定义（单一来源，避免两处 DDL 漂移）。
PAPERS_META_DDL = """CREATE TABLE IF NOT EXISTS papers_meta (
    rid TEXT PRIMARY KEY,          -- 统一资源键（P0-B 2026-09-11）：doi-…/isbn-…/cnki-…/nd-<指纹>
    doi TEXT DEFAULT '',           -- 外部标识（真实 DOI；无 DOI 行 = ''，唯一性靠 rid）
    title TEXT DEFAULT '',
    abstract TEXT DEFAULT '',
    authors_json TEXT DEFAULT '[]',
    affiliations_json TEXT DEFAULT '[]',
    corresponding_json TEXT DEFAULT '[]',
    journal TEXT DEFAULT '',
    year TEXT DEFAULT '',
    month TEXT DEFAULT '',
    issn TEXT DEFAULT '',
    eissn TEXT DEFAULT '',
    keywords_json TEXT DEFAULT '[]',
    research_areas_json TEXT DEFAULT '[]',
    wos_categories_json TEXT DEFAULT '[]',
    funding TEXT DEFAULT '',
    times_cited INTEGER DEFAULT 0,
    wos_id TEXT DEFAULT '',
    references_json TEXT DEFAULT '[]',
    source_file TEXT DEFAULT '',
    imported_at TEXT DEFAULT '',
    paper_id INTEGER,
    journal_override TEXT DEFAULT '',
    kind TEXT DEFAULT '',           -- 资源类型（P0-B step3）：paper/thesis/book/chapter/
                                   -- patent/standard/note/si/review；空=由 rid 前缀推导
    ai_value_score REAL,            -- AI 价值评分 0-5（L1+L2 编译时产出）
    topic_score REAL,               -- 主题匹配评分 0-1（L1+L2 编译时产出）
    paper_rank REAL,                -- PaperRank（paperlit 引用图谱 PageRank 变体）
    cocitation_cluster INTEGER,     -- 共被引聚类 ID（paperlit）
    impact_factor REAL,             -- 期刊影响因子（paperlit 清洗模块填充）
    quartile TEXT DEFAULT '',       -- JCR 分区 Q1/Q2/Q3/Q4（paperlit）
    library_citations INTEGER DEFAULT 0, -- 库内被引次数（paperlit）
    source_main TEXT DEFAULT ''     -- 主数据来源（wos/openalex/crossref/semantic_scholar）
);
"""

# 其余表/索引（identifiers/citations/doi_md5_map/compile_*/concepts/*_fts…）
_SCHEMA = PAPERS_META_DDL + """
-- 非空 DOI 唯一（同一 DOI 不得两行；无 DOI 行不受约束）
CREATE UNIQUE INDEX IF NOT EXISTS idx_meta_doi ON papers_meta(doi) WHERE doi <> '';
-- 外部标识 → rid 别名表（P0-B）：一份资料可有多个标识（DOI + arXiv + WOS…），
-- 查询时任一命中即可定位同一资源，不产生重复条目。
CREATE TABLE IF NOT EXISTS identifiers (
    kind TEXT NOT NULL,            -- doi | isbn | cnki | cstr | arxiv | pmid | wos | report
    value TEXT NOT NULL,
    rid TEXT NOT NULL,
    created_at TEXT DEFAULT '',
    PRIMARY KEY (kind, value)
);
CREATE INDEX IF NOT EXISTS idx_identifiers_rid ON identifiers(rid);
-- T6：目录名 ↔ DOI/md5 双映射（未来无 DOI 文献用 md5 命名目录时，靠此表反查 DOI）
CREATE TABLE IF NOT EXISTS doi_md5_map (
    key TEXT PRIMARY KEY,      -- 目录名（doi_to_dirname 产物 或 md5(PDF 全文)）
    doi TEXT DEFAULT '',       -- 有 DOI 存 DOI；无 DOI 存 ''（单纯 md5 文献）
    pdf_md5 TEXT DEFAULT '',   -- PDF 全文 md5（与 backend store.papers.pdf_md5 同源）
    paper_id INTEGER DEFAULT 0 -- backend store.papers.id（可选关联）
);
CREATE TABLE IF NOT EXISTS citations (
    citing_doi TEXT NOT NULL,
    cited_doi TEXT NOT NULL,
    cited_brief TEXT DEFAULT '',
    PRIMARY KEY (citing_doi, cited_doi)
);
CREATE INDEX IF NOT EXISTS idx_citations_cited ON citations(cited_doi);
CREATE TABLE IF NOT EXISTS compile_jobs (
    paper_doi TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'L1',
    status TEXT DEFAULT 'pending',
    value_score REAL DEFAULT 0,
    priority INTEGER DEFAULT 0,
    error TEXT DEFAULT '',
    started_at TEXT DEFAULT '',
    done_at TEXT DEFAULT '',
    PRIMARY KEY (paper_doi, level)
);
CREATE TABLE IF NOT EXISTS compile_ctx (
    paper_doi TEXT NOT NULL,
    level TEXT NOT NULL,
    summary TEXT DEFAULT '',
    updated_at TEXT DEFAULT '',
    PRIMARY KEY (paper_doi, level)
);
CREATE TABLE IF NOT EXISTS concepts (
    paper_doi TEXT NOT NULL,
    name TEXT NOT NULL,
    definition TEXT DEFAULT '',
    updated_at TEXT DEFAULT '',
    PRIMARY KEY (paper_doi, name)
);
CREATE VIRTUAL TABLE IF NOT EXISTS meta_fts USING fts5(
    rid, title, abstract, authors, affiliations, journal, keywords, year,
    tokenize = 'unicode61'
);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    doi, filename, content,
    tokenize = 'trigram'
);
CREATE VIRTUAL TABLE IF NOT EXISTS fulltext_fts USING fts5(
    doi, content,
    tokenize = 'trigram'
);
-- 知识库回收站（2026-09-12 用户需求）：移除到回收站的资源**不再出现在知识库、也不再被检索**，
-- 但磁盘产物（编译产物/原文层/用户笔记/附件）**全部保留**，可一键恢复。
-- 记录键 = 资源键（rid/DOI/目录名归一化后的 key）+ 迁移前的目录名，便于原样搬回。
CREATE TABLE IF NOT EXISTS kb_trash (
    key TEXT PRIMARY KEY,
    dirname TEXT DEFAULT '',
    title TEXT DEFAULT '',
    trashed_at TEXT DEFAULT '',
    note TEXT DEFAULT ''
);
"""


class KBStore:
    """知识库主库访问（数据层；业务逻辑在 api 门面）。"""

    def __init__(self, roots: Roots):
        self.roots = roots
        self.db_path = roots.main_db

    # ---------------------------------------------------------- 连接
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def is_kb_edited(self, rel_path: str) -> bool:
        """该 kb 文件是否被用户在阅读器里编辑过（**用户资产保护**）。

        表 `kb_edited` 与文献元数据同库（`data/biblio/biblio.db`，由 `Roots.main_db`
        指向）；paperkb 侧只读、建表缺失时一律视为"未编辑"（不抛）。
        用途：`sync_source_to_kb` 刷新陈旧派生副本时跳过用户手改过的文件（P2）。
        """
        try:
            with self._conn() as conn:
                row = conn.execute("SELECT 1 FROM kb_edited WHERE path=?",
                                   (rel_path,)).fetchone()
            return row is not None
        except sqlite3.Error:
            return False

    def init_schema(self) -> None:
        self.roots.ensure()
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            # 2026-09-12：**全部历史迁移已收编进迁移层**
            # （migrations/0001_baseline.py + 0002_settings_prices.py + 0003_meta_pk_rid.py；
            #  见 docs/VERSIONING.md §3 与 docs/COMPAT-REGISTER.md）。
            # 这里只建"当前格式"，旧库由启动时的迁移 runner 先迁移再打开（幂等、零开销）。
            # 迁移：meta_fts 列 doi → rid（FTS 内容由 papers_meta 派生，可重建）
            mcols = {r[1] for r in conn.execute("PRAGMA table_info(meta_fts)").fetchall()}
            if mcols and "rid" not in mcols:
                conn.execute("DROP TABLE IF EXISTS meta_fts")
                conn.executescript(_SCHEMA)
                logger.warning("meta_fts 列 doi→rid 重建（原文索引由 bib 重新导入时重灌）")
            # 迁移：notes/fulltext FTS 分词器升级 trigram（中文子串命中，2026-08-27）
            # unicode61 → trigram 需重建虚拟表；重建后自动从 kb 全量重灌索引
            if self._fts_tokenizer(conn, "notes_fts") != "trigram":
                conn.execute("DROP TABLE IF EXISTS notes_fts")
                conn.execute("DROP TABLE IF EXISTS fulltext_fts")
                conn.executescript(_SCHEMA)
                logger.warning("FTS 分词器升级 trigram：索引已清空，正在从 kb 重建…")
                rebuilt = self.reindex_from_kb(fulltext=True)
                logger.warning("FTS trigram 重建完成: %s", rebuilt)
            # 迁移：papers_meta 补 AI 评分列（2026-09-19）
            meta_cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(papers_meta)").fetchall()}
            for col in ("ai_value_score", "topic_score"):
                if meta_cols and col not in meta_cols:
                    conn.execute(f"ALTER TABLE papers_meta ADD COLUMN {col} REAL")
                    logger.info("papers_meta 补列: %s", col)
            # 迁移：papers_meta 补 paperlit 元数据列（2026-09-19）
            _LIT_META_COLS = {
                "paper_rank": "REAL",
                "cocitation_cluster": "INTEGER",
                "impact_factor": "REAL",
                "quartile": "TEXT DEFAULT ''",
                "library_citations": "INTEGER DEFAULT 0",
                "source_main": "TEXT DEFAULT ''",
            }
            for col, ddl in _LIT_META_COLS.items():
                if meta_cols and col not in meta_cols:
                    conn.execute(f"ALTER TABLE papers_meta ADD COLUMN {col} {ddl}")
                    logger.info("papers_meta 补列(paperlit): %s", col)

    # ---------------------------------------------------------- 标识 ↔ rid
    def resolve_rid(self, key: str) -> str:
        """把任意查询键解析为 rid（P0-B 统一入口，调用方无需关心键的类型）。

        - 已是 rid 形态（doi-/isbn-/cnki-/cstr-/arxiv-/rep-/nd-/si__/review__…）→ 原样
        - 真实 DOI → 查 identifiers(kind='doi')；未登记则按规则算 `doi-…`
          （保证"先导入 bib 还是先编译"都能命中同一 rid）
        - 其他（md5 目录名 / 历史目录名）→ 原样返回（宽松，不阻断既有流程）
        - **DOI 目录名**（`10.1002_adma.202407106`）→ 反推 DOI 再解析（2026-09-16：
          引擎层确有拿目录名当键的调用点，不归一化就会"同一条目两种键、其中一个查空"）
        """
        value = (key or "").strip()
        if not value:
            return ""
        if value.startswith(("doi-", "isbn-", "cnki-", "cstr-", "arxiv-", "rep-",
                             "nd-", "si__", "review__", "book__", "chapter__",
                             "thesis__", "note__", "patent__", "std__")):
            return value
        doi = value if is_doi(value) else ""
        if not doi:
            # 目录名形态（`10.1002_adma.202407106`，= doi_to_dirname 产物）→ 反推 DOI。
            # 2026-09-16 修：引擎/任务层多处用**目录名**当键（如
            # `engine_service.combined_translate` 传 `doi_dir` 给 `get_paper_meta`），
            # 旧实现只认 RID 与裸 DOI ⇒ 恒落到既有行之外 ⇒ `has_meta=False`、
            # `get_meta=None`，变体头部期刊/年份/被引全空（实测 `probe_key_forms.py`）。
            # 不可逆目录名（带 --xxxxxx 消歧后缀）走 doi_md5_map 反查。
            from .doi import dirname_to_doi

            doi = dirname_to_doi(value)
            if not doi:
                try:
                    row = self.get_doi_md5_map(value)
                except Exception:  # noqa: BLE001 - 无映射表按未命中处理
                    row = None
                cand = str((row or {}).get("doi") or "").strip()
                doi = cand if is_doi(cand) else ""
        if doi:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT rid FROM identifiers WHERE kind='doi' AND value=?",
                    (doi,)).fetchone()
            return row["rid"] if row else make_rid("paper", doi=doi)
        return value

    def register_identifier(self, kind: str, value: str, rid: str) -> None:
        """登记外部标识 → rid（幂等；同一标识只指向一个 rid）。"""
        kind = (kind or "").strip().lower()
        value = (value or "").strip()
        if not kind or not value or not rid:
            return
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO identifiers(kind,value,rid,created_at) "
                "VALUES(?,?,?,?)",
                (kind, value, rid, datetime.now().isoformat(timespec="seconds")))

    def find_rid_by_identifier(self, kind: str, value: str) -> str:
        """按外部标识反查 rid（未登记返回 ""）。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT rid FROM identifiers WHERE kind=? AND value=?",
                ((kind or "").lower(), (value or "").strip())).fetchone()
        return row["rid"] if row else ""

    def identifiers_for(self, rid: str) -> list[dict]:
        """列出某资源的全部外部标识（DOI/ISBN/CNKI…）。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT kind, value FROM identifiers WHERE rid=? ORDER BY kind",
                (rid,)).fetchall()
        return [{"kind": r["kind"], "value": r["value"]} for r in rows]

    # ---------------------------------------------------------- papers_meta
    def upsert_meta(self, meta: PaperMeta, rid: str = "") -> str:
        """写入/更新元数据（**键 = rid**；DOI 等外部标识登记进 identifiers 表）。

        返回写入行的 rid。rid 缺省时按规则推导：真 DOI → `doi-…`；否则用 WOS 号，
        再否则用 (标题|年份) 指纹 —— 这样**无 DOI 资料也能入库且互不覆盖**
        （旧实现以 doi 为主键，第二篇无 DOI 文献必然撞主键）。
        """
        now = datetime.now().isoformat(timespec="seconds")
        doi = (meta.doi or "").strip()
        if not rid:
            rid = getattr(meta, "rid", "") or ""
        if not rid:
            if is_doi(doi):
                rid = make_rid("paper", doi=doi)
            else:
                rid = make_rid(
                    "paper", report_no=meta.wos_id or "",
                    fingerprint=hashlib.md5(
                        ((meta.title or "") + "|" + (meta.year or ""))
                        .encode("utf-8")).hexdigest())
        with self._conn() as conn:
            # ── 2026-09-12（用户提问引出）──
            # 本函数是 **INSERT OR REPLACE 整行写入**：来者没提供的列会回落 `PaperMeta` 的
            # 默认值。bib 导入（`api.bib_import` → 这里）**不携带**下面三列，于是整行替换会把
            # 它们**清空**：
            #   · `paper_id`        —— 该元数据 ↔ 解析篇 `papers.id` 的关联（bib 不提供）
            #   · `kind`            —— 资源类型（书/学位论文/标准/专利…，bib 不提供）
            #   · `journal_override`—— 人工纠正的期刊名（bib 不提供）→ 一旦被清，
            #                          journals.db 又匹配不上，**IF 档再次丢失**
            # 这三列属于"应用自己维护的状态"，空值不该覆盖既有值；其余书目列仍按 bib 权威
            # **整行替换**（bib 更准，本就该覆盖）。注意这不是"bib 搞坏了数据"，而是
            # "写入语义是整行替换而非逐字段合并"。
            old = conn.execute(
                "SELECT paper_id, kind, journal_override, ai_value_score, topic_score,"
                " paper_rank, cocitation_cluster, impact_factor, quartile,"
                " library_citations, source_main"
                " FROM papers_meta WHERE rid=?",
                (rid,)).fetchone()
            if old is not None:
                keep: dict = {}
                if meta.paper_id in (None, 0, ""):
                    keep["paper_id"] = old["paper_id"]
                if not (getattr(meta, "kind", "") or "").strip() and (old["kind"] or ""):
                    keep["kind"] = old["kind"]
                if (not (getattr(meta, "journal_override", "") or "").strip()
                        and (old["journal_override"] or "")):
                    keep["journal_override"] = old["journal_override"]
                if getattr(meta, "ai_value_score", None) is None and old["ai_value_score"] is not None:
                    keep["ai_value_score"] = old["ai_value_score"]
                if getattr(meta, "topic_score", None) is None and old["topic_score"] is not None:
                    keep["topic_score"] = old["topic_score"]
                # paperlit 元数据：bib 不提供，保留既有值（sync_lit_meta 单独写入）
                for _lit_col in ("paper_rank", "cocitation_cluster", "impact_factor",
                                 "quartile", "library_citations", "source_main"):
                    old_val = old[_lit_col]
                    new_val = getattr(meta, _lit_col, None)
                    if (new_val in (None, 0, 0.0, "") and old_val is not None
                            and old_val not in (None, 0, 0.0, "")):
                        keep[_lit_col] = old_val
                if keep:
                    meta = meta.model_copy(update=keep)
            conn.execute(
                """INSERT OR REPLACE INTO papers_meta(
                    rid,doi,title,abstract,authors_json,affiliations_json,corresponding_json,
                    journal,year,
                    month,issn,eissn,keywords_json,research_areas_json,
                    wos_categories_json,funding,times_cited,wos_id,references_json,
                    source_file,imported_at,paper_id,journal_override,kind,
                    ai_value_score,topic_score,
                    paper_rank,cocitation_cluster,impact_factor,quartile,
                    library_citations,source_main)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rid, doi, meta.title, meta.abstract,
                 _json(meta.authors), _json(meta.affiliations),
                 _json(getattr(meta, "corresponding", []) or []),
                 meta.journal, meta.year,
                 meta.month, meta.issn, meta.eissn, _json(meta.keywords),
                 _json(meta.research_areas), _json(meta.wos_categories),
                 meta.funding, meta.times_cited, meta.wos_id,
                 _json([r.model_dump() for r in meta.references]),
                 meta.source_file, now, meta.paper_id,
                 getattr(meta, "journal_override", "") or "",
                 (getattr(meta, "kind", "") or "").strip().lower(),
                 getattr(meta, "ai_value_score", None),
                 getattr(meta, "topic_score", None),
                 getattr(meta, "paper_rank", 0.0) or 0.0,
                 getattr(meta, "cocitation_cluster", 0) or 0,
                 getattr(meta, "impact_factor", 0.0) or 0.0,
                 getattr(meta, "quartile", "") or "",
                 getattr(meta, "library_citations", 0) or 0,
                 getattr(meta, "source_main", "") or ""))
            # FTS 同步（删除旧行 + 插入新行）
            conn.execute("DELETE FROM meta_fts WHERE rid=?", (rid,))
            conn.execute(
                """INSERT INTO meta_fts(rid,title,abstract,authors,affiliations,journal,keywords,year)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (rid, meta.title, meta.abstract,
                 " ".join(meta.authors), " ".join(meta.affiliations),
                 meta.journal, " ".join(meta.keywords), meta.year))
            # 外部标识登记（P0-B）：任一标识都能再查回同一 rid
            if is_doi(doi):
                conn.execute(
                    "INSERT OR REPLACE INTO identifiers(kind,value,rid,created_at) "
                    "VALUES('doi',?,?,?)", (doi, rid, now))
            if (meta.wos_id or "").strip():
                conn.execute(
                    "INSERT OR REPLACE INTO identifiers(kind,value,rid,created_at) "
                    "VALUES('wos',?,?,?)", (meta.wos_id.strip(), rid, now))
        return rid

    def replace_citations(self, citing_doi: str, refs: list) -> None:
        """重建某文献的引用边（先删后插，幂等）。refs 为 CitedRef 列表。"""
        with self._conn() as conn:
            conn.execute("DELETE FROM citations WHERE citing_doi=?", (citing_doi,))
            for ref in refs:
                cdoi = (ref.doi or "").strip()
                brief = ref.raw or f"{ref.author} {ref.journal} {ref.year}".strip()
                if not cdoi:
                    continue                    # 无 DOI 的引用不建边（brief 在 references_json）
                conn.execute(
                    "INSERT OR REPLACE INTO citations(citing_doi,cited_doi,cited_brief) "
                    "VALUES(?,?,?)", (citing_doi, cdoi, brief))

    def get_meta(self, key: str) -> PaperMeta | None:
        """按 rid 或任意已登记标识（DOI/WOS…）取元数据（P0-B：键解析内置）。"""
        rid = self.resolve_rid(key)
        if not rid:
            return None
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM papers_meta WHERE rid=?", (rid,)).fetchone()
        return _row_to_meta(row) if row else None

    def get_kind_by_rid(self, rid: str) -> str:
        """papers_meta.kind（P0-B step3）：空串 = 未显式设置（调用方按 RID 前缀推导）。"""
        r = (rid or "").strip()
        if not r:
            return ""
        with self._conn() as conn:
            row = conn.execute("SELECT kind FROM papers_meta WHERE rid=?", (r,)).fetchone()
        if row is None:
            return ""
        return ((row["kind"] if "kind" in row.keys() else "") or "").strip().lower()

    def has_meta(self, key: str) -> bool:
        rid = self.resolve_rid(key)
        if not rid:
            return False
        with self._conn() as conn:
            row = conn.execute("SELECT 1 FROM papers_meta WHERE rid=?", (rid,)).fetchone()
        return row is not None

    def list_meta(self, limit: int = 500) -> list[PaperMeta]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM papers_meta ORDER BY imported_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_meta(r) for r in rows]

    def search_meta(self, query: str, limit: int = 20) -> list[PaperMeta]:
        """FTS5 全文检索（BM25）；AND 无命中自动回退 OR（中文问题→英文索引）。"""
        q = _fts_query(query)
        if not q:
            return []
        rows = self._search_meta_raw(q, limit)
        if not rows:
            q2 = _fts_query(query, mode="OR")
            if q2 != q:
                rows = self._search_meta_raw(q2, limit)
        return [_row_to_meta(r) for r in rows]

    def _search_meta_raw(self, q: str, limit: int) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                """SELECT m.* FROM meta_fts f JOIN papers_meta m ON m.rid = f.rid
                   WHERE meta_fts MATCH ? ORDER BY bm25(meta_fts) LIMIT ?""",
                (q, limit)).fetchall()

    # ---------------------------------------------------------- citations
    def citations_for(self, doi: str) -> dict:
        """引用关系：cited = 它引用的（出边）；citing = 引用它的（入边，需全库）。"""
        with self._conn() as conn:
            cited = conn.execute(
                "SELECT cited_doi, cited_brief FROM citations WHERE citing_doi=? "
                "ORDER BY cited_brief", (doi,)).fetchall()
            citing = conn.execute(
                "SELECT citing_doi FROM citations WHERE cited_doi=?", (doi,)).fetchall()
        return {
            "cited": [{"doi": r["cited_doi"], "brief": r["cited_brief"]} for r in cited],
            "citing": [{"doi": r["citing_doi"]} for r in citing],
        }

    def citation_peers(self, doi: str, limit: int = 10) -> list[dict]:
        """通过引用关系关联的文献（出边+入边合并去重）。"""
        with self._conn() as conn:
            cited = conn.execute(
                "SELECT cited_doi AS doi FROM citations WHERE citing_doi=? LIMIT ?",
                (doi, limit)).fetchall()
            citing = conn.execute(
                "SELECT citing_doi AS doi FROM citations WHERE cited_doi=? LIMIT ?",
                (doi, limit)).fetchall()
        seen = set()
        peers = []
        for r in list(cited) + list(citing):
            d = r["doi"]
            if d and d not in seen and d != doi:
                seen.add(d)
                peers.append({"doi": d})
            if len(peers) >= limit:
                break
        return peers

    def all_dois(self) -> set[str]:
        with self._conn() as conn:
            rows = conn.execute("SELECT doi FROM papers_meta").fetchall()
        return {r["doi"] for r in rows}

    # ---------------------------------------------------------- doi_md5_map（T6：目录名 ↔ DOI/md5 双映射）
    def set_doi_md5_map(self, key: str, doi: str = "", pdf_md5: str = "",
                        paper_id: int = 0) -> None:
        """登记目录名 ↔ (DOI, pdf_md5) 映射（INSERT OR REPLACE，幂等）。

        key=目录名（doi_to_dirname 产物或 md5(PDF 全文)，T5）；无 DOI 文献 doi 存 ''。
        """
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO doi_md5_map(key, doi, pdf_md5, paper_id) "
                "VALUES(?,?,?,?)", (key, doi or "", pdf_md5 or "", paper_id or 0))

    def get_doi_md5_map(self, key: str) -> dict | None:
        """按目录名查映射（md5 目录反查 DOI 用）。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT key, doi, pdf_md5, paper_id FROM doi_md5_map WHERE key=?",
                (key,)).fetchone()
        return dict(row) if row else None

    def list_doi_md5_map(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT key, doi, pdf_md5, paper_id FROM doi_md5_map ORDER BY key"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------- AI 评分
    def update_ai_scores(self, paper_doi: str, *,
                         ai_value_score: float | None = None,
                         topic_score: float | None = None) -> None:
        """写入 L1+L2 编译产出的 AI 评分（零额外成本：编译时顺手打分）。"""
        sets, vals = [], []
        if ai_value_score is not None:
            sets.append("ai_value_score=?")
            vals.append(round(float(ai_value_score), 2))
        if topic_score is not None:
            sets.append("topic_score=?")
            vals.append(round(float(topic_score), 4))
        if not sets:
            return
        rid = self.resolve_rid(paper_doi)
        vals.append(rid)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE papers_meta SET {', '.join(sets)} WHERE rid=?", vals)

    def fill_journal_meta(self, paper_doi: str) -> bool:
        """按需补全 IF/分区：papers_meta 缺失时从 journals.db 查找并写入。

        查找顺序（与 _score_meta 一致）：ISSN/eISSN 精确 → 期刊名规范化。
        返回 True 表示有更新。
        """
        rid = self.resolve_rid(paper_doi)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT impact_factor, quartile, issn, eissn, journal"
                " FROM papers_meta WHERE rid=?", (rid,)).fetchone()
        if row is None:
            return False
        if row["impact_factor"] and row["quartile"]:
            return False
        try:
            from .journals import JournalsDB
            jdb = JournalsDB(self.roots)
            info = jdb.lookup_issn(row["issn"] or "", row["eissn"] or "")
            if info is None and row["journal"]:
                info = jdb.lookup(row["journal"])
            if info is None:
                return False
            jcr = info.get("jcr") or {}
            new_if = jcr.get("jif") or 0
            new_q = jcr.get("quartile") or ""
            if not new_if and not new_q:
                return False
            sets, vals = [], []
            if not row["impact_factor"] and new_if:
                sets.append("impact_factor=?")
                vals.append(float(new_if))
            if not row["quartile"] and new_q:
                sets.append("quartile=?")
                vals.append(new_q)
            if not row["impact_factor"] and new_q:
                sets.append("source_main=?")
                vals.append("journals.db")
            if sets:
                vals.append(rid)
                with self._conn() as conn:
                    conn.execute(
                        f"UPDATE papers_meta SET {', '.join(sets)} WHERE rid=?",
                        vals)
            return bool(sets)
        except Exception:
            return False

    def delete_meta(self, key: str) -> None:
        """删除元数据记录（按 rid 或 doi）。"""
        with self._conn() as conn:
            conn.execute("DELETE FROM papers_meta WHERE rid=?", (key,))
            if key and "." in key:  # 看起来像 DOI，也按 doi 删
                conn.execute("DELETE FROM papers_meta WHERE doi=?", (key,))

    # ---------------------------------------------------------- compile_jobs
    def upsert_job(self, paper_doi: str, level: str, status: str = "pending",
                   value_score: float = 0.0, priority: int = 0, error: str = "",
                   done_at: str = "") -> None:
        """写入/更新队列项。`done_at` 可选（2026-09-12：此前恒写空串 → 无法判断完成时间）。"""
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO compile_jobs(
                     paper_doi,level,status,value_score,priority,error,started_at,done_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (paper_doi, level, status, value_score, priority, error,
                 datetime.now().isoformat(timespec="seconds"), done_at or ""))

    def get_job(self, paper_doi: str, level: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM compile_jobs WHERE paper_doi=? AND level=?",
                (paper_doi, level)).fetchone()
        return dict(row) if row else None

    def delete_job(self, paper_doi: str, level: str = "") -> None:
        """删除编译任务记录（按 paper_doi + 可选 level）。"""
        with self._conn() as conn:
            if level:
                conn.execute("DELETE FROM compile_jobs WHERE paper_doi=? AND level=?",
                             (paper_doi, level))
            else:
                conn.execute("DELETE FROM compile_jobs WHERE paper_doi=?", (paper_doi,))

    # ---------------------------------------------------------- 回收站（2026-09-12）
    def trash_add(self, key: str, dirname: str = "", title: str = "", note: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO kb_trash(key,dirname,title,trashed_at,note)
                   VALUES(?,?,?,?,?)""",
                (key, dirname, title, datetime.now().isoformat(timespec="seconds"), note))

    def trash_remove(self, key: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM kb_trash WHERE key=?", (key,))

    def trash_keys(self) -> set[str]:
        """已移除到回收站的资源键集合（供 kb_list/检索过滤）。"""
        with self._conn() as conn:
            return {r["key"] for r in conn.execute("SELECT key FROM kb_trash")}

    def trash_list(self) -> list[dict]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM kb_trash ORDER BY trashed_at DESC")]

    def delete_index_for(self, keys) -> dict:
        """把某资源从**检索索引**里摘掉（notes_fts / fulltext_fts，含其附件行）。

        与 `index_notes`（只删非附件行、防洗掉用户附件）不同：移入回收站时**附件也必须
        不可检索**（整篇退出知识库），恢复时由 `reindex_from_kb` 连附件一起重建。

        ⚠️ `keys` 可以是**多个候选键**：同一篇在不同位置有三种写法——索引行用**裸 DOI**
        （`dir_to_key` 产物），`papers_meta`/`kb_trash` 用 **rid**（`resource_key` 产物，
        如 `doi-10.1000_x.1`），磁盘目录用**目录名**。只按其中一种删会漏（实测踩到）。
        """
        if isinstance(keys, str):
            keys = [keys]
        cands = [k for k in dict.fromkeys(keys) if k]
        if not cands:
            return {"notes_removed": 0, "fulltext_removed": 0}
        ph = ",".join("?" * len(cands))
        like = " OR ".join("doi LIKE ?" for _ in cands)
        with self._conn() as conn:
            n1 = conn.execute(
                f"DELETE FROM notes_fts WHERE doi IN ({ph})", tuple(cands)).rowcount
            n2 = conn.execute(
                f"DELETE FROM fulltext_fts WHERE doi IN ({ph}) OR {like}",
                (*cands, *[k + "::%" for k in cands])).rowcount
        # 向量索引同步摘除：否则已删文献仍会被语义检索召回，而命中块读不回原文
        # （文件没了 → 空片段，"有引用无证据"）。
        try:
            from .vector import drop_papers_from_index

            drop_papers_from_index(self.roots, cands)
        except Exception as e:  # noqa: BLE001 - 向量索引可选，失败不影响摘除
            logger.info("向量索引摘除跳过: %s", e)
        return {"notes_removed": n1, "fulltext_removed": n2}

    def next_job(self) -> dict | None:
        """取优先级最高且未完成的队列项（queued 优先于 pending）。"""
        with self._conn() as conn:
            row = conn.execute(
                """SELECT * FROM compile_jobs
                   WHERE status IN ('queued','pending')
                   ORDER BY priority DESC, value_score DESC, started_at ASC
                   LIMIT 1""").fetchone()
        return dict(row) if row else None

    def list_jobs(self, status: str | None = None, limit: int = 200) -> list[dict]:
        with self._conn() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM compile_jobs WHERE status=? ORDER BY started_at DESC LIMIT ?",
                    (status, limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM compile_jobs ORDER BY started_at DESC LIMIT ?",
                    (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------- compile_ctx
    def upsert_ctx(self, paper_doi: str, level: str, summary: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO compile_ctx(paper_doi,level,summary,updated_at)
                   VALUES(?,?,?,?)""",
                (paper_doi, level, summary,
                 datetime.now().isoformat(timespec="seconds")))

    def get_ctx(self, paper_doi: str, level: str) -> str:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT summary FROM compile_ctx WHERE paper_doi=? AND level=?",
                (paper_doi, level)).fetchone()
        return (row["summary"] if row else "") or ""

    # ---------------------------------------------------------- notes/fulltext 索引
    def index_notes(self, doi: str, files: list[dict]) -> None:
        """编译产物入 notes_fts（files=[{filename, content}]；先删后插，幂等）。

        ⚠️ P0-B step3：只删**编译产物**行（filename 不以 `attachments/` 开头）——
        用户的附件索引（SI/审稿意见）与编译产物共用 doi 键，整删会把用户资料从
        检索里洗掉。见 `index_attachment`/`index_attachments`。

        2026-09-21：`.md` 产物的 **YAML frontmatter 不入索引**——`type/doi/tags` 是模板
        噪声，既占命中窗口（CJK LIKE 兜底曾把 `--- type: paper-note ---` 当检索证据返回），
        又让 bm25 把"标签词"当正文命中。正文仍原样保留。
        """
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM notes_fts WHERE doi=? AND filename NOT LIKE 'attachments/%'",
                (doi,))
            for f in files:
                content = f.get("content") or ""
                if not content.strip():
                    continue
                name = f.get("filename", "")
                if name.endswith(".md"):
                    content = strip_frontmatter(content)
                conn.execute(
                    "INSERT INTO notes_fts(doi, filename, content) VALUES(?,?,?)",
                    (doi, name, content))

    def append_note(self, doi: str, filename: str, content: str) -> None:
        """追加单条入 notes_fts（不删已有行）。供 _qa 写回（多条 _qa 共存，不互相覆盖）。"""
        content = strip_frontmatter(content or "") if (filename or "").endswith(".md") else content
        if not (content or "").strip():
            return
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO notes_fts(doi, filename, content) VALUES(?,?,?)",
                (doi, filename, content))

    # ---------------------------------------------------------- 附件索引（P0-B step3）
    # 约定（A5）：doi 列 = **父资源 RID**（可为 doi-/nd-/book__ 等任意 RID），
    # filename 列 = `attachments/<kind>/<文件名>` 相对路径（父资源目录为基准，
    # 无父资源的独立根用 `<RID>/<kind>/<文件名>`）。这样 notes_for(rid) 单篇查询照旧，
    # 召回结果自带"哪个附件"，`read_attachment` 可直接用该相对路径读回。
    # fulltext_fts 侧的镜像行键 = `<RID>::<相对路径>`（见 index_attachment_fulltext）。
    ATTACH_FT_SEP = "::"

    def index_attachment(self, rid: str, rel_path: str, content: str) -> None:
        """单个附件文本入 notes_fts（同 (rid, rel_path) 先删后插，重复导入幂等）。

        同时**镜像进 fulltext_fts**（复合键 `<rid>::<rel_path>`）——全文索引用 trigram，
        带中文关键词的 SI/审稿意见/数据文本也能走全文召回。见 `index_attachment_fulltext`。
        """
        rid = (rid or "").strip()
        rel = (rel_path or "").strip()
        if not rid or not rel or not (content or "").strip():
            return
        with self._conn() as conn:
            conn.execute("DELETE FROM notes_fts WHERE doi=? AND filename=?", (rid, rel))
            conn.execute(
                "INSERT INTO notes_fts(doi, filename, content) VALUES(?,?,?)",
                (rid, rel, content))
        self.index_attachment_fulltext(rid, rel, content)

    def index_attachment_fulltext(self, rid: str, rel_path: str,
                                  content: str) -> None:
        """附件文本镜像进 fulltext_fts，键 = `<rid>::<rel_path>`（先删后插，幂等）。

        为什么用复合键：`index_fulltext(doi, …)` 是按**纯 doi 键整删**的（正文全文先删
        后插）。附件若共用纯 rid 键，重编译正文时会把附件全文一起洗掉；复合键让两者
        互不误删。检索侧从 `::` 切回 (rid, 附件相对路径)，见 `retrieve.recall`。
        """
        rid = (rid or "").strip()
        rel = (rel_path or "").strip().replace("\\", "/")
        if not rid or not rel or not (content or "").strip():
            return
        key = f"{rid}{self.ATTACH_FT_SEP}{rel}"
        with self._conn() as conn:
            conn.execute("DELETE FROM fulltext_fts WHERE doi=?", (key,))
            conn.execute("INSERT INTO fulltext_fts(doi, content) VALUES(?,?)",
                         (key, content))

    def indexed_attachment_paths(self, rid: str) -> set[str]:
        """该资源已索引的附件相对路径集合（导入对账用）。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT filename FROM notes_fts WHERE doi=? AND filename LIKE 'attachments/%'",
                ((rid or "").strip(),)).fetchall()
        return {r["filename"] for r in rows}

    def index_attachments(self, rid: str, items: list[dict]) -> int:
        """批量：items=[{filename(相对路径), content}]；返回写入条数。"""
        n = 0
        for it in items:
            if (it.get("content") or "").strip():
                self.index_attachment(rid, it.get("filename", ""), it["content"])
                n += 1
        return n

    def index_fulltext(self, doi: str, content: str) -> None:
        """en.md 全文入 fulltext_fts（可选开关控制）。"""
        if not content.strip():
            return
        with self._conn() as conn:
            conn.execute("DELETE FROM fulltext_fts WHERE doi=?", (doi,))
            conn.execute("INSERT INTO fulltext_fts(doi, content) VALUES(?,?)",
                         (doi, content))

    def _card_note_entries(self, kb: Path, d: Path) -> list[dict]:
        """收集 d/cards/*.md（card-qa 卡片）为 notes_fts 条目。

        - filename = 卡片相对 kb 路径（<dir>/cards/qa-<slug>.md），与 save_qa 追加的
          meta["path"] 一致，保证 build_context 的 notes_content(doi, file) 能取回。
        - content = 去 frontmatter 后的卡片正文（与 save_qa 追加内容正文一致）。
        只收 card-qa 类型：历史检索源只有 QA 卡片进 notes_fts，translate/summary/note
        卡片不纳入保持原语义。
        """
        from .cards import _body, _frontmatter

        cards_dir = d / "cards"
        if not cards_dir.is_dir():
            return []
        out = []
        for p in sorted(cards_dir.glob("*.md")):
            text = p.read_text(encoding="utf-8", errors="replace")
            fm = _frontmatter(text)
            if fm.get("type") != "card-qa":
                continue
            body = _body(text)
            if not body.strip():
                continue
            out.append({"filename": str(p.relative_to(kb)), "content": body})
        return out

    def reindex_from_kb(self, fulltext: bool = False) -> dict:
        """扫 knowledge_base/ 全量重建 notes_fts（+fulltext_fts 可选）。幂等。

        重建源：每篇 _note/_wiki/_relations 编译产物 + 该篇 cards/ 下的 card-qa 卡片，
        并单独索引 _global/cards/ 下的知识库 QA 卡片（全局保留键 "_global"）。
        QA 卡片随单篇/全局一并入 notes_fts，全库重建后不丢、命名空间互不串。
        """
        from .doi import dir_to_key

        notes = fulls = 0
        kb = self.roots.kb_dir
        if kb.exists():
            for d in kb.iterdir():
                # 2026-09-12：同时跳过 `.` 前缀目录——`.trash/`（回收站）里的资源**必须保持
                # 不可检索**，否则一次全量重建就把它们又索引回知识库。
                if not d.is_dir() or d.name.startswith(("_", ".")):
                    continue
                key, _kind = dir_to_key(d.name, self)  # T6：md5 目录也进索引
                if not key:
                    continue
                doi = key
                files = []
                for name in ("_note.md", "_wiki.md", "_relations.md"):
                    p = d / name
                    if p.exists():
                        files.append({"filename": name,
                                      "content": p.read_text(encoding="utf-8",
                                                             errors="replace")})
                files += self._card_note_entries(kb, d)
                if files:
                    self.index_notes(doi, files)
                    notes += 1
                if fulltext:
                    en = d / "en.md"
                    if en.exists():
                        self.index_fulltext(doi, en.read_text(encoding="utf-8",
                                                              errors="replace"))
                        fulls += 1
            # 全局知识库 QA：_global/cards/*.md（_ 前缀目录被主循环跳过，显式处理）
            g = kb / "_global"
            if g.is_dir():
                gfiles = self._card_note_entries(kb, g)
                if gfiles:
                    self.index_notes("_global", gfiles)
                    notes += 1
        # 附件文本（P0-B step3）：编译产物与附件共用 doi 键，上面 index_notes 只删
        # 非附件行；但旧库/旧版本可能整删过 → 全量重建时一并重扫附件，避免"用户资料
        # 在检索里消失"。扫描范围：library/<资源>/attachments/**（有父）+
        # attachments/<RID>/**（无父）。文本抽取纯本地（无 LLM）。
        att = self.reindex_attachments()
        return {"notes_indexed": notes, "fulltext_indexed": fulls,
                "attachments_indexed": att}

    def reindex_attachments(self) -> int:
        """全量重扫附件文本入 notes_fts；**同时清理文件已不存在的孤儿行**。

        返回写入条数（幂等，纯本地抽取）。孤儿行必须清：用户手删附件文件后，
        旧索引仍会让 agent 召回"某篇 SI 里提到 X"，读回时却 404（P0-B step3 实测）。
        """
        from . import attachments as _att

        total = 0
        lib = self.roots.library_dir
        seen: set[str] = set()
        if lib.is_dir():
            for d in sorted(lib.iterdir()):
                if not d.is_dir() or d.name.startswith("."):
                    continue
                base = d / "attachments"
                if not base.is_dir():
                    continue
                seen.add(d.name)
                total += self._index_attachment_dir(_att, d.name, base)
        # 无父资源的独立根：<项目根>/attachments/<RID>/**
        root = lib.parent / "attachments"
        if root.is_dir():
            for d in sorted(root.iterdir()):
                if not d.is_dir() or d.name.startswith(".") or d.name in seen:
                    continue
                total += self._index_attachment_dir(_att, d.name, d)
        self.prune_attachment_index()
        return total

    def _attachment_row_path(self, rid: str, rel: str):
        """附件索引行 → 磁盘路径（无父资源独立根也尝试）；不存在返回 None。

        `rel` 形如 `attachments/<kind>/<名>`；父资源目录名与 RID 可能不同
        （论文 RID 带 `doi-` 前缀），故对 library 与独立根都做候选。
        """
        from . import attachments as _att

        raw = (rel or "").strip().replace("\\", "/")
        if not raw.startswith("attachments/"):
            return None
        inner = raw[len("attachments/"):]
        for cand in _att.library_candidates(self.roots, rid):
            p = cand / "attachments" / inner
            if p.is_file():
                return p
        p = self.roots.library_dir.parent / "attachments" / (rid or "") / inner
        return p if p.is_file() else None

    def prune_attachment_index(self) -> int:
        """删除附件索引里文件已不存在的行（notes_fts + fulltext_fts 镜像行）。

        fulltext_fts 正文行（纯 doi 键）不含 `::`，一律不动——只清复合键孤儿。
        返回清理条数。
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT doi, filename FROM notes_fts WHERE filename LIKE 'attachments/%'"
            ).fetchall()
            stale = [(r["doi"], r["filename"]) for r in rows
                     if self._attachment_row_path(r["doi"], r["filename"]) is None]
            for rid, rel in stale:
                conn.execute("DELETE FROM notes_fts WHERE doi=? AND filename=?", (rid, rel))
            ft_rows = conn.execute(
                "SELECT doi FROM fulltext_fts WHERE doi LIKE ?",
                (f"%{self.ATTACH_FT_SEP}attachments/%",)).fetchall()
            stale_keys = []
            for r in ft_rows:
                rid, _, rel = (r["doi"] or "").partition(self.ATTACH_FT_SEP)
                if not rel or self._attachment_row_path(rid, rel) is None:
                    stale_keys.append(r["doi"])
            for key in stale_keys:
                conn.execute("DELETE FROM fulltext_fts WHERE doi=?", (key,))
        n = len(stale) + len(stale_keys)
        if n:
            logger.info("附件索引清理：%d 条孤儿行（文件已删除）", n)
        return n

    def _index_attachment_dir(self, att_mod, rid: str, base) -> int:
        """把 `<base>/**` 里的可读文本附件索引到 rid 的 notes_fts 下。"""
        n = 0
        for p in sorted(base.rglob("*")):
            if not p.is_file() or p.name.startswith("."):
                continue
            try:
                rel = p.relative_to(base).as_posix()
            except ValueError:
                continue
            text = att_mod.extract_text(p)
            if not text:
                continue
            self.index_attachment(rid, f"attachments/{rel}", text)
            n += 1
        return n

    @staticmethod
    def _fts_tokenizer(conn, name: str) -> str:
        """读虚拟表 SQL 判断分词器（trigram / unicode61）。"""
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (name,)).fetchone()
        sql = (row["sql"] or "") if row else ""
        return "trigram" if "trigram" in sql else "unicode61"

    def search_notes(self, query: str, limit: int = 10,
                     doi: str | None = None) -> list[dict]:
        """notes_fts 召回；doi 给定则只召回该篇（Q5 阶段2 单篇编译笔记）。

        片段口径（2026-09-21 统一）：**两条路都取"命中位置窗口"**
        （`match_window`，边界对齐、上限 `NOTE_WINDOW_LIMIT`）。旧实现主路用
        SQLite `snippet(...,12)`（token 窗口，中英混排下窗口极不稳定），兜底用
        `substr(content,1,500)` 再改成窗口 ⇒ 同一个通道两种片段口径，长度/边界都对不齐。
        """
        q = _fts_query(query)
        if not q:
            return []
        rows = self._search_notes_raw(q, limit, doi=doi)
        if not rows:
            q2 = _fts_query(query, mode="OR")
            if q2 != q:
                rows = self._search_notes_raw(q2, limit, doi=doi)
        # 中文兜底：trigram 短语需连续子串，整句/分词不连续时用 LIKE 子串匹配
        if not rows and _has_cjk(query):
            rows = self._search_notes_like(query, limit, mode="AND", doi=doi)
            if not rows:
                rows = self._search_notes_like(query, limit, mode="OR", doi=doi)
        anchors = _query_anchors(query)
        return [{"doi": r["doi"], "file": r["filename"],
                 "snippet": match_window(r["content"] or "", anchors,
                                         limit=NOTE_WINDOW_LIMIT)}
                for r in rows]

    def _search_notes_raw(self, q: str, limit: int,
                          doi: str | None = None) -> list[sqlite3.Row]:
        cols = (f"doi, filename, substr(content, 1, {_WINDOW_SCAN_CHARS}) AS content")
        if doi:
            with self._conn() as conn:
                return conn.execute(
                    f"""SELECT {cols} FROM notes_fts WHERE notes_fts MATCH ? AND doi = ?
                       ORDER BY bm25(notes_fts) LIMIT ?""", (q, doi, limit)).fetchall()
        with self._conn() as conn:
            return conn.execute(
                f"""SELECT {cols} FROM notes_fts WHERE notes_fts MATCH ?
                   ORDER BY bm25(notes_fts) LIMIT ?""", (q, limit)).fetchall()

    def _search_notes_like(self, query: str, limit: int,
                           mode: str = "AND",
                           doi: str | None = None) -> list[dict]:
        """CJK 子串 LIKE 兜底：按空白分词，全 AND（严格）或 OR（宽松）匹配。

        2026-09-12 用户反馈修复：**无空格的中文问句**（实际就是用户的日常问法，如
        "这篇文献的创新点是什么"）此前被当成**一个整串**去 LIKE，等于要求笔记里出现
        `这篇文献的创新点是什么` 这段连续文字 → 恒不命中（实测 rows=0），
        于是"单篇编译笔记召回"对中文问句**永远为空**。
        现在：对 CJK 片段额外展开 **2 字窗口（bigram）** 作为 OR 备选，只要笔记里出现
        「创新」「文献」这类二字词就能召回；噪声由 ORDER BY length + limit 兜住。

        返回**原始行**（`content` 列），片段由上层 `search_notes` 统一用
        `match_window` 生成——片段口径只有一处（2026-09-21）。
        """
        chunks = [c for c in query.replace('"', " ").split() if c]
        if not chunks:
            return []
        joiner = " AND " if mode == "AND" else " OR "
        # OR 组必须加括号：`A OR B AND doi = ?` 在 SQL 里等价于 `A OR (B AND doi = ?)`
        # ⇒ doi 过滤只作用在紧邻的那一个 LIKE 上，其余锚点的命中会**跨文献泄漏**
        # （实测：单篇召回返回别篇 `_note.md`，2026-09-21 修复）。
        where = "(" + joiner.join("content LIKE ?" for _ in chunks) + ")"
        params = [f"%{c}%" for c in chunks]
        if doi:
            where += " AND doi = ?"
            params = [*params, doi]
        with self._conn() as conn:
            rows = conn.execute(
                f"""SELECT doi, filename, substr(content, 1, {_WINDOW_SCAN_CHARS}) AS content
                    FROM notes_fts WHERE {where}
                    ORDER BY length(content) LIMIT ?""",
                (*params, limit)).fetchall()
            if not rows and _has_cjk(query):
                # CJK bigram OR 兜底（仅在整串匹配失败时才跑，避免无谓扫描）
                grams = _cjk_grams(query)
                if not grams:
                    return []
                where2 = "(" + " OR ".join("content LIKE ?" for _ in grams) + ")"
                params2 = [f"%{g}%" for g in grams]
                if doi:
                    where2 += " AND doi = ?"
                    params2 = [*params2, doi]
                rows = conn.execute(
                    f"""SELECT doi, filename, substr(content, 1, {_WINDOW_SCAN_CHARS}) AS content
                        FROM notes_fts WHERE {where2}
                        ORDER BY length(content) LIMIT ?""",
                    (*params2, limit)).fetchall()
            return list(rows)

    def notes_files(self, doi: str) -> list[str]:
        """该篇已编译产物文件名（notes_fts 中 DISTINCT filename），无则空。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT filename FROM notes_fts WHERE doi=?",
                (doi,)).fetchall()
        return [r["filename"] for r in rows]

    def search_fulltext(self, query: str, limit: int = 10) -> list[dict]:
        q = _fts_query(query)
        if not q:
            return []
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT doi, snippet(fulltext_fts, 1, '[', ']', '…', 12) AS snip
                   FROM fulltext_fts WHERE fulltext_fts MATCH ?
                   ORDER BY bm25(fulltext_fts) LIMIT ?""", (q, limit)).fetchall()
            if not rows and _has_cjk(query):
                chunks = [c for c in query.replace('"', " ").split() if c]
                if chunks:
                    where = " AND ".join("content LIKE ?" for _ in chunks)
                    params = [f"%{c}%" for c in chunks]
                    rows = conn.execute(
                        f"""SELECT doi, substr(content, 1, {_WINDOW_SCAN_CHARS}) AS content
                            FROM fulltext_fts WHERE {where}
                            ORDER BY length(content) LIMIT ?""",
                        (*params, limit)).fetchall()
                    # 兜底 snippet 取"命中位置窗口"而非文件开头（同 notes_fts 口径）
                    return [{"doi": r["doi"],
                             "snippet": match_window(r["content"] or "", chunks)}
                            for r in rows]
        return [{"doi": r["doi"], "snippet": r["snip"]} for r in rows]

    def notes_content(self, doi: str, filename: str) -> str:
        """取某篇某产物文件内容（问答注入用，限长）。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT content FROM notes_fts WHERE doi=? AND filename=?",
                (doi, filename)).fetchone()
        return (row["content"] if row else "") or ""

    # ---------------------------------------------------------- concepts
    def upsert_concept(self, paper_doi: str, name: str, definition: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO concepts(paper_doi,name,definition,updated_at)
                   VALUES(?,?,?,?)""",
                (paper_doi, name, definition,
                 datetime.now().isoformat(timespec="seconds")))

    def concept_count(self, name: str) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) c FROM concepts WHERE name=?", (name,)).fetchone()
        return row["c"] if row else 0

    def concept_rows(self, name: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT paper_doi, definition FROM concepts WHERE name=?",
                (name,)).fetchall()
        return [dict(r) for r in rows]

    def concepts_for_doi(self, paper_doi: str) -> list[dict]:
        """获取某篇文献的所有概念。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT name, definition FROM concepts WHERE paper_doi=?",
                (paper_doi,)).fetchall()
        return [dict(r) for r in rows]

    def all_concepts(self) -> list[dict]:
        """全表概念行（归一化回灌用）。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT paper_doi, name, definition FROM concepts").fetchall()
        return [dict(r) for r in rows]

    def clear_concepts(self) -> None:
        """清空概念表（归一化回灌前调用；随后重新 upsert）。"""
        with self._conn() as conn:
            conn.execute("DELETE FROM concepts")

    # ---------------------------------------------------------- paperlit 同步
    def sync_lit_meta(self, lit_db_path: Path) -> dict:
        """从 paperlit 的 lit.db 同步文献计量数据到 papers_meta。

        按 DOI join，把 lit.db 的 paper_rank / cocitation_cluster / impact_factor /
        quartile / library_citations / source_main 写回 papers_meta。
        返回 {synced: N, total: M}。
        """
        if not lit_db_path.exists():
            return {"synced": 0, "total": 0, "error": "lit.db 不存在"}
        src = sqlite3.connect(str(lit_db_path))
        src.row_factory = sqlite3.Row
        try:
            rows = src.execute(
                "SELECT doi, paper_rank, cocitation_cluster, impact_factor,"
                " quartile, library_citations, source_main"
                " FROM papers WHERE doi <> ''"
                " AND (paper_rank > 0 OR cocitation_cluster > 0"
                "      OR impact_factor > 0 OR quartile <> ''"
                "      OR library_citations > 0 OR source_main <> '')"
            ).fetchall()
        finally:
            src.close()
        if not rows:
            return {"synced": 0, "total": 0}
        synced = 0
        with self._conn() as conn:
            for r in rows:
                doi = r["doi"]
                rid_row = conn.execute(
                    "SELECT rid FROM identifiers WHERE kind='doi' AND value=?",
                    (doi,)).fetchone()
                if not rid_row:
                    rid_row = conn.execute(
                        "SELECT rid FROM papers_meta WHERE doi=?", (doi,)).fetchone()
                if not rid_row:
                    continue
                rid = rid_row["rid"]
                conn.execute(
                    "UPDATE papers_meta SET"
                    " paper_rank=COALESCE(?,paper_rank),"
                    " cocitation_cluster=COALESCE(?,cocitation_cluster),"
                    " impact_factor=COALESCE(?,impact_factor),"
                    " quartile=CASE WHEN ?<>'' THEN ? ELSE quartile END,"
                    " library_citations=COALESCE(?,library_citations),"
                    " source_main=CASE WHEN ?<>'' THEN ? ELSE source_main END"
                    " WHERE rid=?",
                    (r["paper_rank"] or None,
                     r["cocitation_cluster"] or None,
                     r["impact_factor"] or None,
                     r["quartile"] or "", r["quartile"] or "",
                     r["library_citations"] or None,
                     r["source_main"] or "", r["source_main"] or "",
                     rid))
                synced += 1
        return {"synced": synced, "total": len(rows)}

    def cluster_peers(self, cluster: int, exclude_doi: str = "",
                      limit: int = 5) -> list[dict]:
        """同共被引聚类的其他文献（按 paper_rank 降序）。"""
        if cluster <= 0:
            return []
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT doi, title, year, journal, times_cited, paper_rank"
                " FROM papers_meta"
                " WHERE cocitation_cluster = ? AND doi <> ? AND doi <> ''"
                " ORDER BY paper_rank DESC LIMIT ?",
                (cluster, exclude_doi or "", limit)).fetchall()
        return [dict(r) for r in rows]


def _json(value) -> str:
    import json
    return json.dumps(value, ensure_ascii=False)


def _row_to_meta(row: sqlite3.Row | None) -> PaperMeta | None:
    if row is None:
        return None
    import json
    return PaperMeta(
        rid=row["rid"] or "",
        doi=row["doi"] or "",
        title=row["title"] or "",
        abstract=row["abstract"] or "",
        authors=json.loads(row["authors_json"] or "[]"),
        affiliations=json.loads(row["affiliations_json"] or "[]"),
        corresponding=(json.loads(row["corresponding_json"] or "[]")
                       if "corresponding_json" in row.keys() else []),
        journal=row["journal"] or "",
        year=row["year"] or "",
        month=row["month"] or "",
        issn=row["issn"] or "",
        eissn=row["eissn"] or "",
        keywords=json.loads(row["keywords_json"] or "[]"),
        research_areas=json.loads(row["research_areas_json"] or "[]"),
        wos_categories=json.loads(row["wos_categories_json"] or "[]"),
        funding=row["funding"] or "",
        times_cited=row["times_cited"] or 0,
        wos_id=row["wos_id"] or "",
        references=_refs_from_json(row["references_json"]),
        source_file=row["source_file"] or "",
        imported_at=row["imported_at"] or "",
        paper_id=row["paper_id"],
        journal_override=row["journal_override"] or "" if "journal_override" in row.keys() else "",
        kind=(row["kind"] if "kind" in row.keys() else "") or "",
        ai_value_score=(row["ai_value_score"] if "ai_value_score" in row.keys() else None),
        topic_score=(row["topic_score"] if "topic_score" in row.keys() else None),
        paper_rank=(row["paper_rank"] if "paper_rank" in row.keys() else None) or 0.0,
        cocitation_cluster=(row["cocitation_cluster"] if "cocitation_cluster" in row.keys() else None) or 0,
        impact_factor=(row["impact_factor"] if "impact_factor" in row.keys() else None) or 0.0,
        quartile=(row["quartile"] if "quartile" in row.keys() else "") or "",
        library_citations=(row["library_citations"] if "library_citations" in row.keys() else None) or 0,
        source_main=(row["source_main"] if "source_main" in row.keys() else "") or "",
    )


def _refs_from_json(raw: str) -> list:
    from .models import CitedRef
    import json
    try:
        return [CitedRef(**x) for x in json.loads(raw or "[]")]
    except (TypeError, ValueError):
        return []


def _fts_query(query: str, mode: str = "AND") -> str:
    """用户查询 → FTS5 短语查询（引号包裹防注入；AND 严格 / OR 宽松回退）。"""
    q = query.strip()
    if not q:
        return ""
    tokens = [t for t in q.replace('"', " ").split() if t]
    if not tokens:
        return ""
    joiner = " AND " if mode == "AND" else " OR "
    return joiner.join(f'"{t}"' for t in tokens[:8])


def _has_cjk(text: str) -> bool:
    """是否含 CJK 统一表意文字（触发 LIKE 子串兜底）。"""
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


# CJK 兜底命中窗口：只在文件前 100k 字符里定位（更长的文本定位不划算，退化开头窗口）
_WINDOW_SCAN_CHARS = 100_000

# 笔记片段上限（字符）：与向量路的 `vector.SNIPPET_CHARS=1200` **同口径**。
# 旧值 700 太窄——实测 1625 字的 `_note.md` 里「研究结果」的数值落在偏移 603~1100，
# 700 字窗口怎么切都取不到，模型只能答"数值被截断"（而产物里明明有）。
NOTE_WINDOW_LIMIT = 1200


def match_window(content: str, anchors: list[str], radius: int = 300,
                 limit: int = NOTE_WINDOW_LIMIT,
                 slack: int = 300) -> str:
    """命中位置附近的文本窗口（替代 `substr(content,1,N)` 的"文件开头"口径）。

    在**所有**锚点里取最早出现的位置——只试前几个会漏（bigram 的前 3 个二字词未必
    出现在该文件里，旧写法会返回空片段 ⇒ 模型拿到"有引用、无证据"），然后取 ±radius
    窗口并按行/句边界收尾。锚点全没出现（如超出扫描上限）→ 退化为开头窗口。

    `slack`（2026-09-21 修）：窗口必须**比 limit 宽**，否则 `len(seg) <= limit` 时
    `boundary_trim` 判定"无需裁"直接原样返回 ⇒ 右边界是**硬切**（实测尾部出现
    「…剩磁仅1.」「…为水基的」这类半句，与"按行/句收尾"的承诺不符）。
    多取 slack 字再 trim，右边界才会真正落到 段落>行>句>逗号 上。
    """
    text = content or ""
    if not text:
        return ""
    pos = -1
    for a in anchors:
        if not a:
            continue
        p = text.find(a)
        if p >= 0 and (pos < 0 or p < pos):
            pos = p
    if pos < 0:
        return boundary_trim(text.strip(), limit)
    start = max(0, pos - radius)
    seg = text[start:start + limit + max(0, slack)].strip()
    nl = seg.find("\n")
    if 0 <= nl <= 200:      # 去掉开头半截行
        seg = seg[nl + 1:].lstrip()
    return boundary_trim(seg, limit)


def _query_anchors(query: str) -> list[str]:
    """片段定位锚点（`match_window` 用）：空白分词优先，再补 CJK bigram。

    两条路（FTS 主路 / LIKE 兜底）共用同一份锚点 ⇒ 片段口径只有一处。
    """
    chunks = [c for c in (query or "").replace('"', " ").split() if c]
    return list(dict.fromkeys([*chunks, *_cjk_grams(query)]))


def _cjk_grams(query: str, n: int = 2, cap: int = 12) -> list[str]:
    """CJK 查询 → 2 字窗口（bigram）候选，供 LIKE OR 兜底用。

    为什么是 bigram：中文没有空格，`content LIKE '%整句%'` 恒不中；而两字词（"创新"/"文献"）
    才是笔记里真实出现的可匹配单元。切窗口前先剥掉非 CJK 字符（英文/标点/数字不参与），
    去重后按出现顺序截断到 `cap` 个，避免超长问句生成上百个 OR 条件。
    """
    cjk = "".join(ch for ch in query if "\u4e00" <= ch <= "\u9fff")
    if len(cjk) < n:
        return []
    seen: list[str] = []
    for i in range(len(cjk) - n + 1):
        g = cjk[i:i + n]
        if g not in seen:
            seen.append(g)
        if len(seen) >= cap:
            break
    return seen


# ---------------------------------------------------------------- 查询向量缓存（数据层）
class QueryVecCache:
    """查询文本 → 向量的缓存（独立 SQLite：`data/vector/query_cache.db`）。

    为什么放数据层（2026-09-21）：`sqlite3.connect` 只允许出现在数据层
    （`backend/tests/test_version_contract.py` 守卫），向量模块是业务模块——
    业务模块走统一接口，裸连接一律放这里。

    定位：**派生缓存**，不是权威数据。丢了只是重算（多花几次 embedding），
    因此表结构不参与迁移/版本契约，`get/put` 全部吞异常（缓存故障不影响检索）。

    只缓存**查询**向量（几十 token/次），不缓存文档向量（文档走索引本体）。
    收益点是 L3 候选检索：同一批关键词会在多篇文献里反复搜，命中率高；
    问答的自然语言问句命中率低，但代价只是一次点查。
    """

    MAX_ROWS = 5000

    def __init__(self, roots: Roots):
        self.path = roots.vector_dir / "query_cache.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=10)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS qvec(
                   model TEXT NOT NULL, qhash TEXT NOT NULL, dim INTEGER NOT NULL,
                   vec BLOB NOT NULL, hits INTEGER DEFAULT 0, created_at TEXT DEFAULT '',
                   PRIMARY KEY(model, qhash))""")
        self._conn.commit()

    def get(self, model: str, query: str) -> list[float]:
        import array

        qh = _md5_text((query or "").strip())
        try:
            row = self._conn.execute(
                "SELECT vec, dim FROM qvec WHERE model=? AND qhash=?",
                (model, qh)).fetchone()
            if not row:
                return []
            self._conn.execute(
                "UPDATE qvec SET hits=hits+1 WHERE model=? AND qhash=?", (model, qh))
            self._conn.commit()
            vec = array.array("f")
            vec.frombytes(row[0])
            return list(vec) if len(vec) == row[1] else []
        except Exception:  # noqa: BLE001 - 缓存异常不影响检索
            return []

    def put(self, model: str, query: str, vec: list[float]) -> None:
        import array

        qh = _md5_text((query or "").strip())
        try:
            blob = array.array("f", [float(x) for x in vec]).tobytes()
            self._conn.execute(
                """INSERT OR REPLACE INTO qvec(model,qhash,dim,vec,hits,created_at)
                   VALUES(?,?,?,?,0,datetime('now'))""",
                (model, qh, len(vec), blob))
            n = self._conn.execute("SELECT COUNT(*) FROM qvec").fetchone()[0]
            if n > self.MAX_ROWS:      # 简单 LRU：删最旧/最少命中的 10%
                self._conn.execute(
                    """DELETE FROM qvec WHERE (model,qhash) IN (
                           SELECT model,qhash FROM qvec
                           ORDER BY created_at ASC, hits ASC LIMIT ?)""",
                    (max(1, n - int(self.MAX_ROWS * 0.9)),))
            self._conn.commit()
        except Exception:  # noqa: BLE001
            pass


def _md5_text(text: str) -> str:
    return hashlib.md5((text or "").encode("utf-8")).hexdigest()


# ==============================================================================
# KB 向量索引存储（`IndexStore`）：派生索引 = SQLite 元数据 + 段式向量文件 + 进程级单例。
#
# 为什么不是整份 JSON + 整份 npy：10 万篇（≈104 万块）下每编译一篇都要全量重写
# （≈250MB JSON + ≈4.3GB 向量）⇒ 写放大 ≈2× 体积/篇、全量重建 O(N²)。现在元数据按行
# upsert 进 `data/vector/kb_index.db`，向量**只追加**到 `kb_vectors/seg-*.f32`
# （裸 float32 行主序，open(...,'ab') 即 O(1)），空间回收走 `compact()`。
#
# 归属：`data/vector/` 是**派生索引**（可由 knowledge_base 编译产物重建），不是权威数据，
# 因此不进 `data/manifest.json` 的五库版本契约，自带 `meta.schema_version`（新于代码 fail-fast）。
# 注意：裸 `sqlite3.connect` 只允许在数据层——本文件即数据层，**不要再往
# `test_version_contract.CONNECT_ALLOW` 里加条目**（那是替自己的代码削弱守卫）。
# ==============================================================================

VEC_DB_NAME = "kb_index.db"
VEC_SCHEMA_VERSION = 2
VEC_SEG_DTYPE = np.float32

_VEC_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
    k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS segments(
    name TEXT PRIMARY KEY, rows INTEGER NOT NULL, dim INTEGER NOT NULL,
    created REAL NOT NULL, sealed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS passages(
    key TEXT PRIMARY KEY, doi TEXT NOT NULL DEFAULT '', ptype TEXT NOT NULL DEFAULT '',
    chunk INTEGER NOT NULL DEFAULT 0, section TEXT NOT NULL DEFAULT '',
    file TEXT NOT NULL DEFAULT '', start INTEGER NOT NULL DEFAULT -1,
    end INTEGER NOT NULL DEFAULT -1, hash TEXT NOT NULL DEFAULT '',
    chunk_md5 TEXT NOT NULL DEFAULT '', seg TEXT NOT NULL DEFAULT '',
    row INTEGER NOT NULL DEFAULT -1);
CREATE INDEX IF NOT EXISTS ix_passages_doi ON passages(doi);
CREATE INDEX IF NOT EXISTS ix_passages_seg ON passages(seg, row);
CREATE TABLE IF NOT EXISTS papers_state(
    doi TEXT PRIMARY KEY, passages INTEGER NOT NULL DEFAULT 0,
    updated REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS dead_letter(
    id INTEGER PRIMARY KEY AUTOINCREMENT, doi TEXT NOT NULL DEFAULT '',
    op TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
    tries INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0,
    created REAL NOT NULL DEFAULT 0, resolved INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_dead_due ON dead_letter(resolved, next_try);
"""

_VEC_PASSAGE_COLS = ("key", "doi", "ptype", "chunk", "section", "file", "start", "end",
                 "hash", "chunk_md5", "seg", "row")


class IndexStore:
    """`data/vector/` 的读写入口（元数据走 SQLite，向量走段文件）。

    线程内单写者：每个方法各自开一次短连接（WAL），无长事务、无内存等价物驻留。
    """

    def __init__(self, roots: Roots):
        self.roots = roots
        self.index_dir = Path(roots.vector_dir) / "kb_vectors"
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = Path(roots.vector_dir) / VEC_DB_NAME
        self._ensure_schema()

    # ---------------------------------------------------------------- 连接 / 建表
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_VEC_SCHEMA)
            cur = conn.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
            if cur is None:
                conn.execute("INSERT INTO meta(k, v) VALUES('schema_version', ?)",
                             (str(VEC_SCHEMA_VERSION),))
            elif int(cur["v"]) > VEC_SCHEMA_VERSION:
                raise RuntimeError(
                    f"kb_index.db schema 版本 {cur['v']} 新于本代码支持的 {VEC_SCHEMA_VERSION}"
                    "（数据由更新版本写入，拒绝部分读取）")
            conn.commit()

    # ---------------------------------------------------------------- meta 键值
    def get_meta(self, key: str, default: str = "") -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return row["v"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", (key, str(value)))
            conn.commit()

    # ---------------------------------------------------------------- 段管理
    def _seg_path(self, name: str) -> Path:
        return self.index_dir / f"{name}.f32"

    def _active_segment(self, conn: sqlite3.Connection) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM segments WHERE sealed=0 ORDER BY created DESC LIMIT 1").fetchone()

    def _next_seg_name(self, conn: sqlite3.Connection) -> str:
        """取下一个段名（**单调计数器**，存在 meta 里）。

        不能用 `COUNT(*)`：compact() 会先清空 segments 再建新段，按行数取名会与刚删掉的
        旧段重名，随后"删旧段文件"就把新段删了（实测踩过：compact 后索引全空）。
        """
        row = conn.execute("SELECT v FROM meta WHERE k='next_seg'").fetchone()
        n = int(row["v"]) if row else 1
        conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('next_seg', ?)", (str(n + 1),))
        return f"seg-{n:06d}"

    def _ensure_active(self, conn: sqlite3.Connection, dim: int) -> sqlite3.Row:
        row = self._active_segment(conn)
        if row is not None:
            if int(row["dim"]) != dim:
                # 维度变了（换 embedding 模型）：封旧段，另起新段，绝不混用向量空间
                conn.execute("UPDATE segments SET sealed=1 WHERE name=?", (row["name"],))
            else:
                return row
        name = self._next_seg_name(conn)
        conn.execute("INSERT INTO segments(name, rows, dim, created, sealed) VALUES(?,?,?,?,0)",
                     (name, 0, dim, time.time()))
        return conn.execute("SELECT * FROM segments WHERE name=?", (name,)).fetchone()

    # ---------------------------------------------------------------- 写
    def append(self, rows: list[dict], vectors: np.ndarray) -> int:
        """追加一批 passage（元数据行 + 向量行）。返回写入向量数。

        `rows[i]` 与 `vectors[i]` 一一对应；已存在的 key 会被重新指向新行
        （旧行成为垃圾，由 `compact()` 回收）——追加语义保证写放大与索引体积无关。
        """
        if not rows:
            return 0
        vecs = np.asarray(vectors, dtype=VEC_SEG_DTYPE)
        if vecs.ndim != 2 or vecs.shape[0] != len(rows):
            raise ValueError(f"rows/vectors 数量不符: {len(rows)} vs {vecs.shape}")
        dim = int(vecs.shape[1])
        seen: set[str] = set()
        with self._connect() as conn:
            seg = self._ensure_active(conn, dim)
            base = int(seg["rows"])
            path = self._seg_path(seg["name"])
            with open(path, "ab") as fh:     # O(1) 追加（段文件是裸 float32 行主序）
                fh.write(vecs.tobytes(order="C"))
            conn.execute("UPDATE segments SET rows=rows+? WHERE name=?",
                         (len(rows), seg["name"]))
            for i, meta in enumerate(rows):
                conn.execute(
                    """INSERT OR REPLACE INTO passages(
                           key,doi,ptype,chunk,section,file,start,end,hash,chunk_md5,seg,row)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (meta.get("key", ""), meta.get("doi", ""), meta.get("ptype", ""),
                     int(meta.get("chunk") or 0), meta.get("section", ""),
                     meta.get("file", ""), int(meta.get("start", -1) or -1),
                     int(meta.get("end", -1) or -1), meta.get("hash", ""),
                     meta.get("chunk_md5", ""), seg["name"], base + i))
                seen.add(meta.get("doi", ""))
            now = time.time()
            for doi in seen:
                if not doi:
                    continue
                conn.execute(
                    """INSERT INTO papers_state(doi, passages, updated) VALUES(?,?,?)
                       ON CONFLICT(doi) DO UPDATE SET
                           passages=(SELECT COUNT(*) FROM passages WHERE doi=excluded.doi),
                           updated=excluded.updated""", (doi, 0, now))
            conn.commit()
        return len(rows)

    def keys_for(self, doi: str, ptype: str) -> list[str]:
        """某篇某产物已有的全部 key（走 `ix_passages_doi`）。

        用于"清理该篇该产物的陈旧块"：旧实现每次编译都**扫描整个内存 `_key_meta`**
        找同 DOI 的键 ⇒ O(N)/篇 ⇒ 全量重建 O(N²)（10 万篇实测尾段每篇慢 3.5 倍）。
        """
        with self._connect() as conn:
            rows = conn.execute("SELECT key FROM passages WHERE doi=? AND ptype=?",
                                (doi, ptype)).fetchall()
        return [r["key"] for r in rows]

    def save_passage_meta(self, rows: list[dict]) -> int:
        """只更新 passage 的**元数据列**（不动 seg/row）：内容 hash 未变、仅偏移/小节刷新的块。

        若 key 不存在（无向量的孤儿）则忽略——元数据必须指向真实向量行，否则
        命中块会出现"有指针没数据"。
        """
        if not rows:
            return 0
        n = 0
        with self._connect() as conn:
            for m in rows:
                cur = conn.execute(
                    """UPDATE passages SET doi=?, ptype=?, chunk=?, section=?, file=?,
                           start=?, end=?, hash=?, chunk_md5=? WHERE key=?""",
                    (m.get("doi", ""), m.get("ptype", ""), int(m.get("chunk") or 0),
                     m.get("section", ""), m.get("file", ""), int(m.get("start", -1) or -1),
                     int(m.get("end", -1) or -1), m.get("hash", ""), m.get("chunk_md5", ""),
                     m.get("key", "")))
                n += cur.rowcount or 0
            conn.commit()
        return n

    def delete(self, keys: list[str]) -> int:
        """按 key 删除元数据行（向量行成为垃圾，由 compact() 回收）。"""
        keys = [k for k in (keys or []) if k]
        if not keys:
            return 0
        with self._connect() as conn:
            n = 0
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                marks = ",".join("?" * len(chunk))
                cur = conn.execute(f"DELETE FROM passages WHERE key IN ({marks})", chunk)
                n += cur.rowcount or 0
            conn.commit()
        return n

    def refresh_papers_state(self, dois: list[str] | None = None) -> None:
        """重算 papers_state.passages（doi 为空则全量）。"""
        with self._connect() as conn:
            if dois:
                for doi in dois:
                    if not doi:
                        continue
                    conn.execute(
                        """INSERT INTO papers_state(doi, passages, updated) VALUES(?,?,?)
                           ON CONFLICT(doi) DO UPDATE SET
                               passages=(SELECT COUNT(*) FROM passages WHERE doi=?),
                               updated=excluded.updated""",
                        (doi, 0, time.time(), doi))
            else:
                conn.execute("DELETE FROM papers_state")
                conn.execute(
                    """INSERT INTO papers_state(doi, passages, updated)
                       SELECT doi, COUNT(*), ? FROM passages WHERE doi<>'' GROUP BY doi""",
                    (time.time(),))
            conn.commit()

    # ---------------------------------------------------------------- 读
    def load(self) -> tuple[list[str], dict[str, dict], np.ndarray | None]:
        """加载全量索引：`(idx_to_key, key_meta, vectors)`，三者行序严格对齐。

        行序 = 段创建顺序 × 段内 row；仅保留 `passages` 仍指向的行（被刷新/删除的旧行
        只占文件空间、不进结果）。
        """
        with self._connect() as conn:
            segs = conn.execute("SELECT * FROM segments ORDER BY created, name").fetchall()
            live: dict[str, dict[int, str]] = {}      # seg → {row: key}
            for r in conn.execute("SELECT key, seg, row FROM passages WHERE seg<>''"):
                live.setdefault(r["seg"], {})[int(r["row"])] = r["key"]
            keys: list[str] = []
            parts: list[np.ndarray] = []
            for s in segs:
                name = s["name"]
                want = live.get(name) or {}
                if not want:
                    continue
                path = self._seg_path(name)
                if not path.is_file():
                    logger.warning("段文件缺失（跳过）: %s", path)
                    continue
                rows = int(s["rows"])
                dim = int(s["dim"])
                arr = np.fromfile(path, dtype=VEC_SEG_DTYPE)
                if rows and arr.size < rows * dim:
                    logger.warning("段文件截断（跳过）: %s", path)
                    continue
                arr = arr[:rows * dim].reshape(rows, dim)
                order = sorted(want)
                parts.append(arr[order])
                keys.extend(want[r] for r in order)
            meta: dict[str, dict] = {}
            for r in conn.execute(f"SELECT {','.join(_VEC_PASSAGE_COLS)} FROM passages"):
                meta[r["key"]] = {c: r[c] for c in _VEC_PASSAGE_COLS if c != "key"}
        vectors = np.concatenate(parts) if parts else None
        if vectors is not None and len(vectors) != len(keys):
            logger.warning("索引行数不一致（keys=%d vec=%d），丢弃以免错位", len(keys),
                           len(vectors))
            return [], {}, None
        return keys, meta, vectors

    def size(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) AS c FROM passages").fetchone()["c"])

    # ---------------------------------------------------------------- 压实 / 健康
    def compact(self) -> dict:
        """把 live 行重写为**单一段**，删旧段与旧文件（原子切换）。

        触发时机：垃圾行占比过高（刷新/删除累积）或显式重建。单事务内完成
        segments/passages 指针切换，中途崩不会出现"半切换"（SQLite 原子性保证）。
        """
        keys, meta, vectors = self.load()
        with self._connect() as conn:
            old = conn.execute("SELECT SUM(rows) AS r, COUNT(*) AS c FROM segments").fetchone()
            old_rows, old_segs = int(old["r"] or 0), int(old["c"])
            names = [r["name"] for r in conn.execute("SELECT name FROM segments")]
            conn.execute("DELETE FROM segments")
            conn.execute("DELETE FROM passages")
            if keys:
                dim = int(vectors.shape[1])
                name = self._next_seg_name(conn)
                conn.execute(
                    "INSERT INTO segments(name, rows, dim, created, sealed) VALUES(?,?,?,?,1)",
                    (name, len(keys), dim, time.time()))
                path = self._seg_path(name)
                arr = np.ascontiguousarray(vectors, dtype=VEC_SEG_DTYPE)
                with open(path, "wb") as fh:
                    fh.write(arr.tobytes(order="C"))
                for i, k in enumerate(keys):
                    m = meta[k]
                    conn.execute(
                        """INSERT OR REPLACE INTO passages(
                               key,doi,ptype,chunk,section,file,start,end,hash,chunk_md5,seg,row)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (k, m.get("doi", ""), m.get("ptype", ""), int(m.get("chunk") or 0),
                         m.get("section", ""), m.get("file", ""), int(m.get("start", -1)),
                         int(m.get("end", -1)), m.get("hash", ""), m.get("chunk_md5", ""),
                         name, i))
            conn.commit()
        for nm in names:                     # 元数据事务已提交后才删物理文件
            p = self._seg_path(nm)
            if p.is_file():
                try:
                    p.unlink()
                except OSError as e:  # noqa: PERF203 - 删不掉只损失磁盘空间
                    logger.warning("旧段文件删除失败: %s (%s)", p, e)
        stats = {"segments_before": old_segs, "rows_before": old_rows,
                 "rows_after": len(keys), "reclaimed_rows": max(0, old_rows - len(keys))}
        logger.info("向量索引压实: %s", stats)
        return stats

    def health(self) -> dict:
        """索引健康快照（只读；供 UI「索引健康」卡片与巡检接口）。"""
        with self._connect() as conn:
            segs = conn.execute(
                "SELECT COUNT(*) AS c, COALESCE(SUM(rows),0) AS r FROM segments").fetchone()
            live = conn.execute("SELECT COUNT(*) AS c FROM passages").fetchone()["c"]
            papers = conn.execute("SELECT COUNT(*) AS c FROM papers_state").fetchone()["c"]
            dead = conn.execute(
                "SELECT COUNT(*) AS c FROM dead_letter WHERE resolved=0").fetchone()["c"]
            schema = conn.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
        missing = [s for s in self._segment_names()
                   if not self._seg_path(s).is_file()]
        return {
            "schema_version": int(schema["v"]) if schema else 0,
            "db_path": str(self.db_path),
            "segments": int(segs["c"]),
            "segment_rows": int(segs["r"]),
            "live_passages": int(live),
            "garbage_rows": max(0, int(segs["r"]) - int(live)),
            "papers": int(papers),
            "dead_letters": int(dead),
            "missing_segments": missing,
            "bytes": self.db_bytes(),
        }

    def _segment_names(self) -> list[str]:
        with self._connect() as conn:
            return [r["name"] for r in conn.execute("SELECT name FROM segments")]

    def db_bytes(self) -> int:
        total = self.db_path.stat().st_size if self.db_path.is_file() else 0
        for p in self.index_dir.glob("*.f32"):
            try:
                total += p.stat().st_size
            except OSError:
                pass
        return total

    # ---------------------------------------------------------------- 死信（编译/索引失败重试）
    def add_dead_letter(self, doi: str, op: str, error: str, delay_sec: float = 300.0) -> int:
        """登记一条失败作业（带退避 next_try）。

        同一 (doi, op) 未解决的旧条目会累加 tries 并顺延重试时间，不重复堆积。
        """
        now = time.time()
        with self._connect() as conn:
            old = conn.execute(
                "SELECT * FROM dead_letter WHERE doi=? AND op=? AND resolved=0",
                (doi, op)).fetchone()
            if old:
                tries = int(old["tries"]) + 1
                backoff = min(delay_sec * (2 ** (tries - 1)), 24 * 3600)
                conn.execute(
                    "UPDATE dead_letter SET tries=?, error=?, next_try=? WHERE id=?",
                    (tries, error, now + backoff, old["id"]))
                conn.commit()
                return int(old["id"])
            cur = conn.execute(
                """INSERT INTO dead_letter(doi, op, error, tries, next_try, created, resolved)
                   VALUES(?,?,?,1,?,?,0)""", (doi, op, error, now + delay_sec, now))
            conn.commit()
            return int(cur.lastrowid)

    def due_dead_letters(self, now: float | None = None, limit: int = 50) -> list[dict]:
        now = time.time() if now is None else now
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM dead_letter WHERE resolved=0 AND next_try<=?
                   ORDER BY next_try LIMIT ?""", (now, limit)).fetchall()
        return [dict(r) for r in rows]

    def dead_letters(self, limit: int = 200) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM dead_letter WHERE resolved=0 ORDER BY created DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    def resolve_dead_letter(self, dl_id: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE dead_letter SET resolved=1 WHERE id=?", (dl_id,))
            conn.commit()

    def resolve_for(self, doi: str, op: str) -> int:
        """某篇某操作的死信全部结案（成功重试后清账）。"""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE dead_letter SET resolved=1 WHERE doi=? AND op=? AND resolved=0",
                (doi, op))
            conn.commit()
            return cur.rowcount or 0

    # ---------------------------------------------------------------- 旧格式探测（不做迁移）
    def legacy_files_present(self) -> bool:
        """旧格式（整份 JSON + 整份 npy）是否还在（本代码不读它，仅用于提示重建）。"""
        return ((self.index_dir / "kb_index_meta.json").is_file()
                or (self.index_dir / "kb_vectors.npy").is_file())


# ---------------------------------------------------------------- 进程级单例
_stores: dict[str, IndexStore] = {}


def get_index_store(roots: Roots) -> IndexStore:
    """按 vector 根路径取单例（不同 tmp 目录的测试互不串档）。"""
    key = str(Path(roots.vector_dir).resolve())
    if key not in _stores:
        _stores[key] = IndexStore(roots)
    return _stores[key]


def reset_index_store(roots: Roots | None = None) -> None:
    """丢弃单例（测试/换根路径用；不关连接——本实现每次操作短连接）。"""
    if roots is None:
        _stores.clear()
        return
    _stores.pop(str(Path(roots.vector_dir).resolve()), None)


def import_legacy(store: IndexStore) -> dict | None:
    """把旧格式（kb_index_meta.json + kb_vectors.npy）一次性导入新存储。

    **不是读时兼容**：仅在显式重建/迁移时调用，导入后旧文件可删。
    """
    meta_path = store.index_dir / "kb_index_meta.json"
    vec_path = store.index_dir / "kb_vectors.npy"
    if not meta_path.is_file() or not vec_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        order = list(meta.get("idx_to_key") or [])
        kmeta = meta.get("key_meta") or {}
        vectors = np.load(vec_path)
    except Exception as e:  # noqa: BLE001
        logger.warning("旧索引导入失败: %s", e)
        return None
    if not order or vectors is None or len(vectors) != len(order):
        return None
    for k in order:
        m = kmeta.get(k) or {}
        m.setdefault("doi", "")
        m.setdefault("ptype", "")
        m["key"] = k
    rows = [kmeta[k] for k in order]
    store.append(rows, vectors.astype(VEC_SEG_DTYPE))
    store.set_meta("model", str(meta.get("model") or ""))
    store.refresh_papers_state()
    store.compact()
    logger.info("旧向量索引已导入 SQLite + 段文件: %d 行", len(order))
    return {"imported": len(order)}
