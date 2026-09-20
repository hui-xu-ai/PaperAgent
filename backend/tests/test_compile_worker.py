# -*- coding: utf-8 -*-
"""A2 单测：后台编译 worker（fake 执行器/status/value/queue，不真调 LLM）。

覆盖：L1→L2、L2→L3 自动升级、价值分 L1 不升级、失败项不升级、
队列空/LLM 不可用空转、start/stop 循环吞队列。
"""
from __future__ import annotations

import time

from app.services.compile_worker import CompileWorker


class FakeCompile:
    """可注入的编译服务 fakes：队列/价值分/入队记录，可编程返回。"""

    def __init__(self, llm_ready: bool = True, results: list[dict] | None = None):
        self.llm_ready_flag = llm_ready
        self.queued: list[dict] = []          # status("queued") 返回值
        self.values: dict[str, dict] = {}     # doi -> 价值分 dict
        self.enqueued: list[tuple[str, str]] = []   # queue_fn 记录
        self.executor_calls = 0
        self._results = list(results or [])   # 可预设 executor 返回

    # --- 注入点 ---
    def status(self, status: str):
        return list(self.queued) if status == "queued" else []

    def value(self, doi: str):
        return self.values.get(doi)

    def queue(self, doi: str, level: str):
        self.enqueued.append((doi, level))
        return {"status": "queued", "doi": doi, "level": level}

    def executor(self):
        self.executor_calls += 1
        if self._results:
            return [self._results.pop(0)]
        if not self.queued:
            return []
        item = self.queued.pop(0)
        return [{"doi": item["paper_doi"], "level": item["level"],
                 "result": {"status": "done"}}]

    def ready(self):
        return self.llm_ready_flag

    # --- 测试辅助 ---
    def add_queued(self, doi: str, level: str) -> None:
        self.queued.append({"paper_doi": doi, "level": level})

    def make_worker(self, **kw) -> CompileWorker:
        kw.setdefault("executor", self.executor)
        kw.setdefault("status_fn", self.status)
        kw.setdefault("value_fn", self.value)
        kw.setdefault("queue_fn", self.queue)
        kw.setdefault("llm_ready", self.ready)
        return CompileWorker(**kw)


def _worker(fake: FakeCompile, **kw) -> CompileWorker:
    return fake.make_worker(**kw)


# ---------------------------------------------------------------- 自动升级

def test_l1_done_upgrades_to_l2_by_value():
    fake = FakeCompile()
    fake.add_queued("10.a/1", "L1")
    fake.values["10.a/1"] = {"level": "L2", "score": 3.0}
    w = _worker(fake)
    r = w.process_one()
    assert r["doi"] == "10.a/1" and r["level"] == "L1"
    assert fake.enqueued == [("10.a/1", "L2")]   # L1 完成且价值分 L2 → 入队 L2


def test_l1_done_upgrades_when_value_l2():
    """L1 完成且价值分达到 L2 门槛 → 入队 L2（深度 wiki）。"""
    fake = FakeCompile()
    fake.add_queued("10.a/2", "L1")
    fake.values["10.a/2"] = {"level": "L2", "score": 4.5}
    r = _worker(fake).process_one()
    assert r["level"] == "L1"
    assert fake.enqueued == [("10.a/2", "L2")]


def test_no_upgrade_when_l2_is_final():
    """L2 是最高级，不再升级。"""
    fake = FakeCompile()
    fake.add_queued("10.a/3", "L2")
    fake.values["10.a/3"] = {"level": "L2", "score": 4.5}
    r = _worker(fake).process_one()
    assert r["level"] == "L2"
    assert fake.enqueued == []   # L2 是最终级，不升级


def test_no_upgrade_when_value_level_l1():
    fake = FakeCompile()
    fake.add_queued("10.a/4", "L1")
    fake.values["10.a/4"] = {"level": "L1", "score": 1.0}
    r = _worker(fake).process_one()
    assert r["level"] == "L1"
    assert fake.enqueued == []                   # 价值分 L1 → 停在 L1


def test_no_upgrade_when_value_level_l1_only():
    """价值分只达到 L1 → 停在 L1。"""
    fake = FakeCompile()
    fake.add_queued("10.a/5", "L1")
    fake.values["10.a/5"] = {"level": "L1", "score": 2.0}
    _worker(fake).process_one()
    assert fake.enqueued == []   # 价值分 L1 → 停在 L1


def test_failed_item_no_upgrade():
    fake = FakeCompile(results=[{"doi": "10.a/6", "level": "L1",
                                 "error": "kb 中无 document.json"}])
    fake.add_queued("10.a/6", "L1")
    r = _worker(fake).process_one()
    assert "error" in r
    assert fake.enqueued == []                   # 失败不升级、不无限重试


# ---------------------------------------------------------------- 空转

def test_empty_queue_idles_without_executor_call():
    fake = FakeCompile()
    w = _worker(fake)
    assert w.process_one() is None
    assert fake.executor_calls == 0


def test_llm_not_ready_idles():
    fake = FakeCompile(llm_ready=False)
    fake.add_queued("10.a/7", "L1")
    w = _worker(fake)
    assert w.process_one() is None
    assert fake.executor_calls == 0              # LLM 不可用 → 不处理、不置 failed


def test_executor_no_result_returns_none():
    fake = FakeCompile()
    fake.add_queued("10.a/8", "L1")
    w = CompileWorker(executor=lambda: [], status_fn=fake.status,
                      value_fn=fake.value, queue_fn=fake.queue,
                      llm_ready=fake.ready)
    assert w.process_one() is None               # executor 空返回（并发被处理）


# ---------------------------------------------------------------- 循环/生命周期

def test_start_stop_loop_drains_queue_and_upgrades():
    fake = FakeCompile()
    fake.add_queued("10.b/1", "L1")
    fake.add_queued("10.b/2", "L1")
    fake.values["10.b/1"] = {"level": "L2", "score": 3.0}
    fake.values["10.b/2"] = {"level": "L1", "score": 1.0}
    w = _worker(fake, poll_interval=0.5)
    w.start()
    assert w.running()
    assert w._thread.daemon                        # daemon=True
    deadline = time.time() + 10
    while fake.executor_calls < 2 and time.time() < deadline:
        time.sleep(0.05)
    w.stop()
    assert fake.executor_calls == 2                # 两个 queued 都被处理
    assert fake.enqueued == [("10.b/1", "L2")]     # 仅价值分 L2 的那篇升级
    assert not w.running()
    # 幂等 start：stop 后不复活
    w.start()
    w.stop()


def test_loop_idles_on_empty_queue():
    fake = FakeCompile()
    w = _worker(fake, poll_interval=0.5)
    w.start()
    time.sleep(0.8)                                # 空队列 → 应空转 sleep，不忙转
    w.stop()
    assert fake.executor_calls == 0


def test_loop_survives_executor_exception():
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        fake.queued.clear()
        return []

    fake = FakeCompile()
    fake.add_queued("10.c/1", "L1")
    w = CompileWorker(executor=boom, status_fn=fake.status, value_fn=fake.value,
                      queue_fn=fake.queue, llm_ready=fake.ready, poll_interval=0.5)
    w.start()
    time.sleep(0.8)                                # 第一轮抛异常 → 线程不退出
    w.stop()
    assert calls["n"] >= 2                         # 异常后仍继续循环
