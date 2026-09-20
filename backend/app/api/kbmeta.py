# -*- coding: utf-8 -*-
"""知识库元数据 API（M1）：bib 导入预览/确认、引用列表、WOS 检索式、元数据查询。

独立于 /api/kb（知识库文件操作）；此处是 paperkb 门面的 backend 暴露层。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from ..services import container

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/kb-meta", tags=["kb-meta"])


class BibRequest(BaseModel):
    path: str = Field(..., description="bib 文件路径（本机路径，如 用户提供的文献/Bib文件/savedrecs_1.bib）")


class WosQueryRequest(BaseModel):
    dois: list[str] = []


@router.get("/status")
def kbmeta_status() -> dict:
    return container.get_kbapi().status()


@router.get("/stats")
def kbmeta_stats() -> dict:
    """知识库统计/总览（P4 首页统计面板）：规模/编译完成度/缺失清单。"""
    return container.get_kbapi().stats()


@router.get("/reconcile")
def kbmeta_reconcile() -> dict:
    """磁盘 ↔ 数据库对账（P0-A「磁盘为准」）。

    用户在资源管理器手删/手拷文件后，DB 只是派生索引——本端点列出两侧不一致项
    （orphan_dir=盘有产物无元数据 / ghost_row=库有记录盘无目录），前端据此显示
    「发现 N 处不一致」并提供处理入口（补元数据 / 清理记录）。
    """
    return container.get_kbapi().reconcile()


@router.post("/bib/preview")
def bib_preview(req: BibRequest) -> dict:
    """解析 bib → 预览摘要（不 dump 全文；含 DOI 去重/缺失字段警告）。"""
    try:
        return container.get_kbapi().bib_preview(req.path)
    except FileNotFoundError as e:
        raise HTTPException(404, f"文件不存在: {e}")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"解析失败: {e}")


@router.post("/bib/import")
def bib_import(req: BibRequest) -> dict:
    """确认导入：papers_meta upsert + 引用边重建（幂等，重导=更新）。"""
    try:
        return container.get_kbapi().bib_import(req.path)
    except FileNotFoundError as e:
        raise HTTPException(404, f"文件不存在: {e}")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"导入失败: {e}")


@router.get("/papers")
def kbmeta_papers(limit: int = 500) -> list[dict]:
    return container.get_kbapi().list_papers(limit)


@router.get("/paper")
def kbmeta_paper(doi: str = Query(..., description="DOI（含 /，用 query 参数传递）")) -> dict:
    meta = container.get_kbapi().get_paper(doi)
    if meta is None:
        raise HTTPException(404, f"未找到元数据: {doi}")
    return meta


@router.get("/citations")
def kbmeta_citations(doi: str = Query(...)) -> dict:
    return container.get_kbapi().citations_for(doi)


@router.get("/search")
def kbmeta_search(q: str = Query(..., min_length=1), limit: int = 20) -> list[dict]:
    return container.get_kbapi().search(q, limit)


@router.get("/missing-dois")
def kbmeta_missing() -> dict:
    """扫描内容源中缺 papers_meta 的 DOI + 生成 WOS 检索式。"""
    return container.get_kbapi().missing_dois()


@router.post("/wos-query")
def kbmeta_wos(req: WosQueryRequest) -> dict:
    return {"queries": container.get_kbapi().wos_query(req.dois)}


# ---------------------------------------------------------- M2：期刊/评分

@router.post("/journals/preview")
def journals_preview(req: BibRequest) -> dict:
    """JCR xlsx 预览（只回摘要，不 dump 全文）。"""
    try:
        return container.get_kbapi().journals_preview(req.path)
    except FileNotFoundError as e:
        raise HTTPException(404, f"文件不存在: {e}")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"解析失败: {e}")


@router.post("/journals/import")
def journals_import(req: BibRequest) -> dict:
    """确认导入 JCR xlsx → journals.db（jcr+cas upsert，幂等）。"""
    try:
        return container.get_kbapi().journals_import(req.path)
    except FileNotFoundError as e:
        raise HTTPException(404, f"文件不存在: {e}")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"导入失败: {e}")


@router.get("/journals/lookup")
def journals_lookup(name: str = Query("", description="期刊名（规范化匹配）"),
                    issn: str = Query("", description="ISSN/eISSN（精确匹配，优先）"),
                    year: int | None = None) -> dict:
    svc = container.get_kbapi()
    r = None
    if issn:
        r = svc.journals_lookup_issn(issn)
    if r is None and name:
        r = svc.journals_lookup(name, year)
    if r is None:
        raise HTTPException(404, f"期刊未匹配: name={name} issn={issn}")
    return r


@router.get("/journals/stats")
def journals_stats() -> dict:
    return container.get_kbapi().journals_stats()


class MetaEnrichRequest(BaseModel):
    doi: str = ""
    paper_id: int = 0


@router.post("/meta/enrich")
def kbmeta_meta_enrich(req: MetaEnrichRequest) -> dict:
    """DOI → 公开元数据源（Crossref 优先 / OpenAlex 兜底）**临时补全** `papers_meta`。

    用户提问（2026-09-12）："有没有能根据文献内解析的 doi 号，临时补全元数据的方法？"
    —— 有，且免费无需 Key。**只补空字段**：bib 导入后的权威值不会被覆盖；
    自动补上的值会标 `source_file = crossref:<doi>` / `openalex:<doi>` 以示区分。
    补全后即可算出价值分（IF 档来自 journals.db）→ 自动升级链才可能升 L2/L3。
    """
    try:
        from ..services.doi_meta import enrich_paper_meta

        r = enrich_paper_meta(req.doi, paper_id=req.paper_id or None)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"元数据补全失败: {e}")
    if not r.get("ok"):
        raise HTTPException(404, f"未能补全: {r.get('reason')}（doi={req.doi}）")
    return r


class JournalOverrideRequest(BaseModel):
    doi: str
    journal_name: str


@router.post("/journals/override")
def journals_override(req: JournalOverrideRequest) -> dict:
    """人工纠正期刊绑定（匹配失败时指定 journals.db 标准名）。"""
    try:
        return container.get_kbapi().set_journal_override(req.doi, req.journal_name)
    except KeyError as e:
        raise HTTPException(404, str(e))


@router.get("/scores")
def kbmeta_scores() -> list[dict]:
    """全部文献价值评分（编译队列排序依据）。"""
    return container.get_kbapi().list_scores()


@router.get("/score")
def kbmeta_score(doi: str = Query(...)) -> dict:
    s = container.get_kbapi().value_score(doi)
    if s is None:
        raise HTTPException(404, f"未找到元数据: {doi}")
    return s


# ---------------------------------------------------------- M3：编译/卡片

class CompileRequest(BaseModel):
    doi: str
    level: str = "L1"
    force: bool = False


class CompileQueueRequest(BaseModel):
    doi: str
    level: str | None = None


class CompileProcessRequest(BaseModel):
    limit: int = 1


class CompileBatchRequest(BaseModel):
    dois: list[str]
    action: str = "retry"  # "retry" | "l3"


def _publish_compile(doi: str, level: str, result: object, *, queued: bool = False) -> None:
    """编译收尾/入队 → 事件总线（前端据此刷新阅读器「编译结果标签页」）。

    2026-09-12 用户实测修复：**同步** `compile/now` 与 `compile/queue` 都不经过
    `CompileWorker`（worker 的 notify 只覆盖后台队列路径）⇒ 手动编译完成时前端收不到任何信号，
    阅读器标签页永远不刷新（表现为"编译完了，却没出现 `_note.md`/`_wiki.md` 标签"）。
    """
    try:
        err = isinstance(result, dict) and bool(result.get("error"))
        container.get_event_bus().publish(
            "warning" if err else "info", "kb",
            "compile_queue" if queued else "compile_done",
            f"编译{'入队' if queued else '完成'}: {doi} {level}",
            {"doi": doi, "level": level,
             "result": result if isinstance(result, dict) else {},
             "error": (result or {}).get("error", "") if isinstance(result, dict) else ""})
    except Exception:  # noqa: BLE001 - 事件发布失败不影响编译结果
        logger.exception("编译事件发布失败 doi=%s", doi)


@router.post("/compile/now")
def compile_now(req: CompileRequest) -> dict:
    """立即编译（同步 LLM 调用；L1/L2/L3）。"""
    try:
        r = container.get_kbapi().compile_now(req.doi, req.level, req.force)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"编译失败: {e}")
    _publish_compile(req.doi, req.level, r)
    return r


@router.post("/compile/queue")
def compile_queue(req: CompileQueueRequest) -> dict:
    """入队（level 缺省按价值分判定）。"""
    try:
        r = container.get_kbapi().compile_queue(req.doi, req.level)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"入队失败: {e}")
    _publish_compile(req.doi, req.level or "", r, queued=True)
    return r


@router.post("/compile/queue-all")
def compile_queue_all() -> dict:
    """按价值分批量入队（L1 全做，L2/L3 按分）。"""
    return container.get_kbapi().compile_queue_all()


@router.post("/compile/process")
def compile_process(req: CompileProcessRequest) -> list[dict]:
    """处理队列（worker 循环）。"""
    return container.get_kbapi().compile_process(req.limit)


@router.post("/compile/batch")
def compile_batch(req: CompileBatchRequest) -> dict:
    """批量编译（直接执行，不走队列）。

    action="retry": 重试失败的编译（force L1）
    action="l3": 执行 L3 跨文献概念分析
    """
    try:
        return container.get_kbapi().compile_batch(req.dois, req.action)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"批量编译失败: {e}")


@router.get("/compile/jobs")
def compile_jobs(status: str | None = None) -> list[dict]:
    """编译队列（含价值分）。

    批5（2026-09-12 用户实测）：早期用**显式 level** 入队的行 `value_score` 记成 0 ⇒ 队列表格
    显示 0.00（而知识库列表里该篇是 3.48），看起来像"这篇没价值"。新入队行已由
    `Compiler.queue` 直接写入真实分；这里对**缺失/为 0 的历史行**用当前价值分兜底填充
    （判据与 `/api/kb-meta/scores` 同一函数），保证队列显示始终反映该篇真实分数。
    """
    kbapi = container.get_kbapi()
    rows = kbapi.compile_status(status)
    filled: dict[str, float] = {}
    for r in rows:
        try:
            cur = float(r.get("value_score") or 0.0)
        except (TypeError, ValueError):
            cur = 0.0
        if cur > 0:
            continue
        doi = str(r.get("paper_doi") or "")
        if not doi:
            continue
        if doi not in filled:
            try:
                s = kbapi.value_score(doi)
                filled[doi] = float((s or {}).get("score") or 0.0)
            except Exception:  # noqa: BLE001 - 单篇评分失败不影响队列返回
                filled[doi] = 0.0
        r["value_score"] = filled[doi]
    return rows


class CardWriteRequest(BaseModel):
    doi: str
    card_type: str          # card-translate/card-summary/card-qa/card-note
    content: str
    title: str = ""
    para_ids: list[str] = []
    tags: list[str] = []


@router.post("/cards")
def card_write(req: CardWriteRequest) -> dict:
    try:
        return container.get_kbapi().card_write(req.doi, req.card_type, req.content,
                                       title=req.title, para_ids=req.para_ids,
                                       tags=req.tags)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/cards")
def card_list(doi: str | None = None, card_type: str | None = None) -> list[dict]:
    return container.get_kbapi().card_list(doi, card_type)


class QaSaveRequest(BaseModel):
    question: str
    answer: str
    doi: str = ""
    sources: list[str] = []
    scope: str = "paper"


@router.post("/qa/save")
def qa_save(req: QaSaveRequest) -> dict:
    """E3 写回飞轮：问答一键存卡片 + 索引进 notes_fts（scope=paper 文献 / global 全局）。"""
    if not req.question.strip() or not req.answer.strip():
        raise HTTPException(400, "问题与回答不能为空")
    return container.get_kbapi().save_qa(req.question, req.answer, doi=req.doi,
                                sources=req.sources, scope=req.scope)


# ---------------------------------------------------------- M3b：翻译

class TranslateRequest(BaseModel):
    doc_json: str


@router.post("/translate")
def translate(req: TranslateRequest) -> dict:
    """翻译 document.json（总结+分批译文，写回 text_zh/ai_summary）。"""
    try:
        return container.get_kbapi().translate_now(req.doc_json)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"翻译失败: {e}")


class TranslateCompileRequest(BaseModel):
    doc_json: str
    doi: str
    level: str = "L1"


@router.post("/translate-compile")
async def translate_compile_parallel(req: TranslateCompileRequest) -> dict:
    """并行执行翻译+编译（共享全文前缀缓存命中）。

    2026-09-19 用户反馈：翻译和编译不应串行执行，应该并行请求以最大化缓存效率。
    两者都使用 shared_ctx(doc) 作为全文前缀，并行请求时只要前缀一致就能命中缓存。
    """
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    kbapi = container.get_kbapi()

    def _do_translate():
        return kbapi.translate_now(req.doc_json)

    def _do_compile():
        return kbapi.compile_now(req.doi, req.level)

    loop = asyncio.get_event_loop()
    executor = ThreadPoolExecutor(max_workers=2)

    try:
        translate_task = loop.run_in_executor(executor, _do_translate)
        compile_task = loop.run_in_executor(executor, _do_compile)

        translate_result, compile_result = await asyncio.gather(
            translate_task, compile_task, return_exceptions=True
        )

        # 处理异常结果
        if isinstance(translate_result, Exception):
            translate_result = {"error": str(translate_result)}
        if isinstance(compile_result, Exception):
            compile_result = {"error": str(compile_result)}

        return {
            "translate": translate_result,
            "compile": compile_result,
            "parallel": True,
        }
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"并行执行失败: {e}")


# ---------------------------------------------------------- M4：检索/问答

class AskRequest(BaseModel):
    query: str
    top_k: int = 8


@router.post("/ask")
def kbmeta_ask(req: AskRequest) -> dict:
    """问答编排（召回编译产物 → 组装 ≤预算 → LLM 回答，引用 [[DOI]]）。"""
    try:
        return container.get_kbapi().ask(req.query, req.top_k)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"问答失败: {e}")


@router.get("/recall")
def kbmeta_recall(q: str = Query(..., min_length=1), top_k: int = 8) -> list[dict]:
    return container.get_kbapi().recall(q, top_k)


@router.post("/fts/rebuild")
def kbmeta_fts_rebuild() -> dict:
    return container.get_kbapi().rebuild_fts()


# ---------------------------------------------------------- M5：导入流程

class MarkdownImportRequest(BaseModel):
    path: str = Field(..., description="markdown 文件路径（本机路径）")


class SourceSyncRequest(BaseModel):
    doi: str
    force: bool = False


@router.post("/markdown/import")
def kbmeta_markdown_import(req: MarkdownImportRequest) -> dict:
    """途径 B：markdown 导入 → library/<DOI>/en.md + document.json → kb 纳入 → 入队 L1。"""
    try:
        return container.get_kbapi().import_markdown(req.path)
    except FileNotFoundError as e:
        raise HTTPException(404, f"文件不存在: {e}")
    except ValueError as e:
        raise HTTPException(400, f"导入失败: {e}")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"导入失败: {e}")


@router.post("/source/sync")
def kbmeta_source_sync(req: SourceSyncRequest) -> dict:
    """kb 原文层四件纳入/重新同步（force=True=手动"重新同步原文"，默认冻结不动）。

    2026-09-12 用户反馈：修复前解析的文献 library 缺 `source.pdf`（parse/parse_compile
    不走 `_inplace_export`）→ 只从 library 复制永远补不出 PDF。这里先按原始上传 PDF
    幂等补齐 library，再走原来的同步（只补缺、不覆盖）。
    """
    try:
        container.get_tasks().ensure_library_source_pdf_for_key(req.doi)
    except Exception as e:  # noqa: BLE001 - 补齐失败不阻塞同步本身
        logger.warning("补齐 library source.pdf 失败（继续同步）: %s", e)
    try:
        return container.get_kbapi().source_sync(req.doi, req.force)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"同步失败: {e}")


# ---------------------------------------------------------------- 回收站（2026-09-12 用户需求）
class TrashRequest(BaseModel):
    key: str = Field(..., description="资源键（DOI / RID / 目录名）")
    note: str = ""


@router.post("/kb/trash")
def kbmeta_trash(req: TrashRequest) -> dict:
    """把一篇资源**移出知识库到回收站**：磁盘产物全部保留，仅从知识库显示与检索中摘除。"""
    try:
        return container.get_kbapi().kb_trash_move(req.key, req.note)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"移出知识库失败: {e}")


@router.post("/kb/restore")
def kbmeta_restore(req: TrashRequest) -> dict:
    """从回收站恢复回知识库（原样搬回 + 重建索引与附件索引）。"""
    try:
        return container.get_kbapi().kb_trash_restore(req.key)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"恢复失败: {e}")


@router.get("/kb/trash")
def kbmeta_trash_list() -> dict:
    """回收站清单（主界面「恢复」按钮的数据源）。"""
    return container.get_kbapi().kb_trash_list()


@router.post("/kb/trash/delete")
def kbmeta_trash_delete(req: TrashRequest) -> dict:
    """彻底删除回收站里的**单篇**资源（不可恢复；仅删 .trash 目录，不动文献库/解析产物）。"""
    try:
        return container.get_kbapi().kb_trash_delete(req.key)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"彻底删除失败: {e}")


@router.post("/kb/trash/empty")
def kbmeta_trash_empty() -> dict:
    """清空回收站（彻底删除全部，不可恢复；仅作用于 knowledge_base/.trash/）。"""
    try:
        return container.get_kbapi().kb_trash_empty()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"清空回收站失败: {e}")


@router.get("/source/status")
def kbmeta_source_status(doi: str = Query(...)) -> dict:
    return container.get_kbapi().source_status(doi)


@router.get("/kb/status")
def kbmeta_kb_status() -> list[dict]:
    """全部 kb/<DOI>/ 状态（原文层四件 + 编译产物）——前端同步总览。"""
    return container.get_kbapi().kb_status()


@router.get("/kb/list")
def kbmeta_kb_list(q: str = "", journal: str = "",
                   compile_status: str = "",
                   score_min: float = 0.0,
                   sort: str = Query("value", pattern="^(value|year|title)$"),
                   page: int = Query(1, ge=1),
                   page_size: int = Query(50, ge=1, le=200),
                   kind: str = "",
                   has_attachment: bool = False,
                   quartile: str = "",
                   year_from: int | None = None,
                   year_to: int | None = None,
                   min_if: float = 0.0) -> dict:
    """知识库三视图聚合列表：bib 元数据 × 价值分 × kb/编译状态（过滤/排序/分页）。

    compile_status: done=已编译非空 / none=未编译 / L1|L2|L3=该等级已编译。
    kind: 空/all=全部；none=无编号资料；paper|thesis|book|chapter|patent|standard|report|note。
    has_attachment: 只留有附件（SI/审稿意见/数据）的资源（F1/A3）。
    quartile: 分区筛选（逗号分隔，如 Q1,Q2）。
    year_from / year_to: 年份范围。
    min_if: 最低影响因子。
    """
    return container.get_kbapi().kb_list(q=q, journal=journal,
                                compile_status=compile_status,
                                score_min=score_min, sort=sort,
                                page=page, page_size=page_size,
                                kind=kind, has_attachment=has_attachment,
                                quartile=quartile, year_from=year_from,
                                year_to=year_to, min_if=min_if)


@router.get("/kind/options")
def kbmeta_kind_options() -> dict:
    """类型芯片清单 + 计数（F1：前端筛选行直接用服务端计数，不做全量拉取）。"""
    return container.get_kbapi().kind_options()


@router.get("/attachments")
def kbmeta_attachments(rid: str = Query(..., description="资源 RID 或目录键")) -> dict:
    """某资源的附件清单（F3/A3）：相对路径/大小/修改时间/是否已索引。"""
    return container.get_kbapi().kb_attachments(rid)


@router.get("/attachment/read")
def kbmeta_attachment_read(rid: str = Query(...), path: str = Query(...),
                           max_chars: int = Query(20000, ge=200, le=200000)) -> dict:
    """读附件文本片段（A4；白名单路径校验：越权/不存在一律 404，不泄露磁盘结构）。"""
    out = container.get_kbapi().kb_attachment_read(rid, path, max_chars=max_chars)
    if not out.get("ok"):
        raise HTTPException(404 if "不存在" in out.get("error", "") else 400,
                            out.get("error") or "读取失败")
    return out


@router.get("/attachment/file")
def kbmeta_attachment_file(rid: str = Query(...), path: str = Query(...)):
    """附件原文件（预览/下载；白名单路径校验：越权/不存在一律 404）。"""
    import mimetypes
    from pathlib import Path

    from fastapi.responses import FileResponse

    p = container.get_kbapi().kb_attachment_path(rid, path)
    if p is None:
        raise HTTPException(404, "附件不存在或路径未被允许")
    media = mimetypes.guess_type(Path(p).name)[0] or "application/octet-stream"
    return FileResponse(str(p), media_type=media, filename=Path(p).name)


@router.post("/attachments/import")
async def kbmeta_attachment_import(file: UploadFile = File(...),
                                   rid: str = Query("", description="父资源 RID/目录键；空=无父资源"),
                                   kind: str = Query("data", description="si|review|data|note|…"),
                                   index: bool = Query(True, description="是否索引进 notes_fts")):
    """轻量导入一份附件（F5）：挂父资源目录 `attachments/<kind>/`，**0 API 成本**。

    无父资源时按内容指纹生成 RID（`nd-<md5前12>`），落独立根 `attachments/<RID>/`。
    """
    data = await file.read()
    if not data:
        raise HTTPException(400, "空文件")
    kb = container.get_kbapi()
    if not rid:
        import hashlib

        rid = "nd-" + hashlib.md5(data).hexdigest()[:12]
    out = kb.kb_attachment_import(rid, kind, file.filename or "attachment.bin",
                                  data, index=index)
    if not out.get("ok"):
        raise HTTPException(400, out.get("error") or "导入失败")
    return out


@router.post("/attachments/reindex")
def kbmeta_attachment_reindex() -> dict:
    """全量重扫附件文本入检索索引（用户手工拷入附件的补齐入口）。"""
    return container.get_kbapi().kb_attachment_reindex()


@router.get("/source/verify")
def kbmeta_source_verify(doi: str = Query(...), base: str = "kb") -> dict:
    """en.md ↔ document.json 一致性校验。"""
    return container.get_kbapi().verify_doc(doi, base)


@router.post("/backfill")
def kbmeta_backfill(force: bool = False) -> dict:
    """扫 library 全部补齐 kb 原文层四件（历史文献一键补齐，冻结不覆盖）。"""
    return container.get_kbapi().backfill_kb(force=force)


@router.post("/index/regenerate")
def kbmeta_index_regenerate() -> dict:
    """重建 kb 总索引 _index.md（编译/导入/同步后自动；也可手动触发）。"""
    return container.get_kbapi().regenerate_index()


@router.post("/sync-lit-meta")
def kbmeta_sync_lit_meta() -> dict:
    """从 paperlit 的 lit.db 同步文献计量数据（PaperRank/分区/影响因子/库内被引）到 papers_meta。"""
    return container.get_kbapi().sync_lit_meta()


@router.post("/fill-journal-meta")
def kbmeta_fill_journal_meta() -> dict:
    """批量补全缺失的 IF/分区：从 journals.db 按 ISSN/期刊名查找并写入 papers_meta。"""
    from packages.paperkb.paperkb.api import fill_journal_meta_batch
    return fill_journal_meta_batch()


@router.post("/cleanup-stale")
def kbmeta_cleanup_stale() -> dict:
    """清理幽灵记录：kb 目录已删除但数据库仍有元数据/编译任务的条目。"""
    return container.get_kbapi().cleanup_stale_records()


@router.post("/compile/backfill")
def kbmeta_compile_backfill() -> dict:
    """为 kb 中未完成 L1 的文献入队（补齐编译结构标准）。"""
    return container.get_kbapi().compile_backfill()


@router.post("/vector/rebuild")
def kbmeta_vector_rebuild(force: bool = False) -> dict:
    """重建 KB 向量索引（编译结果 _note.md + _wiki.md + concepts）。"""
    from paperkb.api import rebuild_kb_vector_index
    return rebuild_kb_vector_index(force=force)


@router.post("/markdown/upload")
async def kbmeta_markdown_upload(file: UploadFile = File(...)):
    """上传 markdown 文件导入（主界面「导入 md」按钮）：存临时 → 途径 B 导入。

    文件名建议含 DOI（10.xxxx_yyy.md），否则按标题匹配已导 bib。
    """
    if not (file.filename or "").lower().endswith(".md"):
        raise HTTPException(400, "仅支持 .md 文件")
    import tempfile
    from pathlib import Path

    suffix = Path(file.filename or "x.md").suffix
    tmp = Path(tempfile.gettempdir()) / f"kbmd_{file.filename or 'paper'}{suffix}"
    content = await file.read()
    tmp.write_bytes(content)
    try:
        return container.get_kbapi().import_markdown(str(tmp))
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


@router.post("/bib/upload")
async def kbmeta_bib_upload(file: UploadFile = File(...)):
    """上传 bib 文件导入（E2 统一导入向导）：存临时 → bib_preview → bib_import。

    返回导入结果（papers_meta upsert + 引用边重建 + FTS），含预览摘要。
    """
    if not (file.filename or "").lower().endswith(".bib"):
        raise HTTPException(400, "仅支持 .bib 文件")
    import tempfile
    from pathlib import Path

    suffix = Path(file.filename or "x.bib").suffix
    tmp = Path(tempfile.gettempdir()) / f"kbbib_{file.filename or 'refs'}{suffix}"
    tmp.write_bytes(await file.read())
    try:
        preview = container.get_kbapi().bib_preview(str(tmp))
        result = container.get_kbapi().bib_import(str(tmp))
        result["preview"] = preview
        return result
    except FileNotFoundError as e:
        raise HTTPException(404, f"文件不存在: {e}")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"导入失败: {e}")
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
