# -*- coding: utf-8 -*-
"""文献阅读日记 API。

- GET  /api/diary/days          → 有活动的日期列表 + 每月聚合（month=可选过滤）
- GET  /api/diary/day?date=     → 某天导入文献 + 用户笔记
- POST /api/diary/note          → 写/覆盖 _diary/<date>.md
- GET  /api/diary/export        → 整本日记导出 markdown（日期倒序）

纯读/写聚合，经 service 薄封装调用 paperkb.diary（与 paperkb.api 改动解耦）。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..services import container

router = APIRouter(prefix="/api/diary", tags=["diary"])


class NoteWriteRequest(BaseModel):
    date: str = Field(..., description="日期，YYYY-MM-DD")
    text: str = Field("", description="该天阅读心得 markdown（覆盖写）")


@router.get("/days")
def diary_days(month: str | None = Query(None, description="可选，YYYY-MM 过滤")) -> dict:
    """所有有活动的日期（导入/纳入/笔记任一）＋每月聚合（月历圆点）。"""
    try:
        return container.get_kbapi().diary_days(month=month)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"日记聚合失败: {e}")


@router.get("/day")
def diary_day(date: str = Query(..., description="日期，YYYY-MM-DD")) -> dict:
    """某天详情：导入文献列表（每条 title/doi/status/parsed/translated/in_kb）+ 用户笔记。"""
    try:
        return container.get_kbapi().diary_day(date)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"日记读取失败: {e}")


@router.post("/note")
def diary_note_write(req: NoteWriteRequest) -> dict:
    """写/覆盖 _diary/<date>.md（用户当天阅读心得）。"""
    try:
        return container.get_kbapi().diary_note_write(req.date, req.text)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"笔记写入失败: {e}")


@router.get("/export")
def diary_export() -> dict:
    """整本日记导出 markdown（按日期倒序拼接）。"""
    return container.get_kbapi().diary_export()
