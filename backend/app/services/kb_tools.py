# -*- coding: utf-8 -*-
"""知识库管理工具集（主 agent「知识库·管理模式」的 function calling 工具）。

形态：openai tools 格式的 TOOL_SPECS + run_tool 执行器。全部经 kbmeta_service
（paperkb 门面），结果**截断回填**（防上下文膨胀）。

约定：
- 只读工具（查询/列表/校验/预览）可自由调用；写工具（编译/纠正/导入/同步/翻译）
  描述带【写】+ system prompt 约束须用户明确意图。
- 描述精简（省 token 注入）：一句话做什么 + 关键参数；写操作标【写】。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# 工具结果回填上限（字符）；kb_recall 检索片段放宽
_TRIM = 800
_RECALL_TRIM = 4000
# kb_paper_products 回填上限：它是**显式取整份**的出口，各份已由 max_chars(≤8000) 限住，
# 这里只是总量兜底（约 2 份 ×8000）；不要退回 _TRIM，否则等于没取整份。
_PRODUCTS_TRIM = 16000

# 报告/综述落盘目录名（知识库 root 下）
_REPORTS_DIRNAME = "_reports"
# 文件名非法字符（Windows + 路径分隔 + 控制符）
_INVALID_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _trim(v, limit: int = _TRIM) -> str:
    """截断回填（防上下文膨胀）。

    **列表结果按整条取舍**（预算内保留前 N 条**完整**条目）——旧实现对序列化后的 JSON
    字符串做边界裁剪，会把末条切成半截、JSON 结构也坏掉：模型看到"数值被截断"却不知道
    缺了什么（2026-09-21 实测模型回报"数值被截断"）。宁可少给一条完整的，也不给半条。
    字符串输入仍走边界裁剪（段落/行/句，不切进句子或 `$…$`）。
    """
    if isinstance(v, list):
        return _trim_items(v, limit)
    return _trim_text(v, limit)


def _trim_items(items: list, limit: int) -> str:
    """按条目整条取舍；单条自身就超预算时退回字符串边界裁剪（无法两全）。"""
    kept: list = []
    used = 0
    for it in items:
        size = len(json.dumps(it, ensure_ascii=False)) + 2      # 2 = ", " 分隔
        if kept and used + size > limit:
            break
        kept.append(it)
        used += size
    if used > limit:                    # 首条自身就超预算：退回字符串边界裁剪
        return _trim_text(items, limit)
    out = json.dumps(kept, ensure_ascii=False)
    omitted = len(items) - len(kept)
    if omitted:
        hint = f"…[已省略 {omitted} 条]"
        while len(kept) > 1 and len(out) + len(hint) > limit:
            kept.pop()
            out = json.dumps(kept, ensure_ascii=False)
        out += hint
    return out


def _trim_text(v, limit: int = _TRIM) -> str:
    """截断回填（防上下文膨胀）：优先在段落/行/句边界收尾，不切进句子或公式。

    无边界可用时退回硬切（与旧行为一致，见 tests/test_kb_tools.py::test_trim）。
    """
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    if len(s) <= limit:
        return s
    try:
        from paperkb.textseg import boundary_trim

        kept = boundary_trim(s, limit)
    except Exception:  # noqa: BLE001 - 边界裁剪失败退回硬切
        kept = s[:limit]
    if len(kept) >= len(s):
        kept = s[:limit]
    return kept + f"…[截断 {len(s) - len(kept)} 字符]"


# ---------------------------------------------------------------- 报告写入辅助
def _safe_title(title: str) -> str:
    """清洗为安全的报告文件名（单片段，无路径分隔/穿越）。中文保留。"""
    t = _INVALID_FS.sub("_", (title or "").strip())
    t = t.replace("..", "_")
    t = t.strip(" .")
    t = re.sub(r"\s+", "_", t)
    return t[:80] or "report"


def _safe_rel_path(path: str) -> str | None:
    """规范化 reports 内相对子路径（可含子目录）。

    返回约定：空 path → ''（表示用标题命名）；含 `..` 穿越 → None（拒绝）；
    其余 → 规范化后的 POSIX 相对路径。**先按原始段判 `..`**（避免被 _safe_title 抹掉）。
    """
    if not path or not path.strip():
        return ""
    parts: list[str] = []
    for seg in re.split(r"[\\/]+", path.strip()):
        seg = seg.strip()
        if seg in ("", "."):
            continue
        if seg == "..":
            return None  # 路径穿越，拒绝
        clean = _safe_title(seg)
        if clean and clean not in (".", ".."):
            parts.append(clean)
    return "/".join(parts) if parts else None


def _reports_dir() -> Path:
    """知识库 root/_reports/（综述等成文稿件落盘根）。

    优先经访问器（container 注入的 paperkb 门面）取 kb 根路径，回退默认根；
    测试可 monkeypatch 本函数指向临时目录。
    """
    try:
        from .kbmeta_service import get_kbmeta

        roots = getattr(get_kbmeta(), "roots", None)
        if roots is not None:
            return Path(roots.kb_dir) / _REPORTS_DIRNAME
    except Exception:  # noqa: BLE001 容器未初始化/测试环境
        pass
    from ..config import APP_DATA_DIR

    return Path(APP_DATA_DIR) / "knowledge_base" / _REPORTS_DIRNAME


# ---------------------------------------------------------------- 工具定义
TOOL_SPECS: list[dict] = [
    {"type": "function", "function": {
        "name": "kb_recall",
        "description": "知识库检索：按问题召回编译产物/元数据片段（中英文均可）。问答前先调用，再基于片段回答并标注 [[DOI目录]]。",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string", "description": "检索问题/关键词"},
                                      "top_k": {"type": "integer", "description": "召回条数，默认 6"}},
                       "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "kb_search_papers",
        "description": "按标题/作者/期刊/关键词搜索库内文献元数据（bib 权威），返回匹配列表；可用 kind 只找某类（书/学位论文/无编号），has_attachment 只找带 SI/审稿意见的。",
        "parameters": {"type": "object",
                       "properties": {"q": {"type": "string", "description": "搜索词"},
                                      "limit": {"type": "integer", "description": "上限，默认 20"},
                                      "kind": {"type": "string",
                                               "description": "类型过滤：paper/thesis/book/chapter/patent/standard/report/none(无编号)；空=全部"},
                                      "has_attachment": {"type": "boolean",
                                                         "description": "只返回有附件（支撑信息/审稿意见/数据）的资源"}},
                       "required": ["q"]}}},
    {"type": "function", "function": {
        "name": "kb_attachments",
        "description": "列某资源（DOI 或 RID）的依附资料清单：支撑信息 SI / 审稿意见 / 数据等，含相对路径、大小、是否已入检索索引。要读内容用 kb_attachment_text。",
        "parameters": {"type": "object",
                       "properties": {"rid": {"type": "string",
                                              "description": "资源标识：DOI（含 /）或 RID/目录键（book__…/nd-…）"}},
                       "required": ["rid"]}}},
    {"type": "function", "function": {
        "name": "kb_attachment_text",
        "description": "读某资源附件（SI/审稿意见等）的文本片段。path 取自 kb_attachments/kb_recall。长文用 offset 续读（返回 next_offset，0=末尾）。",
        "parameters": {"type": "object",
                       "properties": {"rid": {"type": "string", "description": "资源标识（DOI 或 RID）"},
                                      "path": {"type": "string",
                                               "description": "附件相对路径，如 si/support.md（取自 kb_attachments/kb_recall）"},
                                      "max_chars": {"type": "integer", "description": "最多返回字符数，默认 8000"},
                                      "offset": {"type": "integer", "description": "起始字符位置（续读用），默认 0"}},
                       "required": ["rid", "path"]}}},
    {"type": "function", "function": {
        "name": "kb_paper_detail",
        "description": "单篇详情：元数据（bib 权威）+ 引用/被引关系 + library/kb 原文层状态。",
        "parameters": {"type": "object",
                       "properties": {"doi": {"type": "string", "description": "DOI（含 /）"}},
                       "required": ["doi"]}}},
    {"type": "function", "function": {
        "name": "kb_paper_products",
        "description": "取单篇**编译产物整份正文**（L1 笔记 / L2 深度解读 / L3 关系卡片），"
                       "按份边界对齐截断并报告截断量。kb_recall 只给命中片段、公式或小节"
                       "可能不完整时用它补全证据。",
        "parameters": {"type": "object",
                       "properties": {
                           "doi": {"type": "string", "description": "DOI（含 /）"},
                           "files": {"type": "string",
                                     "description": "逗号分隔，可选 note/wiki/relations；默认 note,wiki"},
                           "max_chars": {"type": "integer",
                                         "description": "每份上限（默认 3000，最大 8000）"}},
                       "required": ["doi"]}}},
    {"type": "function", "function": {
        "name": "kb_paper_scores",
        "description": "单篇价值评分详情（IF 档/被引/主题/⭐/年份分项）——编译优先级依据。",
        "parameters": {"type": "object",
                       "properties": {"doi": {"type": "string", "description": "DOI"}},
                       "required": ["doi"]}}},
    {"type": "function", "function": {
        "name": "kb_list_scores",
        "description": "全部文献价值评分与编译等级（按分排序），了解库内文献与编译优先级。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "kb_compile_now",
        "description": "【写】立即编译单篇（LLM 调用较慢）：L1 六维知识编译/L2 章节/L3 深度。需用户明确意图。",
        "parameters": {"type": "object",
                       "properties": {"doi": {"type": "string", "description": "DOI"},
                                      "level": {"type": "string", "enum": ["L1", "L2", "L3"], "description": "默认 L1"}},
                       "required": ["doi"]}}},
    {"type": "function", "function": {
        "name": "kb_compile_queue",
        "description": "【写】单篇入编译队列（level 缺省按价值分自动判定）。",
        "parameters": {"type": "object",
                       "properties": {"doi": {"type": "string", "description": "DOI"},
                                      "level": {"type": "string", "enum": ["L1", "L2", "L3"]}},
                       "required": ["doi"]}}},
    {"type": "function", "function": {
        "name": "kb_compile_queue_all",
        "description": "【写】按价值分批量入队全部文献（L1 全做，L2/L3 按分）。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "kb_compile_process",
        "description": "【写】处理编译队列（取最高优先级执行，limit 项）。",
        "parameters": {"type": "object",
                       "properties": {"limit": {"type": "integer", "description": "处理项数，默认 1"}}}}},
    {"type": "function", "function": {
        "name": "kb_compile_jobs",
        "description": "编译队列状态列表（pending/queued/compiling/done/failed）。",
        "parameters": {"type": "object",
                       "properties": {"status": {"type": "string", "description": "按状态过滤，可空"}}}}},
    {"type": "function", "function": {
        "name": "kb_journal_override",
        "description": "【写】期刊名人工纠正：bib 期刊名与 journals.db 标准名不匹配时绑定标准名（影响评分与展示）。",
        "parameters": {"type": "object",
                       "properties": {"doi": {"type": "string", "description": "DOI"},
                                      "journal_name": {"type": "string", "description": "journals.db 标准期刊名"}},
                       "required": ["doi", "journal_name"]}}},
    {"type": "function", "function": {
        "name": "kb_journals_stats",
        "description": "期刊库统计（jcr/cas 行数与年份）。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "kb_missing_dois",
        "description": "扫描缺 bib 元数据的 DOI 并生成 WOS 检索式（复制到 webofscience 检索导出 bib 后走 kb_import_bib）。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "kb_import_bib_preview",
        "description": "预览 bib 文件（记录数/有效 DOI/去重/缺字段，不导入）——导入前确认用。",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string", "description": "bib 文件本机路径"}},
                       "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "kb_import_bib",
        "description": "【写】导入 bib 文件（papers_meta upsert + 引用边重建，幂等；元数据唯一权威=bib）。先 kb_import_bib_preview 确认。",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string", "description": "bib 文件本机路径"}},
                       "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "kb_translate_paper",
        "description": "【写】【耗时长】翻译单篇 document.json（总结+分批译文写回，需数分钟）。DOI 定位 kb/library 内 document.json。",
        "parameters": {"type": "object",
                       "properties": {"doi": {"type": "string", "description": "DOI（含 /）"}},
                       "required": ["doi"]}}},
    {"type": "function", "function": {
        "name": "kb_kb_status",
        "description": "知识库原文层总览：每篇 kb/<DOI>/ 的 source.pdf/en.md/document.json/images 与编译产物存在性。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "kb_source_sync",
        "description": "【写】library/<DOI>/ 原文层四件纳入/重新同步到 knowledge_base（force=False 冻结不覆盖；force=True 手动重新同步）。",
        "parameters": {"type": "object",
                       "properties": {"doi": {"type": "string", "description": "DOI"},
                                      "force": {"type": "boolean", "description": "是否强制覆盖（重新同步）"}},
                       "required": ["doi"]}}},
    {"type": "function", "function": {
        "name": "kb_verify_doc",
        "description": "en.md ↔ document.json 一致性校验（kb 原文层漂移检测）。",
        "parameters": {"type": "object",
                       "properties": {"doi": {"type": "string", "description": "DOI"}},
                       "required": ["doi"]}}},
    {"type": "function", "function": {
        "name": "kb_write_report",
        "description": "【写】把综述/报告/总结内容落盘为知识库 _reports/<标题>.md，返回文件路径。"
                       "引用文献用 [[DOI目录]] 标注（须来自 kb 真实文献）。需用户明确意图。",
        "parameters": {"type": "object",
                       "properties": {"title": {"type": "string", "description": "报告标题（用作文件名，自动清洗非法字符）"},
                                      "content": {"type": "string", "description": "Markdown 全文内容"},
                                      "path": {"type": "string",
                                               "description": "可选：reports 内相对子路径（如 综述/人工肌肉.md），默认 <title>.md"}},
                       "required": ["title", "content"]}}},
]


# ---------------------------------------------------------------- 执行器
def _resolve_doc_json(doi: str, kb=None) -> str:
    """DOI → kb/library 内 document.json 路径（kb 优先，回退 library）。

    roots 来自注入的访问器（container 注入的 paperkb 门面），不 import container。
    """
    from pathlib import Path

    from paperkb.doi import doi_to_dirname
    from paperkb.layout import doc_path

    roots = getattr(kb, "roots", None) if kb is not None else None
    if roots is None:
        return ""
    d = doi_to_dirname(doi)
    for base, is_kb in ((roots.kb_dir, True), (roots.library_dir, False)):
        cand = doc_path(Path(base) / d, kb=is_kb)   # 文件名唯一来源（勿裸写）
        if cand.exists():
            return str(cand)
    return ""


def _papers_enriched(kb, limit: int = 1000) -> list[dict]:
    """kb_list 全量（含 kind/attachments）——供按类型/附件过滤与结果富化。

    kb.search 只给 FTS 命中（无 kind 字段），故这里取一次全量做映射
    （库内通常几十~几百篇；比逐条查库便宜）。
    """
    out = kb.kb_list(page=1, page_size=200, kind="", has_attachment=False)
    items = list(out.get("items") or [])
    # kb_list 单页最多 200；需要更多时按 total 翻页（极少触发）
    total = int(out.get("total") or 0)
    page = 2
    while len(items) < min(total, limit) and page <= 10:
        more = kb.kb_list(page=page, page_size=200).get("items") or []
        if not more:
            break
        items.extend(more)
        page += 1
    return items


def _enrich_search(hits: list[dict], enriched: list[dict]) -> list[dict]:
    """给 FTS 命中补 kind/attachments（A6：回答里能说清"这是书/这篇有 SI"）。"""
    by_key: dict[str, dict] = {}
    for it in enriched:
        for k in (it.get("rid") or "", it.get("doi") or "", it.get("dir") or ""):
            if k:
                by_key.setdefault(k, it)
    out = []
    for h in hits:
        if not isinstance(h, dict):
            out.append(h)
            continue
        key = (h.get("rid") or h.get("doi") or "")
        src = by_key.get(key) or {}
        merged = dict(h)
        merged["kind"] = src.get("kind", "")
        merged["attachments"] = src.get("attachments", 0)
        out.append(merged)
    return out


def run_tool(name: str, args: dict, recall_budget: int | None = None, kb=None) -> dict:
    """执行工具（经容器注入的 paperkb 访问器；未注入时回退 get_kbmeta()）。

    recall_budget：kb_recall 检索结果回填预算（字符）；None 时用默认 _RECALL_TRIM，
    综述/写作类任务经 chat_service 传入更大值（放大召回证据量）。
    kb：可注入的访问器（构造注入/依赖注入）；为 None 时回退模块 accessor
    get_kbmeta()（其指向 container 持有的 paperkb 门面）。
    """
    from ..services.kbmeta_service import get_kbmeta

    if kb is None:
        kb = get_kbmeta()
    try:
        if name == "kb_recall":
            out = kb.recall(args.get("query", ""), top_k=int(args.get("top_k", 6)))
            limit = recall_budget if recall_budget else _RECALL_TRIM
            return {"ok": True, "result": _trim(out, limit)}
        if name == "kb_search_papers":
            q = args.get("q", "")
            limit = int(args.get("limit", 20))
            kind = (args.get("kind") or "").strip().lower()
            has_att = bool(args.get("has_attachment", False))
            if kind or has_att:
                # A2：类型/附件过滤走 kb_list（服务端过滤，含磁盘附件计数）
                out = kb.kb_list(q=q, kind=kind or "all", has_attachment=has_att,
                                 page=1, page_size=min(200, max(1, limit)))
                return {"ok": True, "result": _trim(out.get("items") or [])}
            hits = kb.search(q, limit=limit)
            try:
                hits = _enrich_search(hits, _papers_enriched(kb))
            except Exception:  # noqa: BLE001 - 富化失败不影响搜索本身
                pass
            return {"ok": True, "result": _trim(hits)}
        if name == "kb_attachments":
            return {"ok": True, "result": _trim(kb.kb_attachments(args.get("rid", "")))}
        if name == "kb_attachment_text":
            rid = args.get("rid", "")
            path = args.get("path", "")
            out = kb.kb_attachment_read(rid, path,
                                        max_chars=int(args.get("max_chars", 8000)),
                                        offset=int(args.get("offset", 0) or 0))
            if not out.get("ok"):
                return {"ok": False, "result": out.get("error", "读取失败")}
            return {"ok": True, "result": _trim(out, _RECALL_TRIM)}
        if name == "kb_paper_detail":
            doi = args.get("doi", "")
            meta = kb.get_paper(doi)
            if meta is None:
                return {"ok": False, "result": f"未找到元数据: {doi}"}
            cit = kb.citations_for(doi)
            st = kb.source_status(doi)
            return {"ok": True, "result": _trim({"meta": meta, "citations": cit,
                                                 "source": st})}
        if name == "kb_paper_products":
            doi = (args.get("doi") or "").strip()
            if not doi:
                return {"ok": False, "result": "缺少 doi"}
            out = kb.kb_paper_products(doi, args.get("files") or "note,wiki",
                                       args.get("max_chars") or 3000)
            return {"ok": True, "result": _trim(out, _PRODUCTS_TRIM)}
        if name == "kb_paper_scores":
            doi = args.get("doi", "")
            s = kb.value_score(doi)
            if s is None:
                return {"ok": False, "result": f"未找到元数据: {doi}"}
            return {"ok": True, "result": _trim(s)}
        if name == "kb_list_scores":
            return {"ok": True, "result": _trim(kb.list_scores())}
        if name == "kb_compile_now":
            return {"ok": True, "result": _trim(kb.compile_now(
                args.get("doi", ""), args.get("level", "L1")))}
        if name == "kb_compile_queue":
            return {"ok": True, "result": _trim(kb.compile_queue(
                args.get("doi", ""), args.get("level")))}
        if name == "kb_compile_queue_all":
            return {"ok": True, "result": _trim(kb.compile_queue_all())}
        if name == "kb_compile_process":
            return {"ok": True, "result": _trim(kb.compile_process(
                int(args.get("limit", 1))))}
        if name == "kb_compile_jobs":
            return {"ok": True, "result": _trim(kb.compile_status(args.get("status")))}
        if name == "kb_journal_override":
            return {"ok": True, "result": _trim(kb.set_journal_override(
                args.get("doi", ""), args.get("journal_name", "")))}
        if name == "kb_journals_stats":
            return {"ok": True, "result": _trim(kb.journals_stats())}
        if name == "kb_missing_dois":
            return {"ok": True, "result": _trim(kb.missing_dois())}
        if name == "kb_import_bib_preview":
            return {"ok": True, "result": _trim(kb.bib_preview(args.get("path", "")))}
        if name == "kb_import_bib":
            return {"ok": True, "result": _trim(kb.bib_import(args.get("path", "")))}
        if name == "kb_translate_paper":
            doi = args.get("doi", "")
            doc_json = _resolve_doc_json(doi, kb)
            if not doc_json:
                return {"ok": False, "result": f"未找到 {doi} 的 document.json（kb/library）"}
            out = kb.translate_now(doc_json)
            return {"ok": True, "result": _trim(out)}
        if name == "kb_kb_status":
            return {"ok": True, "result": _trim(kb.kb_status())}
        if name == "kb_source_sync":
            return {"ok": True, "result": _trim(kb.source_sync(
                args.get("doi", ""), bool(args.get("force", False))))}
        if name == "kb_verify_doc":
            return {"ok": True, "result": _trim(kb.verify_doc(args.get("doi", "")))}
        if name == "kb_write_report":
            return _write_report(args)
        return {"ok": False, "result": f"未知工具: {name}"}
    except Exception as e:  # noqa: BLE001
        logger.exception("工具 %s 执行失败", name)
        return {"ok": False, "result": f"{type(e).__name__}: {e}"}


def _write_report(args: dict) -> dict:
    """把综述/报告内容落盘为 <kb root>/_reports/<标题>.md（或指定子路径）。

    安全：标题/子路径清洗非法字符、剥离 `..` 穿越；最终路径须落在 _reports 内。
    返回 {ok, result:{path(相对), title, bytes, saved}}。
    """
    title = str(args.get("title", "")).strip()
    content = str(args.get("content", ""))
    if not content:
        return {"ok": False, "result": "内容为空，未写入"}
    base = _reports_dir().resolve()
    rel = _safe_rel_path(str(args.get("path", "")))
    if rel is None:
        return {"ok": False, "result": "非法路径（含目录穿越），已拒绝"}
    target = (base / rel).resolve() if rel else (base / f"{_safe_title(title)}.md").resolve()
    if not target.is_relative_to(base):
        return {"ok": False, "result": "非法路径（越过 _reports 根），已拒绝"}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    rel_out = target.relative_to(base).as_posix()
    info = {"path": rel_out, "title": title or rel_out, "bytes": len(content.encode("utf-8")),
            "saved": True}
    return {"ok": True, "result": _trim(info)}


def tool_specs() -> list[dict]:
    return TOOL_SPECS
