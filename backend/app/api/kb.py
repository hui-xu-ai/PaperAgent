# -*- coding: utf-8 -*-
"""知识库 API（R1 收敛）：目录树 / 文件读写 / 图片 / 索引 / 论文定位。

R1：移除 V04 的 /generate/{paper_id} 与 /backfill（一级/二级笔记与索引已由
paperkb Compiler + regenerate_index 接管；旧 kb_service 生成逻辑已删除）。
本模块只保留文件管理与只读浏览接口。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from paperkb.doi import doi_to_dirname

from ..services import container

router = APIRouter(prefix="/api/kb", tags=["kb"])


@router.get("/tree")
def kb_tree() -> dict:
    return container.get_kb().tree()


@router.get("/file")
def kb_file(path: str = Query(...)) -> dict:
    try:
        return container.get_kb().read_file(path)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


class KbSaveRequest(BaseModel):
    path: str
    content: str


@router.post("/file")
def kb_save(req: KbSaveRequest) -> dict:
    """保存阅读器编辑（U5；记录编辑标记，插件自动生成不再覆盖）。"""
    try:
        result = container.get_kb().write_file(req.path, req.content)
    except ValueError as e:
        raise HTTPException(400, str(e))
    container.get_event_bus().publish("info", "kb", "file_saved",
                                      f"知识库文件已保存: {req.path}",
                                      {"path": req.path})
    return result


class KbPathBody(BaseModel):
    path: str


class KbRenameBody(BaseModel):
    path: str
    new_name: str


@router.post("/dir")
def kb_create_dir(body: KbPathBody) -> dict:
    try:
        return container.get_kb().create_dir(body.path)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/file/new")
def kb_create_file(body: KbSaveRequest) -> dict:
    try:
        return container.get_kb().create_file(body.path, body.content)
    except (ValueError, FileExistsError) as e:
        raise HTTPException(400, str(e))


@router.post("/rename")
def kb_rename(body: KbRenameBody) -> dict:
    try:
        return container.get_kb().rename(body.path, body.new_name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@router.delete("/file")
def kb_delete(path: str = Query(...)) -> dict:
    try:
        return container.get_kb().delete(path)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@router.get("/images")
def kb_images(dir: str = Query(...)) -> dict:
    """列出知识库目录 images/ 下的图片（阅读器图片 tab）。"""
    try:
        return {"images": container.get_kb().list_images(dir)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/image")
def kb_image(path: str = Query(...)) -> Response:
    """知识库图片（二进制，防穿越 + 扩展名白名单）。"""
    target = container.get_kb().image_path(path)
    if target is None:
        raise HTTPException(404, "图片不存在")
    media = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
    }.get(target.suffix.lower(), "application/octet-stream")
    return Response(content=target.read_bytes(), media_type=media,
                    headers={"Cache-Control": "public, max-age=3600"})


@router.get("/pdf")
def kb_pdf(doi: str = Query(...)) -> FileResponse:
    """知识库论文 source.pdf（二进制，pdf.js 阅读器用；只读整文件输出）。

    doi 经 doi_to_dirname 归一化为安全目录名；防路径穿越（`.`/`..` 及 resolved
    后越出 root 均拒绝）。缺 source.pdf → 404。
    """
    raw = (doi or "").strip()
    if not raw:
        raise HTTPException(400, "DOI 不能为空")
    dirname = doi_to_dirname(raw)
    if not dirname or dirname in (".", ".."):
        raise HTTPException(400, "非法 DOI 路径")
    root = container.get_kb().root().resolve()
    target = (root / dirname / "source.pdf").resolve()
    if not target.is_relative_to(root):
        raise HTTPException(400, "非法 DOI 路径")
    if not target.is_file():
        raise HTTPException(404, f"该论文知识库无 source.pdf: {dirname}")
    return FileResponse(target, media_type="application/pdf", filename=f"{dirname}.pdf")


@router.get("/index")
def kb_index() -> dict:
    """_index.md 内容（前端渲染总索引）。"""
    kb = container.get_kb()
    try:
        return {"content": kb.read_file("_index.md")["content"]}
    except FileNotFoundError:
        return {"content": "# 📚 文献知识库索引\n\n（暂无文献）"}


@router.get("/paper/{paper_id}/folder")
def paper_kb_folder(paper_id: int) -> dict:
    """论文 → 知识库文件夹名（阅读器定位，生产级精确匹配）。"""
    store = container.get_store()
    paper = store.get_paper(paper_id)
    if not paper:
        raise HTTPException(404, "论文不存在")
    if not paper.get("doc_json"):
        return {"doi_dir": None}
    kb = container.get_kb()
    try:
        summary = container.get_engine().doc_summary(paper["doc_json"])
    except Exception:  # noqa: BLE001
        return {"doi_dir": None}
    folder = kb.paper_dir(summary.get("doi") or "", paper.get("run_id", ""))
    exists = (kb.root() / folder).exists()
    return {"doi_dir": folder if exists else None}
