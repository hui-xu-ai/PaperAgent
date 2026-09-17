# -*- coding: utf-8 -*-
"""文献检索工具集（AI 检索会话的 function calling 工具）。

形态：openai tools 格式的 TOOL_SPECS + run_tool 执行器。全部经 LitService
（paperlit 门面），结果**截断回填**（防上下文膨胀）。

约定：
- 只读工具（查询/列表/检索）可自由调用；写工具（导入/补全/计算/构建）
  描述带【写】+ system prompt 约束须用户明确意图。
- 描述精简（省 token 注入）：一句话做什么 + 关键参数；写操作标【写】。
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

_TRIM = 800


def _trim(v, limit: int = _TRIM) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return s if len(s) <= limit else s[:limit] + f"…[截断 {len(s) - limit} 字符]"


def _get_lit():
    from .container import get_lit
    return get_lit()


# ---------------------------------------------------------------- 工具定义
TOOL_SPECS: list[dict] = [
    {"type": "function", "function": {
        "name": "lit_status",
        "description": "查看文献库状态（文献总数/引用边数/待补全数/向量索引大小）。",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "lit_search",
        "description": "三层漏斗检索（关键词 + 向量召回 → reranker 精排）：输入检索词，返回按综合分排序的文献列表。",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string", "description": "检索词"}},
                       "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "lit_list_papers",
        "description": "分页列出库内文献（可按是否参考文献过滤）。",
        "parameters": {"type": "object",
                       "properties": {"offset": {"type": "integer", "description": "偏移，默认 0"},
                                      "limit": {"type": "integer", "description": "条数，默认 20"},
                                      "is_reference": {"type": "boolean", "description": "true=仅参考文献, false=仅主文献, null=全部"}},
                       "required": []}}},
    {"type": "function", "function": {
        "name": "lit_get_paper",
        "description": "按 DOI 查询单篇文献详情。",
        "parameters": {"type": "object",
                       "properties": {"doi": {"type": "string", "description": "文献 DOI"}},
                       "required": ["doi"]}}},
    {"type": "function", "function": {
        "name": "lit_enrich_one",
        "description": "【写】补全单篇文献的元数据（OpenAlex + CrossRef）。",
        "parameters": {"type": "object",
                       "properties": {"doi": {"type": "string", "description": "文献 DOI"}},
                       "required": ["doi"]}}},
    {"type": "function", "function": {
        "name": "lit_enrich_pending",
        "description": "【写】批量补全所有待处理文献的元数据（OpenAlex 批量 + CrossRef 兜底）。",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "lit_compute_rank",
        "description": "【写】计算所有文献的 PaperRank（类 PageRank 引用图谱排名）。",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "lit_top_papers",
        "description": "获取 PaperRank 最高的文献列表。",
        "parameters": {"type": "object",
                       "properties": {"limit": {"type": "integer", "description": "条数，默认 20"}},
                       "required": []}}},
    {"type": "function", "function": {
        "name": "lit_compute_clusters",
        "description": "【写】计算共被引聚类（基于引用关系）。",
        "parameters": {"type": "object",
                       "properties": {"min_cocitations": {"type": "integer", "description": "最小共被引次数，默认 2"}},
                       "required": []}}},
    {"type": "function", "function": {
        "name": "lit_cluster_papers",
        "description": "获取指定聚类的所有文献。",
        "parameters": {"type": "object",
                       "properties": {"cluster_id": {"type": "integer", "description": "聚类 ID"}},
                       "required": ["cluster_id"]}}},
    {"type": "function", "function": {
        "name": "lit_vector_search",
        "description": "向量语义检索（需已构建向量索引）：输入自然语言查询，返回按相似度排序的文献。",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string", "description": "查询文本"},
                                      "top_k": {"type": "integer", "description": "返回条数，默认 20"}},
                       "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "lit_build_vector",
        "description": "【写】构建向量索引（为所有有 abstract 的文献生成 embedding，需 API key）。",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "lit_ingest_bib",
        "description": "【写】导入 bib 文件到文献库（解析 + 去重 + 入库）。",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string", "description": "bib 文件路径"}},
                       "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "lit_ingest_bib_dir",
        "description": "【写】批量导入目录下所有 .bib 文件。",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string", "description": "包含 .bib 文件的目录路径"}},
                       "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "lit_compile_topics",
        "description": "【写】编译主题（按 WoS 类别/关键词聚合文献）。",
        "parameters": {"type": "object",
                       "properties": {"field": {"type": "string", "description": "聚合字段：wos_categories/research_areas/keywords，默认 wos_categories"},
                                      "min_papers": {"type": "integer", "description": "最少文献数，默认 2"}},
                       "required": []}}},
    {"type": "function", "function": {
        "name": "lit_list_topics",
        "description": "列出所有主题（按论文数降序）。",
        "parameters": {"type": "object",
                       "properties": {"limit": {"type": "integer", "description": "条数，默认 50"}},
                       "required": []}}},
    {"type": "function", "function": {
        "name": "lit_get_topic",
        "description": "获取单个主题详情（含该主题下的文献列表）。",
        "parameters": {"type": "object",
                       "properties": {"slug": {"type": "string", "description": "主题 slug"}},
                       "required": ["slug"]}}},
    {"type": "function", "function": {
        "name": "lit_cache_stats",
        "description": "查看查询缓存统计（条目数/命中/未命中）。",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "lit_cache_clear",
        "description": "【写】清空查询缓存。",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
]


def run_tool(name: str, args: dict) -> dict:
    """执行工具（经容器注入的 LitService 访问器）。"""
    lit = _get_lit()
    try:
        if name == "lit_status":
            return {"ok": True, "result": _trim(lit.status())}
        if name == "lit_search":
            return {"ok": True, "result": _trim(lit.search(args.get("query", "")))}
        if name == "lit_list_papers":
            offset = int(args.get("offset", 0))
            limit = int(args.get("limit", 20))
            is_ref = args.get("is_reference")
            return {"ok": True, "result": _trim(lit.list_papers(offset=offset, limit=limit,
                                                                is_reference=is_ref))}
        if name == "lit_get_paper":
            paper = lit.get_paper(args.get("doi", ""))
            if paper is None:
                return {"ok": False, "result": f"未找到文献: {args.get('doi', '')}"}
            return {"ok": True, "result": _trim(paper)}
        if name == "lit_enrich_one":
            return {"ok": True, "result": _trim(lit.enrich_one(args.get("doi", "")))}
        if name == "lit_enrich_pending":
            return {"ok": True, "result": _trim(lit.enrich_pending())}
        if name == "lit_compute_rank":
            return {"ok": True, "result": _trim(lit.compute_paper_rank())}
        if name == "lit_top_papers":
            return {"ok": True, "result": _trim(lit.top_papers(limit=int(args.get("limit", 20))))}
        if name == "lit_compute_clusters":
            return {"ok": True, "result": _trim(lit.compute_clusters(
                min_cocitations=int(args.get("min_cocitations", 2))))}
        if name == "lit_cluster_papers":
            return {"ok": True, "result": _trim(lit.cluster_papers(int(args.get("cluster_id", 0))))}
        if name == "lit_vector_search":
            return {"ok": True, "result": _trim(lit.vector_search(
                args.get("query", ""), top_k=int(args.get("top_k", 20))))}
        if name == "lit_build_vector":
            return {"ok": True, "result": _trim(lit.build_vector_index())}
        if name == "lit_ingest_bib":
            return {"ok": True, "result": _trim(lit.ingest_bib(args.get("path", "")))}
        if name == "lit_ingest_bib_dir":
            return {"ok": True, "result": _trim(lit.ingest_bib_dir(args.get("path", "")))}
        if name == "lit_compile_topics":
            return {"ok": True, "result": _trim(lit.compile_topics(
                field=args.get("field", "wos_categories"),
                min_papers=int(args.get("min_papers", 2))))}
        if name == "lit_list_topics":
            return {"ok": True, "result": _trim(lit.list_topics(limit=int(args.get("limit", 50))))}
        if name == "lit_get_topic":
            topic = lit.get_topic(args.get("slug", ""))
            if topic is None:
                return {"ok": False, "result": f"未找到主题: {args.get('slug', '')}"}
            return {"ok": True, "result": _trim(topic)}
        if name == "lit_cache_stats":
            return {"ok": True, "result": _trim(lit.cache_stats())}
        if name == "lit_cache_clear":
            return {"ok": True, "result": _trim(lit.cache_clear())}
        return {"ok": False, "result": f"未知工具: {name}"}
    except Exception as e:  # noqa: BLE001
        logger.exception("工具 %s 执行失败", name)
        return {"ok": False, "result": f"{type(e).__name__}: {e}"}


def tool_specs() -> list[dict]:
    return TOOL_SPECS
