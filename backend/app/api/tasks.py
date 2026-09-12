# -*- coding: utf-8 -*-
"""任务 API：翻译流水线 SSE 进度 + P12F 复核门控队列。"""
from __future__ import annotations

from collections.abc import Iterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ..services import container

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("/{paper_id}/stream")
def task_stream(paper_id: int) -> StreamingResponse:
    """SSE 进度流：event=state（progress/message/state），结束为 done/failed。"""
    store = container.get_store()
    if not store.get_paper(paper_id):
        raise HTTPException(404, "论文不存在")

    def gen() -> Iterator[str]:
        yield from container.get_tasks().task_stream(paper_id)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.post("/{paper_id}/cancel")
def cancel_task(paper_id: int) -> dict:
    """P15：取消当前解析任务（官方云队列等待期间可随时取消）——
    置取消标志，解析在下一个取消检查点终止（不降级、不重试）。"""
    store = container.get_store()
    if not store.get_paper(paper_id):
        raise HTTPException(404, "论文不存在")
    return {"status": container.get_tasks().cancel_current(), "paper_id": paper_id}


# ---------------------------------------------------------- P12F 复核门控
@router.get("/review-queue")
def review_queue() -> dict:
    """待复核队列汇总（任务面板汇总条）：
    waiting=挂起文献（含待处理数）、ready_count=已复核完可翻译数、pending_total。"""
    return container.get_tasks().review_queue()


@router.post("/translate-ready")
def translate_ready(force: bool = False) -> dict:
    """确认按钮：对已复核完（pending==0）的挂起文献**串行**启动翻译；
    force=True = 批量跳过审核（默认由设置开关控制，前端需二次确认）。"""
    return container.get_tasks().translate_ready_all(force=force)
