# -*- coding: utf-8 -*-
"""文献计量图谱：引用网络导出（节点 + 边 + 过滤 + 分面 + 邻居）。

设计目标：
- **服务端过滤/聚合**：年份 / 库内被引 / 文献被引 / 影响因子 / 分区 / 是否在知识库 /
  聚类 / 节点上限，全部在 SQL 层完成，前端只渲染，避免 10 万节点卡顿。
- **前后端分离**：本模块只返回**原始属性**（library_citations / impact_factor / ...），
  节点大小、颜色、标签的视觉映射由前端按用户设置计算（见 frontend/js/graph/scales.js）。
- **KB 成员标记解耦**：paperlit 不依赖 paperkb；知识库 DOI 集合由上层（LitService）注入。

引用边语义（citations 表）：``citing_doi → cited_doi`` 表示 *citing 引用了 cited*。
  - 「引用了 X 的文献」= ``cited_doi=X`` 的 ``citing_doi``（X 的被引方）
  - 「X 引用的文献」  = ``citing_doi=X`` 的 ``cited_doi``（X 的参考方）
"""
from __future__ import annotations

import logging
import sqlite3

from ..db import LitStore

logger = logging.getLogger(__name__)

# 节点上限硬保护：即使前端请求更多也不超过此值（防内存/传输爆炸）。
MAX_NODE_LIMIT = 100_000
DEFAULT_NODE_LIMIT = 5_000

# 允许的排序键 → SQL 列（白名单，防注入）。
_SORT_COLUMNS = {
    "library_citations": "library_citations",
    "times_cited": "times_cited",
    "paper_rank": "paper_rank",
    "impact_factor": "impact_factor",
    "year": "year",
}

# 节点 SELECT 列（轻量，不含 abstract/keywords 等大字段——详情走 /graph/node）。
_NODE_COLUMNS = (
    "doi, title, year, journal, times_cited, library_citations, "
    "impact_factor, quartile, paper_rank, cocitation_cluster, is_reference"
)


def _year_conditions(year_min: int | None, year_max: int | None) -> tuple[list[str], list]:
    """年份过滤条件（year 为 TEXT，仅对 4 位数字年份做数值比较）。"""
    if year_min is None and year_max is None:
        return [], []
    conds = ["year GLOB '[0-9][0-9][0-9][0-9]'"]
    params: list = []
    if year_min is not None:
        conds.append("CAST(year AS INTEGER) >= ?")
        params.append(int(year_min))
    if year_max is not None:
        conds.append("CAST(year AS INTEGER) <= ?")
        params.append(int(year_max))
    return conds, params


def _build_node_filter(*, year_min, year_max, min_library_citations,
                       min_times_cited, min_impact_factor, quartiles,
                       cluster, exclude_references) -> tuple[str, list]:
    """拼装节点 WHERE 子句（参数化，白名单分区）。"""
    where = ["1=1"]
    params: list = []

    yconds, yparams = _year_conditions(year_min, year_max)
    where.extend(yconds)
    params.extend(yparams)

    if min_library_citations is not None:
        where.append("library_citations >= ?")
        params.append(int(min_library_citations))
    if min_times_cited is not None:
        where.append("times_cited >= ?")
        params.append(int(min_times_cited))
    if min_impact_factor is not None:
        where.append("impact_factor >= ?")
        params.append(float(min_impact_factor))
    if quartiles:
        allowed = [q for q in quartiles if q in ("Q1", "Q2", "Q3", "Q4")]
        if allowed:
            placeholders = ",".join("?" for _ in allowed)
            where.append(f"quartile IN ({placeholders})")
            params.extend(allowed)
    if cluster is not None:
        where.append("cocitation_cluster = ?")
        params.append(int(cluster))
    if exclude_references:
        where.append("is_reference = 0")

    return " AND ".join(where), params


def _create_selected_temp(conn: sqlite3.Connection, dois) -> None:
    """建临时表 selected(doi)，供边 JOIN 用（10 万节点也高效，避开 IN 参数上限）。"""
    conn.execute("DROP TABLE IF EXISTS temp.selected")
    conn.execute("CREATE TEMP TABLE selected (doi TEXT PRIMARY KEY)")
    conn.executemany(
        "INSERT OR IGNORE INTO selected(doi) VALUES (?)",
        [(d,) for d in dois],
    )


def build_network(store: LitStore, *,
                  year_min: int | None = None,
                  year_max: int | None = None,
                  min_library_citations: int | None = None,
                  min_times_cited: int | None = None,
                  min_impact_factor: float | None = None,
                  quartiles: list[str] | None = None,
                  cluster: int | None = None,
                  exclude_references: bool = False,
                  in_kb_only: bool = False,
                  sort_by: str = "library_citations",
                  limit: int = DEFAULT_NODE_LIMIT,
                  kb_dois: set[str] | None = None,
                  preview: bool = False,
                  citation_source: str = "filtered",
                  exclude_isolated: bool = True) -> dict:
    """导出引用网络（节点 + 边）。

    Args:
        store: LitStore 实例。
        year_min/year_max: 年份范围（含端点）。
        min_library_citations: 库内被引下限（AI检索数据库内被引次数）。
        min_times_cited: 文献本身被引下限。
        min_impact_factor: 影响因子下限。
        quartiles: 保留的 JCR 分区列表（如 ['Q1','Q2']）。
        cluster: 仅返回指定共被引聚类。
        exclude_references: 排除「仅作为参考文献引入」的节点。
        in_kb_only: 仅返回在用户知识库中的文献（需注入 kb_dois）。
        sort_by: 截断时的保留优先级（library_citations/times_cited/paper_rank/impact_factor/year）。
        limit: 节点上限（超过则按 sort_by 取 Top-N，meta.truncated=True）。
        kb_dois: 用户知识库 DOI 集合（标记 in_kb；由上层注入，paperlit 不依赖 paperkb）。
        citation_source: 被引来源（wos/library/filtered，默认 filtered）。
            - wos: 使用 times_cited（WoS 数据库被引）
            - library: 使用 library_citations（库内被引）
            - filtered: 动态计算筛选集内被引（入度）
        exclude_isolated: 排除孤立节点（度数为0，默认 True）。

    Returns:
        {"nodes": [...], "edges": [{"source","target"}...], "meta": {...}}
    """
    limit = max(1, min(int(limit), MAX_NODE_LIMIT))
    sort_col = _SORT_COLUMNS.get(sort_by, "library_citations")
    kb_dois = kb_dois or set()
    # 校验被引来源参数
    if citation_source not in ("wos", "library", "filtered"):
        citation_source = "filtered"

    where, params = _build_node_filter(
        year_min=year_min, year_max=year_max,
        min_library_citations=min_library_citations,
        min_times_cited=min_times_cited,
        min_impact_factor=min_impact_factor,
        quartiles=quartiles, cluster=cluster,
        exclude_references=exclude_references,
    )
    where_sql = f"WHERE {where}"

    with store._conn() as conn:
        # in_kb_only：用临时表 JOIN 过滤（kb 集合可能上千，避开 IN 参数上限）。
        if in_kb_only:
            conn.execute("DROP TABLE IF EXISTS temp.kb")
            conn.execute("CREATE TEMP TABLE kb (doi TEXT PRIMARY KEY)")
            conn.executemany("INSERT OR IGNORE INTO kb(doi) VALUES (?)",
                             [(d,) for d in kb_dois])
            where_sql += " AND doi IN (SELECT doi FROM temp.kb)"

        # 命中过滤的总节点数（截断前）。
        matched = conn.execute(
            f"SELECT COUNT(*) FROM papers {where_sql}", params
        ).fetchone()[0]

        if preview:
            if in_kb_only:
                conn.execute("DROP TABLE IF EXISTS temp.kb")
            truncated = matched > limit
            return {
                "nodes": [], "edges": [],
                "meta": {"matched_nodes": matched, "returned_nodes": 0,
                         "returned_edges": 0, "truncated": truncated},
            }

        # 取 Top-N 节点（按 sort_col 降序；year 文本排序对 4 位数等价数值序）。
        rows = conn.execute(
            f"SELECT {_NODE_COLUMNS} FROM papers {where_sql} "
            f"ORDER BY {sort_col} DESC LIMIT ?",
            params + [limit],
        ).fetchall()

        nodes = []
        for r in rows:
            d = dict(r)
            doi = d["doi"]
            nodes.append({
                "id": doi,
                "title": d.get("title") or "",
                "journal": d.get("journal") or "",
                "year": _safe_int(d.get("year")),
                "times_cited": d.get("times_cited") or 0,
                "library_citations": d.get("library_citations") or 0,
                "impact_factor": round(d.get("impact_factor") or 0.0, 3),
                "quartile": d.get("quartile") or "",
                "paper_rank": d.get("paper_rank") or 0.0,
                "cluster": d.get("cocitation_cluster") or 0,
                "is_reference": bool(d.get("is_reference")),
                "in_kb": doi in kb_dois,
            })

        selected = [n["id"] for n in nodes]
        edges: list[dict] = []
        if selected:
            _create_selected_temp(conn, selected)
            edge_rows = conn.execute("""
                SELECT c.citing_doi AS source, c.cited_doi AS target
                FROM citations c
                JOIN temp.selected s1 ON c.citing_doi = s1.doi
                JOIN temp.selected s2 ON c.cited_doi = s2.doi
            """).fetchall()
            edges = [{"source": e["source"], "target": e["target"]}
                     for e in edge_rows]
            conn.execute("DROP TABLE IF EXISTS temp.selected")

        # 计算筛选后被引（filtered_citations = 筛选集内入度）
        if citation_source == "filtered" and edges:
            filtered_cit: dict[str, int] = {}
            for e in edges:
                filtered_cit[e["target"]] = filtered_cit.get(e["target"], 0) + 1
            for n in nodes:
                n["filtered_citations"] = filtered_cit.get(n["id"], 0)
        elif citation_source == "filtered":
            # 无边时所有节点 filtered_citations = 0
            for n in nodes:
                n["filtered_citations"] = 0

        # 排除孤立节点（度数=0，即无入边也无出边）
        if exclude_isolated and edges:
            connected = set()
            for e in edges:
                connected.add(e["source"])
                connected.add(e["target"])
            nodes = [n for n in nodes if n["id"] in connected]
        elif exclude_isolated and not edges:
            # 无边时所有节点都是孤立的
            nodes = []

        if in_kb_only:
            conn.execute("DROP TABLE IF EXISTS temp.kb")

    meta = {
        "matched_nodes": matched,
        "returned_nodes": len(nodes),
        "returned_edges": len(edges),
        "truncated": matched > len(nodes),
        "limit": limit,
        "sort_by": sort_col,
        "citation_source": citation_source,
        "exclude_isolated": exclude_isolated,
    }
    logger.info("build_network: %d/%d nodes, %d edges (sort=%s)",
                len(nodes), matched, len(edges), sort_col)
    return {"nodes": nodes, "edges": edges, "meta": meta}


def get_filter_facets(store: LitStore, *,
                      kb_dois: set[str] | None = None) -> dict:
    """过滤器面板的分面信息（取值范围 / 计数 / 聚类列表）。"""
    kb_dois = kb_dois or set()
    with store._conn() as conn:
        def _one(sql, args=()):
            row = conn.execute(sql, args).fetchone()
            return dict(row) if row else {}

        total = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]

        year = _one("""
            SELECT MIN(CAST(year AS INTEGER)) AS mn,
                   MAX(CAST(year AS INTEGER)) AS mx
            FROM papers WHERE year GLOB '[0-9][0-9][0-9][0-9]'
        """)
        libcit = _one("""
            SELECT MIN(library_citations) AS mn, MAX(library_citations) AS mx,
                   AVG(library_citations) AS avg
            FROM papers
        """)
        timescit = _one("""
            SELECT MIN(times_cited) AS mn, MAX(times_cited) AS mx,
                   AVG(times_cited) AS avg
            FROM papers
        """)
        imp = _one("""
            SELECT MIN(impact_factor) AS mn, MAX(impact_factor) AS mx,
                   AVG(impact_factor) AS avg
            FROM papers WHERE impact_factor > 0
        """)
        quartile_rows = conn.execute("""
            SELECT quartile, COUNT(*) AS n FROM papers
            WHERE quartile != '' GROUP BY quartile ORDER BY quartile
        """).fetchall()
        cluster_rows = conn.execute("""
            SELECT cocitation_cluster AS id, COUNT(*) AS size
            FROM papers GROUP BY cocitation_cluster
            ORDER BY size DESC LIMIT 200
        """).fetchall()

    # in_kb 计数：库内 DOI 与 papers 的交集大小。
    in_kb_count = 0
    if kb_dois:
        with store._conn() as conn:
            conn.execute("DROP TABLE IF EXISTS temp.kbf")
            conn.execute("CREATE TEMP TABLE kbf (doi TEXT PRIMARY KEY)")
            conn.executemany("INSERT OR IGNORE INTO kbf(doi) VALUES (?)",
                             [(d,) for d in kb_dois])
            in_kb_count = conn.execute(
                "SELECT COUNT(*) FROM papers WHERE doi IN (SELECT doi FROM temp.kbf)"
            ).fetchone()[0]
            conn.execute("DROP TABLE IF EXISTS temp.kbf")

    return {
        "total_papers": total,
        "year": {"min": _safe_int(year.get("mn")), "max": _safe_int(year.get("mx"))},
        "library_citations": {
            "min": int(libcit.get("mn") or 0),
            "max": int(libcit.get("mx") or 0),
            "avg": round(float(libcit.get("avg") or 0.0), 2),
        },
        "times_cited": {
            "min": int(timescit.get("mn") or 0),
            "max": int(timescit.get("mx") or 0),
            "avg": round(float(timescit.get("avg") or 0.0), 2),
        },
        "impact_factor": {
            "min": round(float(imp.get("mn") or 0.0), 3),
            "max": round(float(imp.get("mx") or 0.0), 3),
            "avg": round(float(imp.get("avg") or 0.0), 3),
        },
        "quartiles": {r["quartile"]: r["n"] for r in quartile_rows},
        "in_kb_count": in_kb_count,
        "clusters": [{"id": r["id"], "size": r["size"]} for r in cluster_rows],
    }


def get_neighbors(store: LitStore, doi: str) -> dict:
    """长按高亮用：返回引用该文献的（citing）与该文献引用的（cited）DOI。"""
    with store._conn() as conn:
        cited_by = [r["citing_doi"] for r in conn.execute(
            "SELECT citing_doi FROM citations WHERE cited_doi=?", (doi,)
        ).fetchall()]
        references = [r["cited_doi"] for r in conn.execute(
            "SELECT cited_doi FROM citations WHERE citing_doi=?", (doi,)
        ).fetchall()]
    return {"doi": doi, "citing": cited_by, "cited": references}


def get_node_detail(store: LitStore, doi: str) -> dict | None:
    """节点详情（标题/摘要/关键词/作者/期刊/年份/IF/分区/被引 + 度数）。"""
    paper = store.get_paper(doi)
    if paper is None:
        return None
    with store._conn() as conn:
        in_deg = conn.execute(
            "SELECT COUNT(*) FROM citations WHERE cited_doi=?", (doi,)
        ).fetchone()[0]
        out_deg = conn.execute(
            "SELECT COUNT(*) FROM citations WHERE citing_doi=?", (doi,)
        ).fetchone()[0]
    return {
        "doi": paper.doi,
        "title": paper.title,
        "abstract": paper.abstract,
        "authors": paper.authors,
        "affiliations": paper.affiliations,
        "corresponding": paper.corresponding,
        "journal": paper.journal,
        "year": _safe_int(paper.year),
        "keywords": paper.keywords,
        "research_areas": paper.research_areas,
        "wos_categories": paper.wos_categories,
        "times_cited": paper.times_cited,
        "library_citations": paper.library_citations,
        "impact_factor": round(paper.impact_factor or 0.0, 3),
        "quartile": paper.quartile,
        "paper_rank": paper.paper_rank,
        "cluster": paper.cocitation_cluster,
        "is_reference": paper.is_reference,
        "in_degree": in_deg,
        "out_degree": out_deg,
    }


def _safe_int(v) -> int | None:
    """把可能是 TEXT/空 的值转 int；非数字返回 None。"""
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
