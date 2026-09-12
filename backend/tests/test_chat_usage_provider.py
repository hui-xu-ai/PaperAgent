# -*- coding: utf-8 -*-
"""用户反馈修复（2026-09-12）单测：会话链路的用量必须记真实 provider id。

现象：事件流里 `engine [deepseek-flash] … 约 ¥0.0585` 有价，而
`session:5 [deepseek-flash] … 约 ¥0.0000` 恒为 0。
根因：`ChatCompleter` 三处 `record_usage` 把 provider **硬编码成 "chat"**，
单价表按 `provider_id::model` 查（`settings_service.get_prices_for`）→
查 `chat::deepseek-flash` 必然未命中 → 恒 0。
修法：改为 `self.provider_id`（构造时由 `init_llm` 传入真实供应商 id）。

不连网：`_client` 被换成假客户端，`guard` 换成记录器。
"""
from __future__ import annotations

from app.services.llm_service import ChatCompleter


class FakeGuard:
    def __init__(self):
        self.begun: list[tuple] = []
        self.records: list[dict] = []

    def begin_call(self, context, input_chars):
        self.begun.append((context, input_chars))

    def record_usage(self, context, prompt_tokens, completion_tokens,
                     provider="", model="", cache_hit_tokens=0):
        self.records.append({"context": context, "provider": provider, "model": model,
                             "prompt": prompt_tokens, "completion": completion_tokens})


class _Delta:
    def __init__(self, content=None, reasoning=None):
        self.content = content
        if reasoning is not None:
            self.reasoning_content = reasoning


class _Choice:
    def __init__(self, delta):
        self.delta = delta


class _Usage:
    prompt_tokens = 603
    completion_tokens = 954
    prompt_cache_hit_tokens = 384


class _Chunk:
    def __init__(self, delta=None, usage=None):
        self.choices = [_Choice(delta)] if delta is not None else []
        self.usage = usage


class _FakeCompletions:
    def __init__(self, chunks):
        self._chunks = chunks
        self.requests: list[dict] = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return iter(self._chunks)


class _FakeClient:
    def __init__(self, chunks):
        self.completions = _FakeCompletions(chunks)
        self.chat = type("_C", (), {"completions": self.completions})()


def _make(provider_id: str, chunks):
    guard = FakeGuard()
    c = ChatCompleter(api_key="sk-test", base_url="https://api.deepseek.com/v1",
                      model="deepseek-flash", guard=guard, provider_id=provider_id)
    fake = _FakeClient(chunks)
    c._client = fake
    return c, guard, fake


def test_stream_records_real_provider_id():
    """核心断言：provider 记录为真实供应商 id，而不是写死的 "chat"。"""
    c, guard, _ = _make("deepseek", [
        _Chunk(_Delta("你")), _Chunk(_Delta("好")), _Chunk(usage=_Usage()),
    ])
    out = "".join(c.stream("session:5", [{"role": "user", "content": "hi"}]))
    assert out == "你好"
    assert len(guard.records) == 1
    rec = guard.records[0]
    assert rec["provider"] == "deepseek", "会话用量必须记真实 provider id（否则单价查不到 → ¥0）"
    assert rec["model"] == "deepseek-flash"
    assert rec["prompt"] == 603 and rec["completion"] == 954


def test_stream_keeps_context_key():
    """context（session:N / engine）不能被顺手改掉——它是用量分组的键。"""
    c, guard, _ = _make("siliconflow", [_Chunk(_Delta("x")), _Chunk(usage=_Usage())])
    list(c.stream("session:12", [{"role": "user", "content": "hi"}]))
    assert guard.begun and guard.begun[0][0] == "session:12"
    assert guard.records[0]["context"] == "session:12"
    assert guard.records[0]["provider"] == "siliconflow"


def test_stream_request_body_has_no_chat_provider_leak():
    """请求体里的模型/温度等契约不变（防顺手改动）。"""
    c, _, fake = _make("deepseek", [_Chunk(_Delta("y"))])
    list(c.stream("session:1", [{"role": "user", "content": "hi"}]))
    req = fake.completions.requests[0]
    assert req["model"] == "deepseek-flash" and req["stream"] is True
    assert req["stream_options"] == {"include_usage": True}
