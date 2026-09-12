# -*- coding: utf-8 -*-
"""单实例闸门（2026-09-12 用户实测报障回归）。

**报障**：双击 `PaperAgent.exe` 没反应，去任务管理器杀掉后台才能运行。
**根因**：端口 8900 上只要有实例应答 `/api/health`，新进程就认为"已在运行"并**静默退出**；
若占端口的是**开发/后台模式**实例（无窗口、无 `/api/desktop/show` ⇒ 404），用户看不到任何反馈。
**修法**：唤不起窗口时**必须可见**（打包版弹窗 + 日志），并给出可操作的三条退出指引。
"""
from __future__ import annotations

import sys

import pytest

import app.main as m
from app import desktop

ALIVE_HEADLESS = {"status": "ok", "desktop": False, "pid": 5144,
                  "app_data_dir": "D:/Python/DeepSeek/PaperAgent"}
ALIVE_DESKTOP = {"status": "ok", "desktop": True, "pid": 999, "app_data_dir": "D:/x"}


@pytest.fixture
def gate(monkeypatch):
    """桩：running_instance / signal_instance_ex / fatal_dialog，并记录弹窗内容。"""
    seen: dict = {}

    def _patch(alive, show_resp):
        monkeypatch.setattr(desktop, "running_instance", lambda url: alive)
        monkeypatch.setattr(desktop, "signal_instance_ex",
                            lambda url, action: show_resp)
        monkeypatch.setattr(desktop, "fatal_dialog",
                            lambda msg: seen.setdefault("msg", msg))
        return seen

    return _patch


def test_port_free_starts_normally(gate):
    gate(None, None)
    assert m._handle_cli_control("http://127.0.0.1:8900") is False


def test_headless_instance_must_be_visible(gate, monkeypatch):
    """占端口的是开发/后台实例（无窗口）⇒ 打包版必须弹窗说明，绝不静默退出。"""
    seen = gate(ALIVE_HEADLESS, None)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert m._handle_cli_control("http://127.0.0.1:8900") is True
    msg = seen.get("msg", "")
    assert "已经在运行" in msg
    assert "后台/开发模式" in msg
    assert "--quit" in msg and "5144" in msg, "必须给出可操作的退出指引 + 占用者 PID"


def test_desktop_instance_shows_window_without_dialog(gate, monkeypatch):
    """占端口的是桌面版（能唤起窗口）⇒ 唤起即可，不必打扰用户。"""
    seen = gate(ALIVE_DESKTOP, {"status": "ok", "window": True})
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert m._handle_cli_control("http://127.0.0.1:8900") is True
    assert "msg" not in seen, "能唤起窗口时不该弹窗"


def test_desktop_instance_with_dead_shell_still_visible(gate, monkeypatch):
    """桌面版但窗口壳已死（window=false）⇒ 同样要弹窗（否则又是"没反应"）。"""
    seen = gate(ALIVE_DESKTOP, {"status": "ok", "window": False})
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert m._handle_cli_control("http://127.0.0.1:8900") is True
    assert "已经在运行" in seen.get("msg", "")


def test_health_exposes_instance_identity(monkeypatch):
    """`/api/health` 必须暴露 desktop/pid/app_data_dir，闸门才能判断"能不能唤起"。"""
    payload = m.health().body.decode("utf-8")
    import json

    d = json.loads(payload)
    for key in ("desktop", "pid", "app_data_dir"):
        assert key in d, f"/api/health 缺 {key}（单实例闸门与排障都依赖它）"
