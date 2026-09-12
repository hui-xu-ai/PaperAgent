# -*- coding: utf-8 -*-
"""用户反馈修复（2026-09-12）单测：供应商「测试连接」的 Key 解析 + 自动编译开关入口。

- 测试连接：编辑已配置的供应商时密钥框是掩码占位（`••••••••`），旧前端据此直接拦下
  "请先填写真实 API Key"——**已保存的 Key 明明可用**。现在由后端按 `id` 回退取已存 Key。
- 自动编译：`save_auto_compile` 早已存在却无 API/UI（关掉后「解析+编译」静默不做事），
  本轮补 `POST /api/settings/auto-compile`，并让 `SettingsService.get_all()` 回传该值。

不碰真实 DB/网络：FakeSettings 替换 container 单例；build_ai 被换成本地假客户端。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.settings as settings_api
from app.services import container
from app.services.settings_service import SettingsService
from app.services.store import Store

MASK = "••••••••"          # 与前端 KEY_MASK 一致（点掩码）
REAL_KEY = "sk-real-1234567890abcdef"
_DS = {"id": "deepseek", "name": "DeepSeek",
       "base_url": "https://api.deepseek.com/v1", "model": "deepseek-flash"}


class _FakeCompletions:
    def __init__(self, sink):
        self._sink = sink

    def create(self, **kwargs):
        self._sink.append(kwargs)

        class _Msg:
            content = "pong"

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]

        return _Resp()


class FakeAI:
    def __init__(self, sink):
        completions = _FakeCompletions(sink)

        class _Chat:
            pass

        chat = _Chat()
        chat.completions = completions

        class _Client:
            pass

        client = _Client()
        client.chat = chat
        self._client = client


class FakeSettings:
    """最小 SettingsService 替身：只需 get_providers(masked=False) 与 auto_compile 读写。"""

    def __init__(self, auto_compile=True):
        self.providers = [
            dict(_DS, api_key=REAL_KEY),
            {"id": "nokey", "name": "无 Key", "base_url": "https://x/v1",
             "model": "m", "api_key": ""},
        ]
        self.auto_compile = auto_compile
        self.saved_auto_compile: list[bool] = []

    def get_providers(self, masked: bool = True):  # noqa: ARG002 - 替身：两种模式同一份
        return self.providers

    def get_auto_compile(self) -> bool:
        return self.auto_compile

    def save_auto_compile(self, enabled: bool) -> None:
        self.auto_compile = bool(enabled)
        self.saved_auto_compile.append(bool(enabled))


@pytest.fixture()
def env(monkeypatch):
    """最小 app（仅 settings 路由）+ FakeSettings；返回 (client, fake, sent_payloads)。"""
    fake = FakeSettings()
    sent: list[dict] = []
    monkeypatch.setattr(container, "get_settings_service", lambda: fake)
    monkeypatch.setattr(settings_api, "build_ai",
                        lambda payload, guard=None: FakeAI(sent))
    app = FastAPI()
    app.include_router(settings_api.router)
    return TestClient(app), fake, sent


# ---------------------------------------------------------------- _resolve_test_key
def test_resolve_key_masked_falls_back_to_stored(env):
    """掩码占位（用户没改密钥）→ 解析出已存的真实 Key。"""
    _, _, _ = env
    p = settings_api.TestProviderModel(**_DS, api_key=MASK)
    assert settings_api._resolve_test_key(p) == REAL_KEY


def test_resolve_key_blank_falls_back_to_stored(env):
    """完全空 Key → 同样回退（前端不再回传掩码时行为一致）。"""
    _, _, _ = env
    p = settings_api.TestProviderModel(**_DS, api_key="")
    assert settings_api._resolve_test_key(p) == REAL_KEY


def test_resolve_key_plaintext_wins(env):
    """用户改了密钥 → 明文优先，不被已存 Key 覆盖（否则改了 Key 测的却是旧的）。"""
    _, _, _ = env
    p = settings_api.TestProviderModel(**_DS, api_key="sk-brand-new-key-value")
    assert settings_api._resolve_test_key(p) == "sk-brand-new-key-value"


def test_resolve_key_unknown_id_returns_plaintext(env):
    """未知 id（新增供应商还没 id）：明文原样返回；空 → 空串（交调用方报错）。"""
    _, _, _ = env
    assert settings_api._resolve_test_key(
        settings_api.TestProviderModel(id="", name="n", base_url="b", model="m",
                                       api_key="sk-plain")) == "sk-plain"
    assert settings_api._resolve_test_key(
        settings_api.TestProviderModel(id="", name="n", base_url="b", model="m",
                                       api_key="")) == ""


# ---------------------------------------------------------------- POST /api/settings/test
def test_test_endpoint_masked_key_succeeds(env):
    """掩码 Key 也能测通（本 bug 的核心断言：不再被拦成"请先填写真实 API Key"）。"""
    client, _, sent = env
    r = client.post("/api/settings/test", json=dict(_DS, api_key=MASK))
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert sent and sent[0]["model"] == "deepseek-flash"


def test_test_endpoint_uses_plaintext_when_given(env):
    """给了明文 → 用明文构造客户端（不静默换成已存 Key）。"""
    client, _, _ = env
    captured: list[dict] = []
    import app.api.settings as m
    m.build_ai = lambda payload, guard=None: (captured.append(payload), FakeAI([]))[1]
    try:
        r = client.post("/api/settings/test", json=dict(_DS, api_key="sk-brand-new-key"))
    finally:
        m.build_ai = settings_api.build_ai
    assert r.status_code == 200, r.text
    assert captured and captured[0]["api_key"] == "sk-brand-new-key"


def test_test_endpoint_reports_missing_key(env):
    """既没填、也没有已存 Key → 明确报错（去掉含糊的"掩码占位"文案）。"""
    client, _, _ = env
    r = client.post("/api/settings/test", json={
        "id": "nokey", "name": "无 Key", "base_url": "https://x/v1",
        "model": "m", "api_key": ""})
    assert r.status_code == 400
    assert "API Key" in r.json()["detail"]


# ---------------------------------------------------------------- 自动编译开关
def test_auto_compile_endpoint_roundtrip(env):
    """POST /api/settings/auto-compile 写入服务端（False/True 两向）。"""
    client, fake, _ = env
    r = client.post("/api/settings/auto-compile", json={"enabled": False})
    assert r.status_code == 200 and r.json() == {"ok": True, "enabled": False}
    assert fake.saved_auto_compile == [False] and fake.get_auto_compile() is False
    r2 = client.post("/api/settings/auto-compile", json={"enabled": True})
    assert r2.json()["enabled"] is True and fake.get_auto_compile() is True


def test_get_all_exposes_auto_compile(tmp_path):
    """真实 SettingsService：默认**开**，关掉后 get_all() 如实回传（前端据此勾选）。"""
    svc = SettingsService(Store(str(tmp_path / "t.db")))
    assert svc.get_auto_compile() is True
    assert svc.get_all()["auto_compile"] is True
    svc.save_auto_compile(False)
    assert svc.get_auto_compile() is False
    assert svc.get_all()["auto_compile"] is False
