# -*- coding: utf-8 -*-
"""批3：编译思考档（`compile_reasoning_effort`）与「档位真的下发」回归测试。

背景（2026-09-12 用户决策 + 服务端实测）：
- `deepseek-flash` 是思考型模型，思考 token 计入 completion 并按输出价计费
  （实测 L2 输出 9,673 token，而产物 `_details.md` 仅 3,019 字符 ⇒ 约 7k 是思考）；
- 服务端**拒绝字面 "auto"**（实测 400 unknown variant `auto`）⇒「自动」只能以**不传参**实现；
- 服务端接受 `none/minimal/low/medium/high`；
- 此前 `compile=high` 的映射**从未生效**：`kbmeta_service.KbLlm` 把 context 折成 `engine`
  只为 TokenGuard 分组，导致 `DEFAULT_REASONING_EFFORTS` 取不到 compile/translate，
  且 `REASONING_MODEL_HINTS` 不认 deepseek ⇒ 所有编译都跑在服务端默认重思考。

本文件锁定：① 设置读写与校验；② effort 决策优先级；③ **编译请求体里确实带上了该参数**。
"""
from __future__ import annotations

import pytest

from app.services.llm_service import DeepSeekAI
from app.services.settings_service import SettingsService


# ---------------------------------------------------------------- 设置读写
def test_compile_effort_default_auto(store):
    svc = SettingsService(store)
    assert svc.get_compile_effort() == "auto"


def test_compile_effort_roundtrip_and_guard(store):
    svc = SettingsService(store)
    assert svc.save_compile_effort("low") == "low"
    assert svc.get_compile_effort() == "low"
    assert svc.save_compile_effort("NONE") == "none"      # 大小写归一
    with pytest.raises(ValueError):
        svc.save_compile_effort("auto-typo")               # 非法值拒绝（不静默改写）
    assert svc.get_compile_effort() == "none"              # 失败不覆盖旧值


def test_compile_effort_in_get_all(store):
    svc = SettingsService(store)
    assert svc.get_all()["compile_reasoning_effort"] == "auto"


def test_dirty_value_falls_back_to_auto(store):
    """库里的脏值（历史/手改）读回按 auto 兜底，绝不把非法值发给端点。"""
    store.set_setting("compile_reasoning_effort", "AUTO ")   # 含空格+大写
    svc = SettingsService(store)
    assert svc.get_compile_effort() == "auto"
    store.set_setting("compile_reasoning_effort", "ultra")
    assert svc.get_compile_effort() == "auto"


# ---------------------------------------------------------------- 翻译思考档（批3）
def test_translate_effort_default_auto(store):
    svc = SettingsService(store)
    assert svc.get_translate_effort() == "auto"          # 推荐默认：不干预 ⇒ 翻译质量不变


def test_translate_effort_roundtrip_and_guard(store):
    svc = SettingsService(store)
    assert svc.save_translate_effort("low") == "low"
    assert svc.get_translate_effort() == "low"
    with pytest.raises(ValueError):
        svc.save_translate_effort("auto-typo")
    assert svc.get_translate_effort() == "low"           # 失败不覆盖旧值
    store.set_setting("translate_reasoning_effort", "ultra")
    assert svc.get_translate_effort() == "auto"          # 脏值兜底


def test_both_efforts_in_get_all(store):
    svc = SettingsService(store)
    all_cfg = svc.get_all()
    assert all_cfg["compile_reasoning_effort"] == "auto"
    assert all_cfg["translate_reasoning_effort"] == "auto"


# ---------------------------------------------------------------- effort 决策
def _ai(monkeypatch, **kw) -> DeepSeekAI:
    import openai
    monkeypatch.setattr(openai, "OpenAI", lambda **kw2: object())
    return DeepSeekAI("sk-fake", "https://api.deepseek.com", "deepseek-flash", **kw)


def test_resolve_effort_compile_auto_falls_back_to_mapping(monkeypatch):
    """`auto`（默认）= 回落既有映射：deepseek-flash 因 hints 不匹配 ⇒ 实际**不发送**（= 历史行为）。"""
    ai = _ai(monkeypatch)
    monkeypatch.setattr(DeepSeekAI, "_task_effort_setting",
                        staticmethod(lambda ctx: "auto"))
    effort, explicit = ai._resolve_effort("compile")
    assert explicit is False                    # 非显式 ⇒ 受 hints 门禁约束
    assert ai._reasoning_supported is False     # deepseek-flash 不在 hints 里
    assert ai._resolve_effort("l2") == ("high", False)


def test_resolve_effort_auto_keeps_glm_high(monkeypatch):
    """兼容性钉：GLM 等思考型模型 + 编译族 auto ⇒ 仍发送既有映射的 compile=high。"""
    import openai
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: object())
    ai = DeepSeekAI("sk-fake", "https://api.deepseek.com", "glm-5.3")
    monkeypatch.setattr(DeepSeekAI, "_task_effort_setting",
                        staticmethod(lambda ctx: "auto"))
    assert ai._reasoning_supported is True
    assert ai._resolve_effort("compile") == ("high", False)


def test_resolve_effort_compile_explicit_is_sent(monkeypatch):
    ai = _ai(monkeypatch)
    monkeypatch.setattr(DeepSeekAI, "_task_effort_setting",
                        staticmethod(lambda ctx: "none"))
    effort, explicit = ai._resolve_effort("compile")
    assert (effort, explicit) == ("none", True)     # 显式 ⇒ 不受 hints 门禁约束


def test_resolve_effort_translate_auto_falls_back(monkeypatch):
    """翻译档 auto（默认）⇒ 回落既有映射：deepseek 不发送（历史行为），GLM 仍发 low。"""
    ai = _ai(monkeypatch)
    monkeypatch.setattr(DeepSeekAI, "_task_effort_setting",
                        staticmethod(lambda ctx: "auto"))
    assert ai._resolve_effort("translate") == ("low", False)
    assert ai._reasoning_supported is False        # deepseek ⇒ 实际不发送


def test_resolve_effort_translate_explicit_is_sent(monkeypatch):
    ai = _ai(monkeypatch)
    monkeypatch.setattr(DeepSeekAI, "_task_effort_setting",
                        staticmethod(lambda ctx: "low"))
    assert ai._resolve_effort("translate") == ("low", True)


def test_task_effort_setting_routes_by_context(monkeypatch):
    """编译族/翻译族各取各的设置项（不能串味）。"""
    import app.services.container as c
    ai = _ai(monkeypatch)

    class _Svc:
        def get_compile_effort(self):
            return "none"
        def get_translate_effort(self):
            return "minimal"

    monkeypatch.setattr(c, "get_settings_service", lambda: _Svc())
    assert DeepSeekAI._task_effort_setting("compile") == "none"
    assert DeepSeekAI._task_effort_setting("translate") == "minimal"


def test_resolve_effort_provider_level_wins(monkeypatch):
    ai = _ai(monkeypatch, reasoning_effort="medium")
    assert ai._resolve_effort("compile") == ("medium", True)
    assert ai._resolve_effort("translate") == ("medium", True)


# ---------------------------------------------------------------- 请求体确实带上
def test_compile_request_carries_reasoning_effort(monkeypatch):
    """端到端（HTTP 打桩）：设置=low 时，编译请求体必须带 reasoning_effort=low。"""
    import requests
    ai = _ai(monkeypatch)
    monkeypatch.setattr(DeepSeekAI, "_task_effort_setting",
                        staticmethod(lambda ctx: "low"))
    captured: dict = {}

    class _Resp:
        status_code = 200
        def raise_for_status(self):
            return None
        def json(self):
            return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2}}

    # llm_service 在函数内 `import requests as _req`，故打桩真实 requests.post
    monkeypatch.setattr(requests, "post",
                        lambda url, headers=None, json=None, timeout=None:
                        (captured.update(payload=json), _Resp())[1])
    out = ai.complete("PROMPT", context="engine", effort_context="compile")
    assert out == "ok"
    assert captured["payload"]["reasoning_effort"] == "low"


def test_compile_request_omits_effort_when_auto(monkeypatch):
    import requests
    ai = _ai(monkeypatch)
    monkeypatch.setattr(DeepSeekAI, "_task_effort_setting",
                        staticmethod(lambda ctx: "auto"))
    captured: dict = {}

    class _Resp:
        def raise_for_status(self):
            return None
        def json(self):
            return {"choices": [{"message": {"content": "ok"}}], "usage": {}}

    monkeypatch.setattr(requests, "post",
                        lambda url, headers=None, json=None, timeout=None:
                        (captured.update(payload=json), _Resp())[1])
    ai.complete("PROMPT", context="engine", effort_context="compile")
    assert "reasoning_effort" not in captured["payload"]     # 自动 = 不传参（服务端自适应）


def test_kbmeta_passes_real_context_for_effort(monkeypatch):
    """`_KBLLMAdapter` 必须把**真实 context**（compile）传给 effort 决策，否则档位永远取不到。"""
    from app.services.kbmeta_service import _KBLLMAdapter

    class _Base:
        def __init__(self):
            self.kw = None
        def complete(self, prompt, context="engine", effort_context=None):
            self.kw = {"context": context, "effort_context": effort_context}
            return "ok"

    base = _Base()
    _KBLLMAdapter(base).complete("p", "compile")
    assert base.kw == {"context": "engine", "effort_context": "compile"}   # TokenGuard 用 engine
    _KBLLMAdapter(base).complete("p", "translate")
    assert base.kw == {"context": "translate", "effort_context": "translate"}


@pytest.fixture
def settings_service_stub():
    """占位 fixture（保持测试签名统一；本文件不依赖真实容器）。"""
    return None


