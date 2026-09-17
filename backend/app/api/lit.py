# -*- coding: utf-8 -*-
"""paperlit API 路由（/api/lit/v1/）：文献 AI 检索全功能暴露。

设计约束：
- 返回稳定 DTO dict，不暴露 paperlit 内部 Pydantic 模型
- 错误码：FileNotFoundError→404, ValueError→400, RuntimeError→503
- 全部同步（paperlit 内部已做批量/限流）
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from ..services import container

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/lit/v1", tags=["lit"])


# ---------------------------------------------------------------- 请求 DTO

class BibPathRequest(BaseModel):
    path: str = Field(..., description="bib 文件/目录路径")
    import_refs: bool = Field(True, description="是否导入参考文献（默认导入）")
    min_year: int | None = Field(None, description="最小年份（过滤早于此年份的文献）")
    max_year: int | None = Field(None, description="最大年份（过滤晚于此年份的文献）")


class DoiRequest(BaseModel):
    doi: str = Field(..., description="DOI")


class DoisRequest(BaseModel):
    dois: list[str] = Field(..., description="DOI 列表")


class GraphRankRequest(BaseModel):
    damping: float = Field(0.85, ge=0.1, le=1.0, description="PageRank 阻尼系数")


class ClusterRequest(BaseModel):
    min_cocitations: int = Field(2, ge=1, description="最小共被引次数")


class VectorBuildRequest(BaseModel):
    batch_size: int = Field(32, ge=1, le=256, description="批量编码大小")


class CleaningRulesRequest(BaseModel):
    year: dict | None = Field(None, description="年份规则，如 {'min': 2015}")
    impact_factor: dict | None = Field(None, description="影响因子规则，如 {'min': 3.0}")
    quartile: dict | None = Field(None, description="分区规则，如 {'keep': ['Q1', 'Q2']}")
    library_citations: dict | None = Field(None, description="库内引用规则，如 {'min': 2}")
    mode: str = Field("delete", description="清洗模式：delete 或 mark")


class TopicCompileRequest(BaseModel):
    field: str = Field("wos_categories",
                       description="聚合维度：wos_categories / research_areas / keywords")
    min_papers: int = Field(2, ge=1, description="主题最少论文数")


# ---------------------------------------------------------------- 错误处理辅助

def _handle_error(e: Exception, context: str = ""):
    """统一错误映射。"""
    prefix = f"{context}: " if context else ""
    if isinstance(e, FileNotFoundError):
        raise HTTPException(404, f"{prefix}文件不存在: {e}")
    if isinstance(e, ValueError):
        raise HTTPException(400, f"{prefix}{e}")
    if isinstance(e, RuntimeError):
        raise HTTPException(503, f"{prefix}{e}")
    raise HTTPException(400, f"{prefix}{e}")


# ================================================================ 状态总览

@router.get("/status")
def lit_status() -> dict:
    """文献检索库总览（paper_count / citation_count / unenriched_count / vector_index_size）。"""
    try:
        return container.get_lit().status()
    except Exception as e:
        _handle_error(e, "status")


# ================================================================ 导入

@router.post("/ingest/bib")
def ingest_bib(req: BibPathRequest) -> dict:
    """导入单个 bib 文件。"""
    try:
        return container.get_lit().ingest_bib(
            req.path,
            import_refs=req.import_refs,
            min_year=req.min_year,
            max_year=req.max_year,
        )
    except Exception as e:
        _handle_error(e, "ingest/bib")


@router.post("/ingest/bib-dir")
def ingest_bib_dir(req: BibPathRequest) -> dict:
    """批量导入目录下所有 .bib 文件。"""
    try:
        return container.get_lit().ingest_bib_dir(
            req.path,
            import_refs=req.import_refs,
            min_year=req.min_year,
            max_year=req.max_year,
        )
    except Exception as e:
        _handle_error(e, "ingest/bib-dir")


@router.post("/ingest/upload")
async def ingest_upload(file: UploadFile = File(...)) -> dict:
    """上传 bib 文件导入。"""
    if not (file.filename or "").lower().endswith(".bib"):
        raise HTTPException(400, "仅支持 .bib 文件")
    data = await file.read()
    if not data:
        raise HTTPException(400, "空文件")
    try:
        return container.get_lit().ingest_upload(file.filename or "upload.bib", data)
    except Exception as e:
        _handle_error(e, "ingest/upload")


# ================================================================ 文献浏览

@router.get("/papers")
def list_papers(offset: int = Query(0, ge=0),
                limit: int = Query(20, ge=1, le=200),
                is_reference: bool | None = None) -> dict:
    """分页列出文献。"""
    try:
        return container.get_lit().list_papers(offset=offset, limit=limit,
                                               is_reference=is_reference)
    except Exception as e:
        _handle_error(e, "papers")


@router.get("/papers/count")
def paper_count() -> dict:
    return {"count": container.get_lit().paper_count()}


@router.get("/paper")
def get_paper(doi: str = Query(..., description="DOI")) -> dict:
    paper = container.get_lit().get_paper(doi)
    if paper is None:
        raise HTTPException(404, f"未找到文献: {doi}")
    return paper


@router.get("/citations/count")
def citation_count() -> dict:
    return {"count": container.get_lit().citation_count()}


# ================================================================ 关键词检索（FTS5）

@router.get("/search/keyword")
def search_keyword(q: str = Query(..., min_length=1),
                   limit: int = Query(20, ge=1, le=200)) -> list[dict]:
    try:
        return container.get_lit().search_keyword(q, limit=limit)
    except Exception as e:
        _handle_error(e, "search/keyword")


# ================================================================ 元数据补全

@router.get("/enrich/unenriched-count")
def unenriched_count() -> dict:
    return {"count": container.get_lit().unenriched_count()}


@router.post("/enrich/one")
def enrich_one(req: DoiRequest) -> dict:
    try:
        return container.get_lit().enrich_one(req.doi)
    except Exception as e:
        _handle_error(e, "enrich/one")


@router.post("/enrich/batch")
def enrich_batch(req: DoisRequest) -> dict:
    try:
        return container.get_lit().enrich_batch(req.dois)
    except Exception as e:
        _handle_error(e, "enrich/batch")


@router.post("/enrich/pending")
def enrich_pending() -> dict:
    try:
        return container.get_lit().enrich_pending()
    except Exception as e:
        _handle_error(e, "enrich/pending")


# ================================================================ 期刊名规范化

@router.get("/journals/unique")
def journals_unique() -> list[dict]:
    try:
        return container.get_lit().get_unique_journals()
    except Exception as e:
        _handle_error(e, "journals/unique")


@router.post("/journals/normalize")
def journals_normalize() -> dict:
    try:
        return container.get_lit().normalize_journals()
    except Exception as e:
        _handle_error(e, "journals/normalize")


@router.get("/journals/mapper-stats")
def journals_mapper_stats() -> dict:
    """获取期刊映射表统计。"""
    try:
        return container.get_lit().get_journal_mapper_stats()
    except Exception as e:
        _handle_error(e, "journals/mapper-stats")


class JournalMappingRequest(BaseModel):
    abbreviation: str = Field(..., description="期刊缩写")
    full_name: str = Field(..., description="期刊全称")
    source: str = Field("manual", description="来源")


@router.post("/journals/mapping")
def journals_add_mapping(req: JournalMappingRequest) -> dict:
    """添加期刊映射。"""
    try:
        success = container.get_lit().add_journal_mapping(
            req.abbreviation, req.full_name, req.source)
        return {"success": success}
    except Exception as e:
        _handle_error(e, "journals/mapping")


# ================================================================ 文献清洗

@router.post("/cleaning/attach-metrics")
def cleaning_attach_metrics() -> dict:
    """关联期刊指标（影响因子、分区）。"""
    try:
        return container.get_lit().attach_journal_metrics()
    except Exception as e:
        _handle_error(e, "cleaning/attach-metrics")


@router.post("/cleaning/compute-citations")
def cleaning_compute_citations() -> dict:
    """计算库内引用次数。"""
    try:
        return container.get_lit().compute_library_citations()
    except Exception as e:
        _handle_error(e, "cleaning/compute-citations")


@router.post("/cleaning/preview")
def cleaning_preview(req: CleaningRulesRequest) -> dict:
    """预览清洗结果（不实际删除）。"""
    try:
        rules = {k: v for k, v in req.dict().items() if v is not None and k != "mode"}
        return container.get_lit().preview_cleaning(rules)
    except Exception as e:
        _handle_error(e, "cleaning/preview")


@router.post("/cleaning/execute")
def cleaning_execute(req: CleaningRulesRequest) -> dict:
    """执行清洗（删除或标记文献）。"""
    try:
        rules = {k: v for k, v in req.dict().items() if v is not None and k != "mode"}
        return container.get_lit().execute_cleaning(rules, mode=req.mode)
    except Exception as e:
        _handle_error(e, "cleaning/execute")


# ================================================================ 引用图谱

@router.post("/graph/rank")
def graph_rank(req: GraphRankRequest) -> dict:
    try:
        return container.get_lit().compute_paper_rank(damping=req.damping)
    except Exception as e:
        _handle_error(e, "graph/rank")


@router.get("/graph/top")
def graph_top(limit: int = Query(20, ge=1, le=200)) -> list[dict]:
    try:
        return container.get_lit().top_papers(limit=limit)
    except Exception as e:
        _handle_error(e, "graph/top")


@router.post("/graph/clusters")
def graph_clusters(req: ClusterRequest) -> dict:
    try:
        return container.get_lit().compute_clusters(min_cocitations=req.min_cocitations)
    except Exception as e:
        _handle_error(e, "graph/clusters")


@router.get("/graph/cluster")
def graph_cluster(cluster_id: int = Query(..., ge=0)) -> list[dict]:
    try:
        return container.get_lit().cluster_papers(cluster_id)
    except Exception as e:
        _handle_error(e, "graph/cluster")


# ============================================================ 文献计量图谱（引用网络可视化）

_KB_DOIS_TTL = 30.0  # 秒：知识库 DOI 集合缓存（图谱标记 in_kb 用，避免每次扫盘）
_kb_dois_cache: tuple[float, set[str]] | None = None


def _get_kb_dois() -> set[str]:
    """知识库 DOI 集合（短 TTL 缓存）。paperkb 不可用时返回空集（in_kb 全 False）。"""
    global _kb_dois_cache
    now = time.monotonic()
    if _kb_dois_cache and now - _kb_dois_cache[0] < _KB_DOIS_TTL:
        return _kb_dois_cache[1]
    try:
        dois = set(container.get_kbapi().kb_dois())
    except Exception as e:  # noqa: BLE001 - 图谱不应因 kb 不可用而失败
        logger.warning("获取知识库 DOI 集合失败（in_kb 标记降级为空）: %s", e)
        dois = set()
    _kb_dois_cache = (now, dois)
    return dois


@router.get("/graph/network")
def graph_network(
    year_min: int | None = Query(None),
    year_max: int | None = Query(None),
    min_library_citations: int | None = Query(None, ge=0),
    min_times_cited: int | None = Query(None, ge=0),
    min_impact_factor: float | None = Query(None, ge=0),
    quartiles: str | None = Query(None, description="逗号分隔，如 Q1,Q2"),
    cluster: int | None = Query(None),
    exclude_references: bool = Query(False),
    in_kb_only: bool = Query(False),
    sort_by: str = Query("library_citations"),
    limit: int = Query(5000, ge=1, le=100000),
) -> dict:
    """引用网络（节点 + 边），服务端过滤。节点上限 limit（按 sort_by 取 Top-N）。"""
    try:
        qlist = [q.strip() for q in quartiles.split(",") if q.strip()] \
            if quartiles else None
        return container.get_lit().graph_network(
            year_min=year_min, year_max=year_max,
            min_library_citations=min_library_citations,
            min_times_cited=min_times_cited,
            min_impact_factor=min_impact_factor,
            quartiles=qlist, cluster=cluster,
            exclude_references=exclude_references, in_kb_only=in_kb_only,
            sort_by=sort_by, limit=limit, kb_dois=_get_kb_dois(),
        )
    except Exception as e:
        _handle_error(e, "graph/network")


@router.get("/graph/filters")
def graph_filters() -> dict:
    """过滤器面板分面（取值范围 / 计数 / 聚类列表）。"""
    try:
        return container.get_lit().graph_filter_facets(kb_dois=_get_kb_dois())
    except Exception as e:
        _handle_error(e, "graph/filters")


@router.get("/graph/neighbors")
def graph_neighbors(doi: str = Query(..., description="中心节点 DOI")) -> dict:
    """长按高亮用：引用该文献的（citing）与该文献引用的（cited）DOI。"""
    try:
        return container.get_lit().graph_neighbors(doi)
    except Exception as e:
        _handle_error(e, "graph/neighbors")


@router.get("/graph/node")
def graph_node(doi: str = Query(..., description="节点 DOI")) -> dict:
    """节点详情（标题/摘要/关键词/作者/期刊/年份/IF/分区/被引 + 度数）。"""
    detail = container.get_lit().graph_node_detail(doi)
    if detail is None:
        raise HTTPException(404, f"未找到节点: {doi}")
    detail["in_kb"] = doi in _get_kb_dois()
    return detail


# ================================================================ 向量索引

@router.post("/vector/build")
def vector_build(req: VectorBuildRequest) -> dict:
    try:
        return container.get_lit().build_vector_index(batch_size=req.batch_size)
    except Exception as e:
        _handle_error(e, "vector/build")


@router.get("/vector/search")
def vector_search(q: str = Query(..., min_length=1),
                  top_k: int = Query(20, ge=1, le=200)) -> list[dict]:
    try:
        return container.get_lit().vector_search(q, top_k=top_k)
    except Exception as e:
        _handle_error(e, "vector/search")


@router.get("/vector/size")
def vector_size() -> dict:
    return {"size": container.get_lit().vector_index_size()}


# ================================================================ 三层漏斗检索

@router.get("/search")
def search(q: str = Query(..., min_length=1)) -> list[dict]:
    """三层漏斗检索（L1 粗筛 → L2 rerank → L3 交付）。"""
    try:
        return container.get_lit().search(q)
    except Exception as e:
        _handle_error(e, "search")


@router.get("/search/cached")
def search_cached(q: str = Query(..., min_length=1)) -> list[dict]:
    """带缓存的检索（命中缓存直接返回）。"""
    try:
        return container.get_lit().cached_search(q)
    except Exception as e:
        _handle_error(e, "search/cached")


# ================================================================ 查询缓存

@router.get("/cache/stats")
def cache_stats() -> dict:
    try:
        return container.get_lit().cache_stats()
    except Exception as e:
        _handle_error(e, "cache/stats")


@router.post("/cache/clear")
def cache_clear() -> dict:
    try:
        cleared = container.get_lit().cache_clear()
        return {"cleared": cleared}
    except Exception as e:
        _handle_error(e, "cache/clear")


# ================================================================ 主题编译

@router.post("/topics/compile")
def topics_compile(req: TopicCompileRequest) -> dict:
    """编译主题（按指定维度聚合）。"""
    try:
        return container.get_lit().compile_topics(field=req.field,
                                                  min_papers=req.min_papers)
    except Exception as e:
        _handle_error(e, "topics/compile")


@router.get("/topics")
def topics_list(limit: int = Query(50, ge=1, le=500),
                offset: int = Query(0, ge=0)) -> list[dict]:
    """列出所有主题（按论文数降序）。"""
    try:
        return container.get_lit().list_topics(limit=limit, offset=offset)
    except Exception as e:
        _handle_error(e, "topics")


@router.get("/topics/topic")
def topic_detail(slug: str = Query(..., description="主题 slug")) -> dict:
    topic = container.get_lit().get_topic(slug)
    if topic is None:
        raise HTTPException(404, f"未找到主题: {slug}")
    return topic


# ================================================================ 一键管线

@router.post("/pipeline")
def run_pipeline() -> dict:
    """一键全流程：元数据补全 → 期刊名规范化 → PaperRank → 向量索引。

    主题编译（compile_topics）耗 token，须用户在「主题」页手动触发，不纳入自动流程。
    """
    lit = container.get_lit()
    steps: list[dict] = []

    def _run(name: str, fn):
        t0 = time.monotonic()
        try:
            result = fn()
            elapsed = round(time.monotonic() - t0, 2)
            steps.append({"step": name, "status": "ok",
                          "result": result, "seconds": elapsed})
        except Exception as e:
            elapsed = round(time.monotonic() - t0, 2)
            steps.append({"step": name, "status": "error",
                          "error": str(e), "seconds": elapsed})

    _run("enrich_pending", lit.enrich_pending)
    _run("normalize_journals", lit.normalize_journals)
    _run("compute_paper_rank", lit.compute_paper_rank)
    _run("build_vector_index", lit.build_vector_index)

    return {"steps": steps, "final_status": lit.status()}


# ================================================================ 配置管理

@router.get("/config")
def get_config() -> dict:
    """获取 AI检索 配置（API key 脱敏）。"""
    try:
        settings_svc = container.get_settings_service()
        return settings_svc.get_lit_config()
    except Exception as e:
        _handle_error(e, "config")


class LitConfigRequest(BaseModel):
    embedding_api_key: str | None = None
    reranker_api_key: str | None = None
    embedding_model: str | None = None
    reranker_model: str | None = None


@router.post("/config")
def save_config(req: LitConfigRequest) -> dict:
    """保存 AI检索 配置到 .env。"""
    try:
        settings_svc = container.get_settings_service()
        readback = settings_svc.save_lit_config(
            embedding_api_key=req.embedding_api_key,
            reranker_api_key=req.reranker_api_key,
            embedding_model=req.embedding_model,
            reranker_model=req.reranker_model,
        )
        cfg = settings_svc.get_lit_config()
        return {"status": "ok", "config": cfg, "env_ok": readback.get("ok", True),
                "env_mismatch": readback.get("mismatch", [])}
    except Exception as e:
        _handle_error(e, "config")
