# -*- coding: utf-8 -*-
"""编译完成事件（2026-09-12 修复）：前端「编译结果标签页」自动刷新的**信号源**。

用户实测报障：编译完成后阅读器不出现 `_note.md` / `_details.md` 标签页。
根因之一 = **编译完成不发任何事件**（后台队列走 CompileWorker、手动编译走同步 API，两条路都不发）
⇒ 前端只能等 15s 轮询，且文件集缓存不失效 ⇒ 标签页永不出现（本文件守住这两条信号）。
"""
from __future__ import annotations


class _Bus:
    def __init__(self):
        self.events: list[tuple] = []

    def publish(self, level, source, category, message, data=None):
        self.events.append((category, data or {}))
        return {}


class _Kb:
    def __init__(self, result):
        self._result = result

    def compile_now(self, doi, level, force=False):
        return dict(self._result, doi=doi, level=level)

    def compile_queue(self, doi, level=None):
        return {"status": "queued", "doi": doi, "level": level or "L1"}


class TestWorkerNotifies:
    """后台队列路径：每处理完一项必须发通知（否则前端永远不知道编译完成了）。"""

    def _worker(self, notify):
        from app.services.compile_worker import CompileWorker

        return CompileWorker(
            executor=lambda: [{"doi": "10.1/a", "level": "L1", "result": {"status": "done"}}],
            status_fn=lambda s: [{"doi": "10.1/a", "level": "L1"}],
            value_fn=lambda doi: {"level": "L1"},
            queue_fn=lambda doi, lv: {},
            llm_ready=lambda: True,
            notify=notify,
        )

    def test_process_one_calls_notify(self):
        seen: list = []
        out = self._worker(seen.append).process_one()
        assert out and out["doi"] == "10.1/a"
        assert seen and seen[0]["doi"] == "10.1/a", "编译项收尾必须通知（前端靠它刷新标签页）"

    def test_notify_failure_does_not_break_worker(self):
        def _boom(_item):
            raise RuntimeError("通知炸了")

        out = self._worker(_boom).process_one()
        assert out and out["doi"] == "10.1/a", "通知失败不得影响 worker 主流程"


class TestApiPublishesCompileEvents:
    """同步 API 路径：compile/now 与 compile/queue 都必须发事件。"""

    def _patch(self, monkeypatch, result):
        from app.api import kbmeta
        from app.services import container

        bus = _Bus()
        monkeypatch.setattr(container, "get_event_bus", lambda: bus)
        monkeypatch.setattr(container, "get_kbapi", lambda: _Kb(result))
        return kbmeta, bus

    def test_compile_now_publishes_compile_done(self, monkeypatch):
        kbmeta, bus = self._patch(monkeypatch, {"status": "done", "error": ""})
        r = kbmeta.compile_now(kbmeta.CompileRequest(doi="10.1/a", level="L2", force=True))
        assert r["status"] == "done"
        assert bus.events and bus.events[0][0] == "compile_done", bus.events
        assert bus.events[0][1]["level"] == "L2"

    def test_compile_now_error_publishes_warning(self, monkeypatch):
        kbmeta, bus = self._patch(monkeypatch, {"status": "failed", "error": "boom"})
        kbmeta.compile_now(kbmeta.CompileRequest(doi="10.1/a", level="L2", force=True))
        assert bus.events[0][0] == "compile_done"

    def test_compile_queue_publishes_queue_event(self, monkeypatch):
        kbmeta, bus = self._patch(monkeypatch, {"status": "queued"})
        kbmeta.compile_queue(kbmeta.CompileQueueRequest(doi="10.1/a", level="L2"))
        assert bus.events and bus.events[0][0] == "compile_queue", bus.events
