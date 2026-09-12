# -*- coding: utf-8 -*-
"""优雅关停单测（工程化 P0，2026-09-12）。

背景：旧实现 `os._exit(0)` 硬杀进程——正在跑的解析/翻译产物会半写（SQLite 事务、
markdown 写一半）。现在改为：uvicorn 停止接新请求 → **有界宽限窗**等在跑任务收尾
（`GRACEFUL_EXIT_GRACE`，默认 30s）→ 超时才硬退；残留任务由下次启动的
`_recover_orphans` 重新入队。

本文件锁定三条性质（不真跑解析，用假 TaskManager 控制"在跑任务数"）：
1. 无在跑任务 → 立即返回（不白等 30s）；
2. 有在跑任务 → 等到宽限窗用尽才返回（不会提前掐断）；
3. 关停请求 → `server.should_exit=True`（而不是直接 os._exit）。
"""
from __future__ import annotations

import time

import pytest


class _FakeTasks:
    def __init__(self, n: int) -> None:
        self.n = n

    def active_tasks(self) -> int:
        return self.n


@pytest.fixture()
def main_mod(monkeypatch):
    import app.main as m
    from app.services import container

    monkeypatch.setattr(container, "get_tasks", lambda: _FakeTasks(0))
    return m


def test_wait_returns_immediately_when_idle(main_mod, monkeypatch):
    monkeypatch.setattr(main_mod, "GRACEFUL_EXIT_GRACE", 30.0)
    t0 = time.time()
    main_mod._graceful_wait()          # noqa: SLF001
    assert time.time() - t0 < 1.0, "没有在跑任务时不该白等宽限窗"


def test_wait_blocks_until_grace_elapsed(main_mod, monkeypatch):
    from app.services import container

    monkeypatch.setattr(container, "get_tasks", lambda: _FakeTasks(1))
    monkeypatch.setattr(main_mod, "GRACEFUL_EXIT_GRACE", 1.5)
    t0 = time.time()
    main_mod._graceful_wait()          # noqa: SLF001
    elapsed = time.time() - t0
    assert elapsed >= 1.4, f"有在跑任务时必须在宽限窗内等待（实测 {elapsed:.2f}s）"
    assert elapsed < 5.0, "宽限窗必须有上界，不能无限等"


def test_request_sets_should_exit_not_hard_kill(main_mod, monkeypatch):
    class _Srv:
        should_exit = False

    srv = _Srv()
    monkeypatch.setattr(main_mod, "_server_ref", srv)
    main_mod._exit_requested.clear()
    ok = main_mod._request_graceful_exit("test")   # noqa: SLF001
    assert ok is True and srv.should_exit is True, "关停应交给 uvicorn（优雅），不是 os._exit"
    assert main_mod._exit_requested.is_set() is True


def test_request_without_server_returns_false(main_mod, monkeypatch):
    """没有 server 引用时返回 False，让调用方知道需要兜底（不静默"以为已关停"）。"""
    monkeypatch.setattr(main_mod, "_server_ref", None)
    main_mod._exit_requested.clear()
    assert main_mod._request_graceful_exit("test") is False   # noqa: SLF001
    main_mod._exit_requested.clear()


def test_file_logging_is_idempotent(main_mod):
    """日志落盘（工程化 P0：此前日志从不落盘，用户报障无现场）。

    只验证幂等性（重复调用不重复挂 handler）——落盘行为已在真实链路实测
    （起后端后 `logs/paperagent.log` 出现启动日志）。
    """
    import logging

    p1 = main_mod._setup_file_logging()      # noqa: SLF001
    p2 = main_mod._setup_file_logging()      # noqa: SLF001
    assert p1 == p2
    if not p1:
        return                                  # 环境不允许建 logs/ 时跳过
    handlers = [h for h in logging.getLogger().handlers
                if isinstance(h, logging.handlers.RotatingFileHandler)
                and str(getattr(h, "baseFilename", "")).endswith("paperagent.log")]
    assert len(handlers) == 1, "重复调用不得重复挂 handler"
