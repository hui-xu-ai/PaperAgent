# -*- coding: utf-8 -*-
"""论文 API：上传 / 批量上传 / 列表 / 详情 / 导出文件。"""
from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..services import container

router = APIRouter(prefix="/api/papers", tags=["papers"])

ALLOWED_EXT = {".pdf"}


def _require_mineru() -> None:
    """解析硬门禁（批2，用户拍板）：未配置 MinerU Key → 400 且**不建任务/不落文件**。

    与 `EngineService.parse_pdf` 的门禁同判据（`config.mineru_ready`，读 .env 实时值）；
    这里提前拦是为了给出**可操作的 400**（含 `mineru_required` 标记，前端弹「去填 Key」），
    而不是让任务排队后才失败。真正兜底仍在引擎层（唯一解析入口）。
    """
    from ..config import mineru_ready
    if not mineru_ready(container.get_settings().mineru_api_key):
        raise HTTPException(400, detail={
            "mineru_required": True,
            "message": "未配置 MinerU API Key：解析功能已禁用（免费/本地通道已移除）。"
                       "请到「设置中心 → 解析」填写 Key 并保存后再导入。",
        })


def _sanitize_filename(name: str) -> str:
    """净化上传文件名（保留有意义的名字，仅替换非法字符/控制符，限长 160）。"""
    import re as _re
    name = _re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    name = _re.sub(r"\s+", " ", name)
    return name[:160] or "paper.pdf"


def _save_upload(input_root: Path, filename: str, content: bytes,
                 run_id: str) -> Path:
    """保存上传 PDF：**文件名永远保留原始文件名**（目录命名依赖它，绝不允许
    md5/run_id 前缀污染——P2-10）。重名/内容差异用 input/<run_id>/ 子目录隔离，
    文件本身仍是 <原始文件名>.pdf。"""
    input_root.mkdir(parents=True, exist_ok=True)
    safe = _sanitize_filename(filename or "paper.pdf")
    sub = input_root / run_id
    sub.mkdir(parents=True, exist_ok=True)
    p = sub / safe
    p.write_bytes(content)
    return p


@router.post("")
async def upload_paper(file: UploadFile = File(...),
                       mode: str = Form("parse_compile"),
                       overwrite: bool = Form(False)) -> dict:
    """上传 PDF → 暂存 work/upload/ → 创建论文记录 → 提交流水线。

    2026-09-12（用户拍板 · 工业级数据布局）：暂存目录从 `input/` 改为 `work/upload/`
    （可清理区，解析产物落盘后即删）；同一 PDF（md5 相同）重复上传返回 **409** 与
    已导入篇目信息，用户确认覆盖后带 `overwrite=true` 重发 → 旧记录（含会话/任务）删除后重建。
    """
    if mode not in ("full", "parse", "parse_compile"):
        raise HTTPException(400, "mode 仅支持 full / parse / parse_compile")
    _require_mineru()          # 批2 硬门禁：无 Key 不建任务、不落文件，直接 400
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"仅支持 PDF：{file.filename}")
    run_id = uuid.uuid4().hex
    content = await file.read()
    if not content:
        raise HTTPException(400, "空文件")
    store = container.get_store()
    md5 = hashlib.md5(content).hexdigest()
    dup = store.find_paper_by_md5(md5)
    if dup and dup.get("status") in ("parsing", "translating", "translated", "parsed",
                                     "waiting_review", "reviewing", "done"):
        if not overwrite:
            raise HTTPException(409, detail={
                "duplicate": True, "md5": md5, "paper_id": dup["id"],
                "title": dup.get("title") or dup.get("pdf_name") or "",
                "doc_title": dup.get("doc_title") or "",
                "status": dup.get("status") or "",
                "message": "该 PDF 已导入过；确认覆盖后带 overwrite=true 重新提交"})
        store.delete_paper(int(dup["id"]))       # 覆盖：旧记录（含会话/任务）级联删除
        dup = None
    save_path = _save_upload(Path(container.get_settings().engine_input_root),
                             file.filename or "paper.pdf", content, run_id)

    paper_id = store.create_paper(str(save_path), title=file.filename,
                                  pdf_name=file.filename, pdf_md5=md5)
    store.update_paper(paper_id, pipeline_mode=mode)
    container.get_tasks().submit_pipeline(paper_id)
    return {"paper_id": paper_id, "status": "pending",
            "filename": file.filename, "overwritten": bool(overwrite and dup is None),
            "note": "仅解析" if mode == "parse" else "翻译流水线已提交"}


@router.post("/batch")
async def upload_papers_batch(files: list[UploadFile] = File(...),
                              mode: str = Form("parse_compile"),
                              overwrite: bool = Form(False)) -> dict:
    """上传 PDF → md5 重复检测（**提醒**，不静默跳过）→ 逐个提交流水线。

    2026-09-12（用户拍板）：同一 PDF 重复导入不再默默跳过，而是返回 `duplicates`
    让前端弹确认框；用户坚持覆盖时前端带 `overwrite=true` 重发**这几个文件**，
    后端删除旧记录（会话/任务随级联删除，library/kb 产物由重解析覆盖）后重新入库。

    每篇文献独立流水线 + 独立 paper 会话（上下文隔离，避免 token 堆积）。
    返回处理/跳过清单 + `duplicates` 明细，供前端汇报与二次确认。
    """
    if mode not in ("full", "parse", "parse_compile"):
        raise HTTPException(400, "mode 仅支持 full / parse / parse_compile")
    _require_mineru()          # 批2 硬门禁：无 Key 直接 400（批量同理，避免整批建任务后全失败）
    store = container.get_store()
    input_root = Path(container.get_settings().engine_input_root)
    input_root.mkdir(parents=True, exist_ok=True)
    processed: list[dict] = []
    skipped: list[dict] = []
    duplicates: list[dict] = []
    for f in files:
        fn = f.filename or "未命名.pdf"
        content = await f.read()
        if not content:
            skipped.append({"filename": fn, "reason": "空文件"})
            continue
        md5 = hashlib.md5(content).hexdigest()
        dup = store.find_paper_by_md5(md5)
        # 已完成的重复 → 交前端确认；未完成的旧记录（失败/待处理）仍按"重新处理"复用
        if dup and dup.get("status") in ("parsing", "translating", "translated", "parsed",
                                         "waiting_review", "reviewing", "done"):
            if not overwrite:
                duplicates.append({"filename": fn, "paper_id": dup["id"], "md5": md5,
                                   "title": dup.get("title") or dup.get("pdf_name") or "",
                                   "doc_title": dup.get("doc_title") or "",
                                   "status": dup.get("status") or ""})
                skipped.append({"filename": fn,
                                "reason": "该 PDF 已导入过（待确认是否覆盖重解析）",
                                "paper_id": dup["id"], "status": dup.get("status")})
                continue
            store.delete_paper(int(dup["id"]))   # 覆盖：旧记录（含会话/任务）级联删除
            dup = None
        run_id = uuid.uuid4().hex
        save_path = _save_upload(input_root, fn, content, run_id)
        if dup:  # 上次失败/待处理 → 复用记录重新提交（pdf_name 保留原始文件名）
            store.update_paper(dup["id"], pdf_path=str(save_path), pdf_md5=md5,
                               status="pending", error="", doc_json="", run_id="",
                               pipeline_mode=mode)
            if not dup.get("pdf_name"):
                store.update_paper(dup["id"], pdf_name=fn)
            pid = dup["id"]
            processed.append({"filename": fn, "paper_id": pid, "note": "上次失败，重新处理"})
        else:
            pid = store.create_paper(str(save_path), title=fn, pdf_name=fn, pdf_md5=md5)
            store.update_paper(pid, pipeline_mode=mode)
            processed.append({"filename": fn, "paper_id": pid,
                              "note": "覆盖重解析" if overwrite else ""})
        container.get_tasks().submit_pipeline(pid)
    container.get_event_bus().publish(
        "info", "task", "batch_upload",
        f"批量上传：处理 {len(processed)} 篇，跳过 {len(skipped)} 篇", {})
    return {"processed": processed, "skipped": skipped, "duplicates": duplicates,
            "note": f"处理 {len(processed)} 篇，跳过 {len(skipped)} 篇"}


@router.post("/{paper_id}/retry-export")
def retry_export(paper_id: int) -> dict:
    """仅重试导出备份包（V12 修复：翻译完成但导出失败时，不重新翻译不耗 token）。"""
    store = container.get_store()
    paper = store.get_paper(paper_id)
    if not paper:
        raise HTTPException(404, "论文不存在")
    doc_json = paper.get("doc_json")
    if not doc_json or not Path(doc_json).exists():
        raise HTTPException(400, "中间产物缺失，请重新上传处理")
    try:
        export = container.get_engine().export(doc_json)
    except Exception as e:  # noqa: BLE001
        container.get_event_bus().publish("error", "task", "export_failed",
                                          f"论文[{paper_id}] 重试导出失败: {e}", {})
        raise HTTPException(500, f"导出失败: {e}")
    store.update_paper(paper_id, status="translated", error="")
    # A5 断链补齐：retry_export 此前旁路 kb 纳入与自动编译，现补调用（幂等，失败不阻塞）
    try:
        container.get_tasks().ensure_kb_assembled(doc_json)
    except Exception:  # noqa: BLE001
        pass
    container.get_event_bus().publish("info", "task", "task_done",
                                      f"论文[{paper_id}] 导出完成（重试成功）",
                                      {"paper_id": paper_id, "export": str(export)})
    return {"ok": True, "export": str(export)}


@router.post("/{paper_id}/retry")
def retry_pipeline(paper_id: int) -> dict:
    """P2-B 补充：失败论文一键重试——清状态重新入队（解析/翻译流水线）。

    ★2026-09-17：入队前**自愈登记路径**（`TaskManager.repair_pdf_path`）——`papers.pdf_path`
    登记的是上传暂存件，成功解析后已被 `_clean_upload_staging` 清掉；权威副本在
    `library/<资源>/source.pdf`。不做这一步，"解析曾成功、之后失败"的篇点重试必然报
    `PAPER-0001 输入文件不存在`（用户实测缺陷）。两处都找不到 → **409 + 可操作提示**，
    而不是让它跑成"解析失败（全部通道）"。
    """
    store = container.get_store()
    paper = store.get_paper(paper_id)
    if not paper:
        raise HTTPException(404, "论文不存在")
    if paper.get("status") not in ("failed",):
        # 已完成/进行中不允许重试（防止重复消耗 MinerU/LLM 额度）
        raise HTTPException(400, f"仅失败状态的论文可重试（当前：{paper.get('status')}）")
    tasks = container.get_tasks()
    ready = tasks.repair_pdf_path(store, container.get_settings(),
                                 container.get_engine(), paper)
    if not ready:
        raise HTTPException(409, detail={
            "code": "PAPER-INPUT-MISSING",
            "message": "原始 PDF 已不在暂存区，且 library 未找到权威副本——"
                       "请在「＋导入」中重新导入该 PDF",
            "pdf_path": paper.get("pdf_path") or "",
        })
    # 注：清空 doc_json 是原有行为（重解析会写回新的）。★2026-09-18 观察：产物目录**不看**
    # doc_json —— `stages.py:489` 用 `output_dir_name(doi, pdf_path)` 现算，而重试的输入是
    # `library/<RID>/source.pdf` ⇒ 目录名落成 `source`，与已有的 `library/<RID>/` 并存（重复目录）。
    # 这是**独立于本次修复**的产物布局问题，已记入 FINDING-RETRY-STAGING §八，待拍板后再动。
    store.update_paper(paper_id, status="pending", error="",
                       doc_json="", run_id="", parse_source="")
    tasks.submit_pipeline(paper_id)  # 失败态任务会自动新建 task 记录
    container.get_event_bus().publish(
        "info", "task", "retry",
        f"论文[{paper_id}] 已重新提交流水线", {"paper_id": paper_id})
    return {"ok": True, "paper_id": paper_id, "status": "pending",
            "pdf_path": ready}



@router.delete("/{paper_id}")
def delete_paper_import(paper_id: int) -> dict:
    """删除某篇论文的**导入记录**（T5）。

    边界（关键，写清楚）：
      - **删**：backend store 的 papers 行 + 级联 tasks/sessions/messages（导入记录本身）。
      - **不删**：library/<DOI>/ 与 kb/<DOI>/ 产物（用户的知识库/翻译/解析资产，属用户所有）。
    删除后该 PDF 的 md5 不再命中（find_paper_by_md5 返回 None）→ 批量/单篇上传不再
    被"该 PDF 已处理过"拦截，可重新导入；重导会重新触发解析并重建 library/kb（可接受）。

    幂等：store.delete_paper 对已删除记录安全（返回全 0 计数）；此处 paper 不存在
    返回 404（清晰告知前端，符合现有 get_paper/retry 等路由约定）。
    """
    store = container.get_store()
    paper = store.get_paper(paper_id)
    if not paper:
        raise HTTPException(404, "论文不存在")
    removed = store.delete_paper(paper_id)
    container.get_event_bus().publish(
        "info", "task", "paper_deleted",
        f"论文[{paper_id}] 导入记录已删除（同 md5 PDF 可重新导入，知识库产物已保留）",
        {"paper_id": paper_id, "md5": removed.get("md5", ""),
         "removed_tasks": removed.get("removed_tasks", 0),
         "removed_sessions": removed.get("removed_sessions", 0)})
    # 删除后会话已级联删除；前端收到 ok 后 loadPapers() 刷新列表，同 md5 PDF 即解除拦截可重导。
    return {"ok": True, "paper_id": paper_id, "md5": removed.get("md5", ""),
            "removed_tasks": removed.get("removed_tasks", 0),
            "removed_sessions": removed.get("removed_sessions", 0),
            "removed_messages": removed.get("removed_messages", 0)}


@router.post("/{paper_id}/translate-now")
def translate_now(paper_id: int) -> dict:
    """P12F 逃生口：挂起待复核文献**立即翻译**（force 跳过剩余待复核项，
    用当前主文本——AI 误落地风险由复核回滚机制兜底）。"""
    store = container.get_store()
    paper = store.get_paper(paper_id)
    if not paper:
        raise HTTPException(404, "论文不存在")
    if paper.get("status") not in ("waiting_review", "parsed", "failed"):
        raise HTTPException(400, f"当前状态不可立即翻译（{paper.get('status')}）")
    msg = container.get_tasks().continue_translate(paper_id, force=True)
    if msg != "ok":
        raise HTTPException(400, msg)
    container.get_event_bus().publish(
        "info", "task", "translate_now",
        f"论文[{paper_id}] 已强制启动翻译（跳过待复核项）", {"paper_id": paper_id})
    return {"ok": True, "paper_id": paper_id, "message": msg}


class PaperTemplateBody(BaseModel):
    template: str = ""


@router.post("/{paper_id}/template")
def set_paper_template(paper_id: int, body: PaperTemplateBody) -> dict:
    """设置单篇输出模板（T06）：保存覆盖 + 本地重渲染 kb 中英对照主产物 en_zh.md（不耗 token）。"""
    store = container.get_store()
    paper = store.get_paper(paper_id)
    if not paper:
        raise HTTPException(404, "论文不存在")
    tpl = body.template.strip()
    svc = container.get_settings_service()
    if tpl not in svc.MD_TEMPLATES:
        raise HTTPException(400, f"模板必须是 {svc.MD_TEMPLATES} 之一")
    store.update_paper(paper_id, md_template=tpl)
    doc_json = paper.get("doc_json")
    rerendered = False
    if doc_json and Path(doc_json).exists():
        try:
            container.get_engine().rerender_paper(doc_json, tpl)
            rerendered = True
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"重渲染失败: {e}")
    container.get_event_bus().publish(
        "info", "settings", "template",
        f"论文[{paper_id}] 模板已设为 {tpl}" + ("（已重渲染）" if rerendered else ""),
        {"paper_id": paper_id, "template": tpl})
    return {"ok": True, "template": tpl, "rerendered": rerendered}


# E3：store.list_papers 默认 limit=50（仅最近 50 篇）——分页需全量扫描后再过滤/排序/切片；
# 5000 篇量级全量 SELECT 为轻量扫描，重活（doc_summary/latest_task）仅作用于当前页。
_FULL_SCAN_LIMIT = 1_000_000


def _kind_of_paper(doc_json: str) -> str:
    """论文资源类型（F1/F2）：目录名前缀推导（与 paperkb.layout 同一来源）。

    论文目录名**从不**以 `kind__` 开头（论文为无前缀），故直接按资源目录名判定。
    """
    from paperkb.layout import kind_from_rid

    name = Path(doc_json).parent.name if doc_json else ""
    return kind_from_rid(name if name.startswith(
        ("book__", "thesis__", "std__", "patent__", "chapter__")) else "")


def _attachment_count(doc_json: str) -> int:
    """该文献的附件数（F3：文献卡 `📎 附件 N`）；只扫两层目录，不递归 stat。"""
    if not doc_json:
        return 0
    base = Path(doc_json).parent / "attachments"
    if not base.is_dir():
        return 0
    n = 0
    for kind_dir in base.iterdir():
        if kind_dir.is_dir():
            n += sum(1 for f in kind_dir.iterdir()
                     if f.is_file() and not f.name.startswith("."))
    return n


@router.get("")
def list_papers(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    q: str = Query("", max_length=200),
    all: bool = Query(False, description="O2：一次性返回全部(过滤后)以供前端按日期分组"),
    kind: str = Query("", description="F1：资源类型过滤（all/none/paper/thesis/book/…）"),
    has_attachment: bool = Query(False, description="F1：只留有附件（SI/审稿意见）的文献"),
) -> dict:
    """论文列表分页（E3：5000 篇不卡）。

    全量取 → q 过滤（标题/文件名子串，大小写不敏感）→ created_at 降序 → 分页切片。
    all=True 时忽略 page/page_size，返回全部(过滤后)供前端按导入日期分组跳转。
    kind/has_attachment：F1 类型芯片（服务端过滤；`none`=无编号 `nd-`）。
    返回 total=过滤后总数；papers 元素字段结构不变（兼容旧调用）。
    """
    store = container.get_store()
    engine = container.get_engine()
    all_papers = store.list_papers(limit=_FULL_SCAN_LIMIT)
    ql = q.strip().lower()
    if ql:
        all_papers = [p for p in all_papers
                      if ql in (p.get("title") or "").lower()
                      or ql in (p.get("pdf_name") or p.get("title") or "").lower()]
    kl = (kind or "").strip().lower()
    if kl and kl != "all":
        def _match(p: dict) -> bool:
            d = p.get("doc_json") or ""
            name = Path(d).parent.name if d else ""
            k = _kind_of_paper(d) or "paper"
            if kl == "none":
                return name.startswith("nd-") or bool(re.fullmatch(r"[0-9a-f]{32}", name))
            return k == kl

        all_papers = [p for p in all_papers if _match(p)]
    if has_attachment:
        all_papers = [p for p in all_papers if _attachment_count(p.get("doc_json") or "") > 0]
    all_papers.sort(key=lambda p: p.get("created_at") or "", reverse=True)
    total = len(all_papers)
    if all:
        start, page_size = 0, max(_FULL_SCAN_LIMIT, page_size)
    else:
        start = (page - 1) * page_size
    papers = []
    for p in all_papers[start:start + page_size]:
        summary = {}
        if p["doc_json"]:
            try:
                summary = engine.doc_summary(p["doc_json"])
            except Exception:  # noqa: BLE001
                summary = {}
        task = store.latest_task(p["id"], "pipeline")
        # P0-A（2026-09-11）磁盘为准：解析产物是否仍在。用户会在资源管理器手删
        # library/<dir>/，若前端只信 DB 就会"文件没了卡片还在"。False 时前端标
        # 「⚠ 产物已丢失」并引导用删除记录清理（DELETE /api/papers/{id}）。
        _doc_json = p.get("doc_json") or ""
        _on_disk = bool(_doc_json) and Path(_doc_json).exists()
        # 2026-09-12 用户反馈：导入 PDF 解析**进行中**，文献库会短暂冒「⚠ 产物已丢失 /
        # 清理这些记录」。根因：解析中 `papers.doc_json` 还是空串（要到解析成功才写入）
        # ⇒ on_disk 恒 False；而前端有 5s 轮询 loadPapers，于是全程每 5s 闪一次。
        # "还没生成" ≠ "已丢失"：任务在跑（或被显式重导清空）时不判失效。
        _task_state = (task or {}).get("state", "")
        _in_flight = _task_state in ("pending", "running") or p.get("status") in ("pending", "parsing")
        if _in_flight and not _doc_json:
            _on_disk = True
        # 2026-09-12：library 产物被删/移走后 doc_summary 读不出来 → doi/doc_title 变空，
        # 该篇在前端连"挂哪个父资源/对应哪篇知识库"都无法确定（resourceKeyOf 退化成空）。
        # 兜底用**解析时登记的** doi_md5_map（目录名 → DOI，与磁盘无关）。
        _doi = summary.get("doi", "")
        if _doc_json and not _doi:
            try:
                from ..services.kbmeta_service import get_kbmeta

                _row = get_kbmeta().get_doi_md5_map(Path(_doc_json).parent.name) or {}
                _doi = str(_row.get("doi") or "").strip()
            except Exception:  # noqa: BLE001 - 兜底失败按无 DOI 处理，不影响列表
                _doi = ""
        papers.append({
            "id": p["id"], "title": p["title"], "status": p["status"],
            "on_disk": _on_disk,
            "error": p["error"], "doc_title": summary.get("title", ""),
            "doi": _doi,   # Q5：知识库关联（kb 状态/编译操作）
            "filename": p.get("pdf_name") or p["title"],  # P2-6：原始 PDF 文件名（有意义）
            "paragraph_count": summary.get("paragraph_count", 0),
            "translated_paragraphs": summary.get("translated_paragraphs", 0),
            "task_state": (task or {}).get("state", ""),
            "task_progress": (task or {}).get("progress", 0),
            "task_message": (task or {}).get("message", ""),
            "created_at": p["created_at"],
            "md_template": p.get("md_template", ""),
            # P2-B：解析来源（mineru-v4 精准 / mineru 免费 / pymupdf-local 本地）
            "parse_source": p.get("parse_source", ""),
            # P5 点1：流水线模式（full=解析+翻译+编译 / parse_compile=解析+编译不翻译 / parse=仅解析）
            "pipeline_mode": p.get("pipeline_mode", "full"),
            # P0-B step3（F2/F3）：资源类型 + 附件数（卡片徽标与附件入口）
            "kind": _kind_of_paper(_doc_json) or "paper",
            "attachments": _attachment_count(_doc_json),
        })
    return {"papers": papers, "total": total, "page": page, "page_size": page_size}


@router.get("/{paper_id}")
def get_paper(paper_id: int) -> dict:
    store = container.get_store()
    paper = store.get_paper(paper_id)
    if not paper:
        raise HTTPException(404, "论文不存在")
    summary = {}
    if paper["doc_json"]:
        try:
            summary = container.get_engine().doc_summary(paper["doc_json"])
        except Exception:  # noqa: BLE001
            summary = {}
    return {"paper": paper, "summary": summary,
            "tasks": store.list_tasks(paper_id)}


@router.get("/{paper_id}/files")
def list_export_files(paper_id: int) -> dict:
    """列出统一规范库内文件（library/<DOI>/，用于下载/预览）。

    G13：按 DOI/文件名定位目录（doc_json 可能指向已迁移/失效路径，不能依赖其父目录）。
    """
    store = container.get_store()
    paper = store.get_paper(paper_id)
    if not paper:
        raise HTTPException(404, "论文不存在")
    out_root = Path(container.get_settings().engine_work_root).resolve()
    folder_name = ""
    if paper.get("doc_json"):
        try:
            if Path(paper["doc_json"]).exists():
                summary = container.get_engine().doc_summary(paper["doc_json"])
                folder_name = container.get_kb().paper_dir(
                    summary.get("doi") or "", paper.get("run_id", ""),
                    fallback=Path(paper.get("pdf_path", "")).stem)
            else:
                folder_name = Path(paper["doc_json"]).resolve().parent.parent.name
        except Exception:  # noqa: BLE001
            folder_name = ""
    if not folder_name:
        folder_name = Path(paper.get("pdf_path", "")).stem
        import re as _re
        folder_name = _re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", folder_name).strip(" .")
    export_root = (out_root / folder_name) if folder_name else out_root
    files = []
    if export_root.is_dir():
        for f in sorted(export_root.rglob("*")):
            if f.is_file():
                rel = f.relative_to(out_root)
                # P2-B 修复：统一正斜杠路径（Windows str(Path) 反斜杠会破坏前端 '/' 切分）
                files.append({"path": rel.as_posix(), "name": f.name,
                              "size": f.stat().st_size})
    return {"files": files}


@router.get("/files/download")
def download_file(path: str = Query(...)) -> FileResponse:
    """下载规范库文件（校验路径在 engine_work_root 内，防目录穿越）。"""
    out_root = Path(container.get_settings().engine_work_root).resolve()
    target = (out_root / path).resolve()
    if not target.is_relative_to(out_root) or not target.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(target, filename=target.name)


@router.get("/{paper_id}/export")
def download_export(paper_id: int) -> FileResponse:
    """下载整篇论文的英文干净版主产物（library/<DOI>/en.md）。

    T4：中英对照主产物已迁 kb（en_zh.md），library 下载源统一为 en.md。
    """
    store = container.get_store()
    paper = store.get_paper(paper_id)
    if not paper or not paper["doc_json"]:
        raise HTTPException(404, "导出文件不存在")
    doc_path = Path(paper["doc_json"]).resolve()
    folder = doc_path.parent if doc_path.parent.name != "intermediate" else doc_path.parent.parent
    paper_md = folder / "en.md"
    if not paper_md.is_file():
        raise HTTPException(404, "en.md 未生成（解析未完成？）")
    return FileResponse(paper_md, filename=paper_md.name)
