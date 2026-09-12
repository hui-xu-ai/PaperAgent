# -*- coding: utf-8 -*-
"""事件 API（V01）：SSE 实时推送 + 历史恢复（信息面板数据源）。"""
from __future__ import annotations

import json
import queue
from collections.abc import Iterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from ..services import container

router = APIRouter(prefix="/api/events", tags=["events"])


@router.get("/stream")
def event_stream() -> StreamingResponse:
    """SSE 事件流：先发历史（最近 100 条），再实时推送；15s 心跳保活。"""

    def gen() -> Iterator[str]:
        bus = container.get_event_bus()
        q = bus.subscribe()
        try:
            for ev in bus.history(100):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            while True:
                try:
                    ev = q.get(timeout=15)
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"  # 心跳（注释行，SSE 规范）
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
