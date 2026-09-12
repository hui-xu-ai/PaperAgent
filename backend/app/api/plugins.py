# -*- coding: utf-8 -*-
"""插件 API（V05/U10）：列表 / 启用禁用 / 安装 Skill（zip 上传）。"""
from __future__ import annotations

import io
import logging
import posixpath
import re
import zipfile

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from ..services import container
from ..services.plugin_registry import USER_PLUGINS_DIR

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/plugins", tags=["plugins"])


class ToggleModel(BaseModel):
    enabled: bool


@router.get("")
def list_plugins() -> dict:
    return {"plugins": container.get_plugin_registry().list_plugins()}


@router.post("/{plugin_id}/toggle")
def toggle_plugin(plugin_id: str, body: ToggleModel) -> dict:
    try:
        meta = container.get_plugin_registry().set_enabled(plugin_id, body.enabled)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "plugin": meta}


def _safe_zip_members(names: list[str]) -> list[str]:
    """Zip Slip 防护：拒绝绝对路径与 .. 穿越，规范化返回安全成员列表。"""
    safe = []
    for n in names:
        norm = posixpath.normpath(n.replace("\\", "/"))
        if norm.startswith(("..", "/")) or norm == ".." or ":" in norm:
            raise HTTPException(400, f"压缩包含非法路径: {n}")
        safe.append(norm)
    return safe


@router.post("/install")
async def install_plugin(file: UploadFile = File(...)) -> dict:
    """安装 Skill 插件（zip 内须含 plugin.yaml + plugin.py）。

    解压到用户插件目录（APP_DATA_DIR/plugins/）→ 重扫注册表 → 新插件加载。
    """
    name = file.filename or "plugin.zip"
    if not name.lower().endswith(".zip"):
        raise HTTPException(400, "仅支持 .zip 格式的 Skill 包")
    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(400, "压缩包过大（>10MB）")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        names = _safe_zip_members(zf.namelist())
    except zipfile.BadZipFile:
        raise HTTPException(400, "不是有效的 zip 文件")

    # 插件名 = zip 文件名（去扩展名，规范化）
    plugin_dir = re.sub(r"[^\w.-]", "_", name[:-4]) or "plugin"
    dest = USER_PLUGINS_DIR / plugin_dir
    dest.mkdir(parents=True, exist_ok=True)
    for n in names:
        if n.endswith("/"):
            continue
        target = dest / n
        try:
            with zf.open(n) as src, open(target, "wb") as out:
                out.write(src.read())
        except Exception as e:  # noqa: BLE001
            logger.warning("解压失败 %s: %s", n, e)
            raise HTTPException(400, f"解压失败: {n}")

    # 重扫并校验新插件
    reg = container.get_plugin_registry()
    result = reg.rescan()
    loaded = reg.get_plugin(plugin_dir)
    if loaded is None:
        raise HTTPException(400,
                            "安装完成但插件未加载（需包含 plugin.yaml + plugin.py 且格式正确）")
    container.get_event_bus().publish(
        "info", "plugin", "plugin_installed",
        f"Skill 已安装并加载: {loaded.name} v{loaded.version}",
        {"plugin_id": loaded.id})
    return {"ok": True, "plugin": loaded.to_dict(), "total": result["count"]}
