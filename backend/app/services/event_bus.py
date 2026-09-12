# -*- coding: utf-8 -*-
"""全局事件总线（V01）：任务/引擎/LLM/插件/错误事件发布 + SSE 推送。

- 内存环形缓冲（页面刷新后可拉历史）
- SSE 订阅者队列（慢消费者不阻塞发布者）
- 事件：{ts, level, source, category, message, data}
  级别：info / warning / error（error 触发前端红色高亮）
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)

LEVELS = ("info", "warning", "error")


class EventBus:
    def __init__(self, max_buffer: int = 500, max_sub_queues: int = 8):
        self._buffer: deque[dict] = deque(maxlen=max_buffer)
        self._subs: list[queue.Queue] = []
        self._listeners: dict[str, list] = {}  # category -> [callback(event)]
        self._lock = threading.Lock()
        self._max_sub_queues = max_sub_queues

    # ---------------------------------------------------------- 发布
    def publish(self, level: str, source: str, category: str,
                message: str, data: dict | None = None) -> dict:
        """发布事件（线程安全）。返回事件对象。"""
        if level not in LEVELS:
            level = "info"
        event = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "level": level,
            "source": source,
            "category": category,
            "message": str(message),
            "data": data or {},
        }
        with self._lock:
            self._buffer.append(event)
            for q in list(self._subs):
                try:
                    q.put_nowait(event)
                except queue.Full:
                    pass  # 慢消费者：丢事件不阻塞（订阅方刷新历史兜底）
            listeners = list(self._listeners.get(category, ()))
        for cb in listeners:  # 监听器在锁外执行（避免回调死锁）
            try:
                cb(event)
            except Exception:  # noqa: BLE001 - 监听器异常不影响总线
                logger.exception("事件监听器异常 category=%s", category)
        if level == "error":
            logger.error("[%s/%s] %s", source, category, message)
        else:
            logger.info("[%s/%s] %s", source, category, message)
        return event

    # ---------------------------------------------------------- 监听器（插件钩子）
    def on(self, category: str, callback) -> None:
        """注册事件监听器（插件生命周期解耦触发）。"""
        with self._lock:
            self._listeners.setdefault(category, []).append(callback)

    # ---------------------------------------------------------- 订阅
    def subscribe(self) -> queue.Queue:
        """返回事件队列（SSE 消费）。客户端断开需 unsubscribe。"""
        with self._lock:
            if len(self._subs) >= self._max_sub_queues:
                raise RuntimeError("事件订阅者过多")
            q: queue.Queue = queue.Queue(maxsize=500)
            self._subs.append(q)
            return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def history(self, limit: int = 200) -> list[dict]:
        """最近 N 条历史事件（页面刷新/首次连接恢复用）。"""
        with self._lock:
            return list(self._buffer)[-limit:]
