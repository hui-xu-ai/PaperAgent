# -*- coding: utf-8 -*-
"""Token 用量 API（V02）：全局/会话统计 → 前端标签栏。"""
from __future__ import annotations

from fastapi import APIRouter

from ..services import container

router = APIRouter(prefix="/api/usage", tags=["usage"])


@router.get("/summary")
def usage_summary(context_prefix: str | None = None) -> dict:
    """token/费用汇总。context_prefix 可选：如 session:1（本会话）或 None（全局）。"""
    usage = container.get_usage()
    data = usage.summary(context_prefix)
    # T3：返回完整价格结构 {"by_provider_model": {...}, "default": {...}}（前端渲染用）
    data["prices"] = container.get_settings_service().get_prices()
    return data


@router.get("/recent")
def usage_recent(limit: int = 50) -> dict:
    """最近 LLM 调用明细（事件面板/审计用）。"""
    return {"calls": container.get_usage().recent(limit)}


@router.post("/reset")
def usage_reset(context_prefix: str | None = None) -> dict:
    """P5 点2：清零 token 用量（context_prefix 可选：只清某会话，缺省清全局）。
    返回清零后的汇总。"""
    return container.get_usage().reset(context_prefix)
