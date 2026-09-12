# -*- coding: utf-8 -*-
"""LLM 客户端测试（构造/注入逻辑，不联网）。"""
from __future__ import annotations

import pytest

from app.services.llm_service import ChatCompleter, DeepSeekAI, DeepSeekError


def test_empty_key_raises():
    with pytest.raises(DeepSeekError):
        DeepSeekAI("", "https://api.deepseek.com", "deepseek-chat")
    with pytest.raises(DeepSeekError):
        ChatCompleter("", "https://api.deepseek.com", "deepseek-chat")


def test_construct_lazy(monkeypatch):
    """构造是懒连接：假 key 可实例化（不发网络请求）。"""
    import openai
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: object())
    ai = DeepSeekAI("sk-fake", "https://api.deepseek.com", "deepseek-chat",
                    timeout_sec=60, max_retries=1)
    assert ai.name == "deepseek"
    assert ai.model == "deepseek-chat"
    cc = ChatCompleter("sk-fake", "https://api.deepseek.com", "deepseek-chat", timeout_sec=60)
    assert cc.model == "deepseek-chat"


def test_extract_delta_text_str_and_list():
    from app.services.llm_service import _extract_delta_text

    class DeltaStr:
        content = "hello"

    class DeltaList:
        content = ["a", {"text": "b"}, {"text": {"value": "c"}}]

    class DeltaNone:
        content = None

    assert _extract_delta_text(DeltaStr()) == "hello"
    assert _extract_delta_text(DeltaList()) == "abc"
    assert _extract_delta_text(DeltaNone()) == ""


# ---------------------------------------------------------- TokenGuard 红线

def test_guard_input_too_large_blocks():
    """单次输入过大 → 拦截（红线：防 prompt 异常膨胀）。"""
    from app.services.llm_service import TokenBudgetExceeded, TokenGuard
    g = TokenGuard(max_input_chars_per_call=100)
    with pytest.raises(TokenBudgetExceeded):
        g.begin_call("ctx", input_chars=101)
    # 未超限不报错
    g.begin_call("ctx", input_chars=50)


def test_guard_high_frequency_blocks():
    """同一 context 高频调用（>上限）→ 拦截。"""
    from app.services.llm_service import TokenBudgetExceeded, TokenGuard
    g = TokenGuard()
    g.CONTEXT_LIMITS["default"] = {"max_calls": 3, "max_total_input_chars": 800_000}
    for _ in range(3):
        g.begin_call("t", input_chars=10)   # 前 3 次正常
    with pytest.raises(TokenBudgetExceeded):
        g.begin_call("t", input_chars=10)   # 第 4 次拦截


def test_guard_total_input_blocks():
    """累计输入超限（死循环特征）→ 拦截；被拦截调用不累加状态。"""
    from app.services.llm_service import TokenBudgetExceeded, TokenGuard
    g = TokenGuard()
    g.CONTEXT_LIMITS["default"] = {"max_calls": 100, "max_total_input_chars": 100}
    g.begin_call("t", 60)   # 累计 60
    with pytest.raises(TokenBudgetExceeded):
        g.begin_call("t", 60)  # 60+60=120 > 100 → 拦截（状态未变）
    with pytest.raises(TokenBudgetExceeded):
        g.begin_call("t", 60)  # 重试同大小仍拦截


def test_guard_engine_limits_relaxed():
    """引擎 context 规则：分步小上下文多次调用不误伤（V02 实测校准）。"""
    from app.services.llm_service import TokenGuard
    g = TokenGuard()
    limits = g._limits_for("engine")
    assert limits["max_calls"] == 12  # V13：10→12（单任务 8 次 + 余量；任务前 reset）
    assert limits["max_total_input_chars"] == 800_000
    # 引擎分步 8 次小调用（每次 20k 字符）正常
    for _ in range(8):
        g.begin_call("engine", 20_000)


def test_guard_usage_records_and_events():
    """usage 记录 + 事件发布。"""
    from app.services.event_bus import EventBus
    from app.services.llm_service import TokenGuard
    bus = EventBus()
    g = TokenGuard(event_bus=bus)
    g.begin_call("engine", 100)
    g.record_usage("engine", 5000, 2000)
    events = bus.history()
    assert any(e["category"] == "usage" for e in events)
    assert "5000" in events[-1]["message"]


def test_guard_retry_reports_event():
    """重试预告 → warning 事件（用户可见消耗）。"""
    from app.services.event_bus import EventBus
    from app.services.llm_service import TokenGuard
    bus = EventBus()
    g = TokenGuard(event_bus=bus)
    g.report_retry("engine", 30000)
    ev = bus.history()[-1]
    assert ev["level"] == "warning"
    assert ev["category"] == "retry"
    assert "30000" in ev["message"]


# ---------------------------------------------------------- 魔塔空信封（P12F）

def _empty_envelope():
    """魔塔限流/额度耗尽占位：200 + choices None + usage 全 0（用户日志原样）。"""
    return {"id": "", "object": "", "created": 0,
            "model": "deepseek-ai/DeepSeek-V4-Flash-0731", "system_fingerprint": "",
            "choices": None,
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


def _ok_envelope(text="hello"):
    return {"id": "chatcmpl-x", "object": "chat.completion", "created": 1,
            "model": "deepseek-ai/DeepSeek-V4-Flash-0731", "system_fingerprint": "",
            "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


class _FakeResp:
    def __init__(self, data): self._d = data
    def raise_for_status(self): pass
    def json(self): return self._d


def _mk_ai(monkeypatch, base_url="https://api-inference.modelscope.cn/v1"):
    import openai
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: object())
    monkeypatch.setattr("app.services.llm_service._RETRY_SLEEP_SEC", 0.0)
    return DeepSeekAI("sk-fake", base_url, "deepseek-ai/DeepSeek-V4-Flash-0731",
                      timeout_sec=60, max_retries=1)


def test_complete_empty_envelope_retries_then_success(monkeypatch):
    """空信封（choices=None 全 0）= 可重试：第 1 次空信封 → 重试成功。"""
    import requests
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        return _FakeResp(_ok_envelope() if calls["n"] > 1 else _empty_envelope())

    monkeypatch.setattr(requests, "post", fake_post)
    ai = _mk_ai(monkeypatch)
    out = ai.complete("prompt", context="engine")
    assert out == "hello"
    assert calls["n"] == 2   # 1 次空信封 + 1 次重试（max_retries=1）


def test_complete_empty_envelope_exhausted_hints_switch(monkeypatch):
    """重试耗尽 → DeepSeekError 带「空信封」+ 切换供应商提示。"""
    import pytest
    import requests
    monkeypatch.setattr(requests, "post",
                        lambda *a, **k: _FakeResp(_empty_envelope()))
    ai = _mk_ai(monkeypatch)
    with pytest.raises(DeepSeekError) as ei:
        ai.complete("prompt", context="engine")
    msg = str(ei.value)
    assert "空信封" in msg and "额度可能耗尽" in msg
    assert "切换供应商" in msg       # modelscope hint
    assert "已重试 1 次" in msg


def test_complete_structure_error_not_retried(monkeypatch):
    """非空信封的结构异常（choices 非列表）→ 不重试直接失败。"""
    import pytest
    import requests
    bad = {"id": "x", "choices": {"foo": 1}, "usage": {"prompt_tokens": 1}}
    monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResp(bad))
    ai = _mk_ai(monkeypatch)
    with pytest.raises(DeepSeekError, match="响应结构异常"):
        ai.complete("prompt", context="engine")


# ---------------------------------------------------------------- P：供应商 max_tokens（翻译截断）
def test_default_max_output_tokens_raise():
    """P：翻译输出上限默认 64000（整篇一次不被截断；≥16384 旧基线仍成立）。"""
    from app.services.llm_service import DEFAULT_MAX_OUTPUT_TOKENS
    assert DEFAULT_MAX_OUTPUT_TOKENS == 64000
    assert DEFAULT_MAX_OUTPUT_TOKENS >= 16384


def test_build_ai_uses_provider_max_tokens(monkeypatch):
    """P：build_ai 透传供应商 max_tokens；未设置用默认（64000，≥16384 旧基线）。"""
    import openai
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: object())
    from app.services.llm_service import DEFAULT_MAX_OUTPUT_TOKENS, build_ai
    ai = build_ai({"api_key": "k", "base_url": "u", "model": "m"})
    assert ai.max_tokens == DEFAULT_MAX_OUTPUT_TOKENS
    assert ai.max_tokens >= 16384
    ai2 = build_ai({"api_key": "k", "base_url": "u", "model": "m", "max_tokens": 32000})
    assert ai2.max_tokens == 32000


def test_complete_sends_configured_max_tokens(monkeypatch):
    """P：DeepSeekAI.complete 按实例 max_tokens 发送（翻译 context 走该方法 → 用该上限）。"""
    import openai
    import requests
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: object())
    cap = {}

    def fake_post(*a, **k):
        cap["json"] = k.get("json")
        return _FakeResp(_ok_envelope())

    monkeypatch.setattr(requests, "post", fake_post)
    ai = DeepSeekAI("sk-fake", "https://api.deepseek.com", "deepseek-chat",
                    max_tokens=32000, timeout_sec=60, max_retries=1)
    ai.complete("prompt", context="translate")
    assert cap["json"]["max_tokens"] == 32000


# ---------------------------------------------------------------- reasoning_effort（思考强度）
def _capture_post(monkeypatch):
    """monkeypatch requests.post 捕获请求体 json；返回 (capture, post_fn)。"""
    import requests
    cap = {}

    def fake_post(*a, **k):
        cap["json"] = k.get("json")
        return _FakeResp(_ok_envelope())

    monkeypatch.setattr(requests, "post", fake_post)
    return cap


def test_default_reasoning_efforts_map():
    """DEFAULT_REASONING_EFFORTS：翻译/总结类 low，编译/汇总类 high。"""
    from app.services.llm_service import DEFAULT_REASONING_EFFORTS
    assert DEFAULT_REASONING_EFFORTS["translate"] == "low"
    assert DEFAULT_REASONING_EFFORTS["summary"] == "low"
    assert DEFAULT_REASONING_EFFORTS["compile"] == "high"
    assert DEFAULT_REASONING_EFFORTS["summarize"] == "high"
    assert DEFAULT_REASONING_EFFORTS["translate"] != DEFAULT_REASONING_EFFORTS["compile"]


def test_complete_sends_reasoning_effort_low_for_translate(monkeypatch):
    """思考型模型（GLM）+ translate context → 请求体带 reasoning_effort=low（省输出 token）。"""
    import openai
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: object())
    cap = _capture_post(monkeypatch)
    ai = DeepSeekAI("sk-fake", "https://api.deepseek.com", "glm-5.3",
                    timeout_sec=60, max_retries=1)
    ai.complete("prompt", context="translate")
    assert cap["json"]["reasoning_effort"] == "low"


def test_complete_sends_reasoning_effort_high_for_compile(monkeypatch):
    """思考型模型（GLM）+ compile context → reasoning_effort=high（编译深度推理）。"""
    import openai
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: object())
    cap = _capture_post(monkeypatch)
    ai = DeepSeekAI("sk-fake", "https://api.deepseek.com", "glm-5.3",
                    timeout_sec=60, max_retries=1)
    ai.complete("prompt", context="compile")
    assert cap["json"]["reasoning_effort"] == "high"


def test_complete_no_reasoning_effort_for_chat_model(monkeypatch):
    """DeepSeek-chat（不思考）→ 不发送 reasoning_effort（防端点不支持的报错）。"""
    import openai
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: object())
    cap = _capture_post(monkeypatch)
    ai = DeepSeekAI("sk-fake", "https://api.deepseek.com", "deepseek-chat",
                    timeout_sec=60, max_retries=1)
    ai.complete("prompt", context="translate")
    assert "reasoning_effort" not in cap["json"]


def test_complete_explicit_reasoning_effort_overrides_context(monkeypatch):
    """供应商显式配置 reasoning_effort 优先于 context 自动映射。"""
    import openai
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: object())
    cap = _capture_post(monkeypatch)
    ai = DeepSeekAI("sk-fake", "https://api.deepseek.com", "glm-5.3",
                    reasoning_effort="max", timeout_sec=60, max_retries=1)
    ai.complete("prompt", context="translate")   # context 映射 low，但显式 max 优先
    assert cap["json"]["reasoning_effort"] == "max"


def test_build_ai_passes_reasoning_effort(monkeypatch):
    """build_ai 透传供应商 reasoning_effort；未配置 → None + 模型名判定是否发送。"""
    import openai
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: object())
    from app.services.llm_service import build_ai
    ai = build_ai({"api_key": "k", "base_url": "u", "model": "m", "reasoning_effort": "low"})
    assert ai.reasoning_effort == "low"
    assert ai._reasoning_supported is True
    ai2 = build_ai({"api_key": "k", "base_url": "u", "model": "m"})
    assert ai2.reasoning_effort is None
    assert ai2._reasoning_supported is False    # 模型名不命中思考型 → 不送
