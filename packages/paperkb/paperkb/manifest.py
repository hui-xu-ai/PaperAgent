# -*- coding: utf-8 -*-
"""数据资产版本戳 `data/manifest.json`（见 `docs/VERSIONING.md` §5/§7）。

作用：把"这份数据是什么格式、被哪个应用版本写过"记在数据根里，让升级能
**先校验再动手**：
  · 数据格式 **新于** 代码 → 立刻失败并给出明确提示（绝不猜测式部分读取）；
  · 数据格式 **旧于** 代码 → 返回 needs_migration，由迁移层处理（不是读时兼容）；
  · 缺 manifest（历史数据，早于本机制）→ 按当前格式**认领**并写明 `adopted=true`。

清单是**极小资产**：一个 JSON，不含任何业务状态（业务状态在五个库里）。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

MANIFEST_NAME = "manifest.json"

__all__ = ["MANIFEST_NAME", "DataFormatError", "manifest_path", "read_manifest",
           "ensure_manifest", "write_manifest"]


class DataFormatError(RuntimeError):
    """数据由更新版本写入（或格式不受支持）——必须 fail-fast，不得继续读取。"""


def manifest_path(roots) -> Path:
    return Path(roots.data_dir) / MANIFEST_NAME


def read_manifest(roots) -> dict | None:
    p = manifest_path(roots)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _db_state(roots) -> dict:
    return {
        "system": Path(roots.system_db).exists(),
        "chat": Path(roots.chat_db).exists(),
        "diary": Path(roots.diary_db).exists(),
        "biblio": Path(roots.biblio_db).exists(),
        "reference": Path(roots.reference_db).exists(),
    }


def write_manifest(roots, data: dict) -> Path:
    p = manifest_path(roots)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = dict(data)
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def has_existing_data(roots) -> bool:
    """数据根里是否已有实体数据（任一新库文件存在 → 说明是"历史数据被认领"，不是新装）"""
    return any(Path(p).exists() for p in
               (roots.system_db, roots.chat_db, roots.biblio_db, roots.reference_db))


def adopt_data_format(roots, *, data_format: int, min_readable: int) -> int:
    """缺 manifest 时该按什么格式认领：

    · 已有库文件（历史数据，早于 manifest 机制）→ 按**最旧可读格式**认领，
      让迁移层把它升上来（否则会被误当"已最新"而跳过迁移）；
    · 全新安装（没有库文件）→ 按当前格式。
    """
    return min_readable if has_existing_data(roots) else data_format


def refresh_dbs(roots) -> dict:
    """建库之后刷新 `manifest.dbs`（`ensure_manifest` 早于 Store 建表，那时状态必然全 false）。"""
    cur = read_manifest(roots) or {}
    if not cur:
        return cur
    cur["dbs"] = _db_state(roots)
    write_manifest(roots, cur)
    return cur


def ensure_manifest(roots, *, app_version: str, data_format: int, layout: str = "v1",
                    min_readable: int | None = None, adopt: bool = True) -> dict:
    """读/建/校验 `data/manifest.json`，返回清单 dict（附 `needs_migration` 标记）。

    抛 `DataFormatError`：数据格式高于代码（降级场景），或低于可自动迁移窗口。

    `min_readable` 缺省取 `paperkb.version.MIN_READABLE_DATA_FORMAT`
    （**不是** `data_format`——否则每次升格式都会把上一版数据误判为"太旧不可读"，
    2026-09-12 由黄金夹具测试抓到：v1 数据被 v2 代码拒绝启动）。
    """
    cur = read_manifest(roots)
    if min_readable is None:
        from .version import MIN_READABLE_DATA_FORMAT as _floor

        floor = int(_floor)
    else:
        floor = int(min_readable)
    if cur is None:
        if not adopt:
            raise DataFormatError("缺少 data/manifest.json（未认领的数据目录）")
        adopted_df = adopt_data_format(roots, data_format=data_format,
                                      min_readable=floor)
        cur = {"layout": layout, "data_format": adopted_df,
               "created_at": datetime.now().isoformat(timespec="seconds"),
               "adopted": True,
               "note": "首次写入（已有库文件 ⇒ 按最旧可读格式认领，交由迁移层升级；"
                       "无库文件 ⇒ 按当前格式）"}
    df = int(cur.get("data_format") or 0)
    if df > data_format:
        raise DataFormatError(
            f"数据格式 v{df} 高于本程序支持的 v{data_format}（数据由更新版本写过）。"
            f"请升级程序，或改用只读工具查看；不要用旧版本继续写入。")
    if df < floor:
        raise DataFormatError(
            f"数据格式 v{df} 低于可自动迁移窗口 v{floor}；"
            f"请先运行离线升级工具 tools/upgrade_data.py。")
    cur["needs_migration"] = df < data_format
    cur["app_version"] = app_version
    cur["dbs"] = _db_state(roots)
    write_manifest(roots, cur)
    return cur
