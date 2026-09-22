# -*- coding: utf-8 -*-
"""批次上限「安全上限」探测服务（2026-09-22 用户要求：点一下自动测、按安全系数给建议值）。

锁三件事：
1. **选模型顺序** = 线上翻译路由同序：激活的翻译专用模型 → 主模型 → 都没有则明确报错；
2. **后台线程 + 进度**：start() 立刻返回、不阻塞请求；进度可轮询；结果落 settings；
3. **只给建议**：探测服务**不写** translate_batch_chars（写不写由用户的「写入」按钮决定）。
"""
from __future__ import annotations

import time

from app.services import container
from app.services import llm_service
from app.services.translate_probe_service import TranslateProbeService


class _Settings:
    def __init__(self, pool=None, main=None):
        self._pool, self._main = pool or [], main
        self.saved: list[dict] = []

    def get_enabled_translation_providers(self, masked: bool = True):
        return list(self._pool)

    def get_active_provider(self, masked: bool = True):
        return self._main

    def save_translate_probe_result(self, result: dict) -> None:
        self.saved.append(result)


class _FakeAI:
    model = "fake-model"
    max_tokens = 64000
    provider_name = "假供应商"


def _wire(monkeypatch, settings, kbapi_result=None, kbapi_error=None):
    """把 container / llm_service / kbapi 三处外部依赖换成假的。"""
    monkeypatch.setattr(container, "get_settings_service", lambda: settings)
    monkeypatch.setattr(llm_service, "build_ai", lambda p, g, **kw: _FakeAI())
    monkeypatch.setattr(container, "get_guard", lambda: None)

    class _Kb:
        def probe_translate_batch(self, llm, progress_cb=None):
            if kbapi_error:
                raise kbapi_error
            if progress_cb:
                progress_cb(1, 5, "正在测 3000 字符档（3 段）…")
            return dict(kbapi_result or {"model": llm.model, "recommended": 3600,
                                        "highest_pass": 6000, "tested": []})

    monkeypatch.setattr(container, "get_kbapi", lambda: _Kb())
    return None


def _wait(svc, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = svc.progress()
        if st["status"] in ("done", "error"):
            return st
        time.sleep(0.02)
    raise AssertionError("探测未在 %.1fs 内结束：%s" % (timeout, svc.progress()))


# ---------------------------------------------------------------- 选模型
def test_prefers_active_translation_model(monkeypatch):
    s = _Settings(pool=[{"id": "t1", "name": "翻译专用", "api_key": "k",
                         "base_url": "u", "model": "m"}],
                  main={"id": "main", "api_key": "k2", "base_url": "u2", "model": "m2"})
    _wire(monkeypatch, s)
    svc = TranslateProbeService()
    svc._run()
    st = svc.progress()
    assert st["status"] == "done" and st["via"] == "翻译专用模型"
    assert st["model"] == "fake-model"


def test_falls_back_to_main_model(monkeypatch):
    s = _Settings(pool=[], main={"id": "main", "name": "主", "api_key": "k",
                                "base_url": "u", "model": "m"})
    _wire(monkeypatch, s)
    svc = TranslateProbeService()
    svc._run()
    assert svc.progress()["via"] == "主模型"


def test_no_model_is_a_clear_error(monkeypatch):
    _wire(monkeypatch, _Settings())
    svc = TranslateProbeService()
    svc._run()
    st = svc.progress()
    assert st["status"] == "error"
    assert "没有可用的模型" in st["error"]


# ---------------------------------------------------------------- 后台 + 进度 + 落盘
def test_start_returns_immediately_and_reports_progress(monkeypatch):
    s = _Settings(pool=[{"id": "t1", "name": "翻译专用", "api_key": "k", "base_url": "u",
                         "model": "m"}])
    _wire(monkeypatch, s)
    svc = TranslateProbeService()
    started = svc.start()
    assert started["ok"] is True and started["task_id"]
    st = _wait(svc)
    assert st["status"] == "done" and st["result"]["recommended"] == 3600
    assert st["result"]["via"] == "翻译专用模型"
    assert st["result"]["provider_name"] == "翻译专用"      # 用供应商显示名，不用模型名
    assert s.saved and s.saved[0]["recommended"] == 3600, "结果必须落 settings（界面回显）"


def test_second_start_while_running_is_not_restarted(monkeypatch):
    s = _Settings(pool=[{"id": "t1", "name": "n", "api_key": "k", "base_url": "u",
                         "model": "m"}])
    _wire(monkeypatch, s)
    svc = TranslateProbeService()
    svc._set(status="running", task_id="probe_x")
    again = svc.start()
    assert again["already_running"] is True and again["task_id"] == "probe_x"


def test_probe_failure_is_surfaced_as_error(monkeypatch):
    s = _Settings(pool=[{"id": "t1", "name": "n", "api_key": "k", "base_url": "u",
                         "model": "m"}])
    _wire(monkeypatch, s, kbapi_error=RuntimeError("知识库/解析库里找不到可用的英文正文"))
    svc = TranslateProbeService()
    svc._run()
    st = svc.progress()
    assert st["status"] == "error" and "英文正文" in st["error"]


# ---------------------------------------------------------------- 只给建议，不写设置
def test_probe_never_writes_batch_chars(monkeypatch):
    """探测服务**不得**直接写批次上限（用户点了「写入」才由 /translate-batch 写）。"""
    s = _Settings(pool=[{"id": "t1", "name": "n", "api_key": "k", "base_url": "u",
                         "model": "m"}])
    written: list = []
    s.save_translate_batch_chars = lambda v: written.append(v)      # type: ignore[attr-defined]
    _wire(monkeypatch, s)
    svc = TranslateProbeService()
    svc._run()
    assert svc.progress()["status"] == "done"
    assert written == [], "探测只给建议值，绝不自动写入"


# ---------------------------------------------------------------- API 层
def test_get_endpoint_returns_last_result_when_idle(monkeypatch):
    """没在跑时 GET 要回显**上次落盘**的结果（刷新页面还能看到建议值）。"""
    from app.api import settings as api_settings
    last = {"recommended": 3600, "model": "glm-4.5-air", "probed_at": "2026-09-22T23:00:00"}

    class _Probe:
        @staticmethod
        def progress():
            return {"status": "idle"}

    class _S:
        @staticmethod
        def get_translate_probe_result():
            return last

    monkeypatch.setattr(container, "get_translate_probe", lambda: _Probe())
    monkeypatch.setattr(container, "get_settings_service", lambda: _S())
    out = api_settings.get_translate_probe()
    assert out["status"] == "idle" and out["last_result"] == last
