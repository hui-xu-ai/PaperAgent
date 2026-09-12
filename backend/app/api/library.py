# -*- coding: utf-8 -*-
"""规范库 API（T04）：浏览/读取 library/ 内全部解析与翻译产物。

与 knowledge_base 互补：kb 是精简纳入层（笔记+纳入文件），library 是完整产物树
（document.json / 全部 md 变体 / images / audit / intermediate），阅读器可双来源浏览。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from ..services import container

router = APIRouter(prefix="/api/library", tags=["library"])


def _root():
    from pathlib import Path
    return Path(container.get_settings().engine_work_root).resolve()


def _safe(rel_path: str):
    root = _root()
    target = (root / rel_path).resolve()
    if not target.is_relative_to(root):
        raise HTTPException(400, "非法路径")
    return target


@router.get("/tree")
def library_tree() -> dict:
    """规范库目录树：<DOI>/ 下全部文件（含子目录中间产物）。"""
    from pathlib import Path
    root = _root()
    folders = []
    if root.exists():
        for d in sorted(root.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            files = []
            for f in sorted(d.rglob("*")):
                if f.is_file():
                    files.append({"path": f.relative_to(root).as_posix(),
                                  "name": f.name, "size": f.stat().st_size})
            folders.append({"doi_dir": d.name, "files": files})
    return {"root": str(root), "folders": folders}


@router.get("/file")
def library_file(path: str = Query(...)) -> dict:
    """读取规范库内文本文件（防目录穿越）。"""
    target = _safe(path)
    if not target.is_file():
        raise HTTPException(404, "文件不存在")
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "二进制文件请用图片/下载接口")
    return {"path": str(target.relative_to(_root())), "content": content}


@router.get("/image")
def library_image(path: str = Query(...)) -> Response:
    """规范库内图片（二进制，扩展名白名单 + 防穿越）。"""
    target = _safe(path)
    if not target.is_file() or target.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}:
        raise HTTPException(404, "图片不存在")
    media = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
    }.get(target.suffix.lower(), "application/octet-stream")
    return Response(content=target.read_bytes(), media_type=media,
                    headers={"Cache-Control": "public, max-age=3600"})


@router.get("/download")
def library_download(path: str = Query(...)):
    """下载规范库内文件（文本/二进制均可）。"""
    from fastapi.responses import FileResponse
    target = _safe(path)
    if not target.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(target, filename=target.name)
