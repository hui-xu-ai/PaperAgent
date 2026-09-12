# -*- coding: utf-8 -*-
"""思考强度（reasoning_effort）+ 流式新事件（status / reasoning）单测。

冻结契约（前端按此实现）：
1. `POST /api/chat/stream` body 可选 `effort` = "low"|"high"|"auto"|null；
   缺省 / "auto" / null / **非法值** ⇒ **不发送** `reasoning_effort`（用供应商默认）。
2. SSE 追加事件：检索**开始前**的 `{"type":"status","text":"正在检索知识库…"}`；
   供应商返回 `delta.reasoning_content` 时的 `{"type":"reasoning","text":...}`。
3. `GET /api/settings` 回传 `chat_reasoning_effort`（默认 "auto"）；
   `POST /api/settings/chat-reasoning-effort {"effort":...}` → `{"ok":true,"effort":...}`，非法值 400。
4. `effort` 进回答缓存 key（换档不复用旧档答案）。

不联网：openai client / 对话通道全部换成 fake（只记录请求体与事件）。
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.chat as chat_api
import app.api.settings as settings_api
from app.services import container
from app.services.chat_service import ChatService
from app.services.llm_service import ChatCompleter
from app.services.settings_service import SettingsService
from app.services.store import Store
from conftest import ENGINE_DOC


# ---------------------------------------------------------------- fakes（不联网）
class _Delta:
    """流式 delta 替身：正文/思维链按需给（不给的属性即"供应商没返回"）。"""

    def __init__(self, content=None, reasoning_content=None, **extra):
        if content is not None:
            self.content = content
        if reasoning_content is not None:
            self.reasoning_content = reasoning_content
        for k, v in extra.items():
            setattr(self, k, v)


class _Chunk:
    def __init__(self, delta=None, usage=None):
        self.choices = [] if delta is None else [SimpleNamespace(delta=delta)]
        self.usage = usage


class _FakeCompletions:
    """openai chat.completions 替身：记录每次请求体；fail_on 命中即 raise（模拟端点拒绝）。"""

    def __init__(self, chunks, fail_on=None):
        self.chunks = list(chunks)
        self.fail_on = fail_on
        self.payloads: list[dict] = []

    def create(self, **kwargs):
        self.payloads.append(kwargs)
        if self.fail_on and self.fail_on(kwargs):
            raise RuntimeError("Error code: 400 - unknown parameter: reasoning_effort")
        return iter(self.chunks)


def _completer(monkeypatch, *, model="deepseek-chat", reasoning_effort=None,
               chunks=None, fail_on=None):
    """构造 ChatCompleter 并注入 fake client（避免真 OpenAI 客户端/网络）。"""
    import openai
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: object())
    cc = ChatCompleter("sk-fake", "https://api.example/v1", model,
                       reasoning_effort=reasoning_effort)
    comp = _FakeCompletions(chunks or [_Chunk(_Delta(content="hi"))], fail_on=fail_on)
    cc._client = SimpleNamespace(chat=SimpleNamespace(completions=comp))
    return cc, comp


class _StubSettingsService:
    def get_system_prompt_extra(self):
        return ""

    def get_retrieval_mode(self):
        return "notes"


class _StubKbMeta:
    def paper_compiled(self, doi):
        return []

    def recall_paper(self, doi, query, top_k=4):
        return []

    def recall(self, query, top_k=6):
        return []


class _ReasoningChat:
    """对话通道替身：先吐思维链、再吐正文；记录每次透传的 effort。"""

    def __init__(self):
        self.efforts: list[str | None] = []

    def stream_events(self, context, messages, effort=None):
        self.efforts.append(effort)
        yield {"type": "reasoning", "text": "先看摘要，"}
        yield {"type": "reasoning", "text": "再比方法。"}
        yield {"type": "delta", "text": "这是答案。"}


@pytest.fixture
def chat(settings, store, engine, fake_chat):
    return ChatService(settings, store, engine, fake_chat,
                       settings_service=_StubSettingsService(),
                       kbmeta=lambda: _StubKbMeta())


def _translated_paper(store):
    pid = store.create_paper(r"input\a.pdf", "test.pdf")
    store.update_paper(pid, doc_json=str(ENGINE_DOC), status="translated")
    return pid


# ---------------------------------------------------------------- ① effort 透传
def test_request_effort_low_high_into_request_body(monkeypatch):
    """合法值 low/high ⇒ 写进请求体 reasoning_effort（含"非思考型"模型名的显式请求）。"""
    for val in ("low", "high"):
        cc, comp = _completer(monkeypatch, model="deepseek-chat")
        list(cc.stream_events("session:1", [{"role": "user", "content": "q"}], effort=val))
        assert comp.payloads[0]["reasoning_effort"] == val


def test_no_effort_means_no_parameter(monkeypatch):
    """effort=None（构造级也未配置）⇒ 请求体**不带** reasoning_effort。"""
    cc, comp = _completer(monkeypatch)
    list(cc.stream_events("session:1", [{"role": "user", "content": "q"}]))
    assert "reasoning_effort" not in comp.payloads[0]


def test_constructor_level_effort_needs_thinking_model(monkeypatch):
    """构造级默认值：思考型模型名（GLM 命中 REASONING_MODEL_HINTS）才发；否则不发。"""
    cc, comp = _completer(monkeypatch, model="glm-4.6", reasoning_effort="low")
    list(cc.stream_events("s:1", [{"role": "user", "content": "q"}]))
    assert comp.payloads[0]["reasoning_effort"] == "low"

    cc2, comp2 = _completer(monkeypatch, model="deepseek-chat", reasoning_effort="low")
    list(cc2.stream_events("s:1", [{"role": "user", "content": "q"}]))
    assert "reasoning_effort" not in comp2.payloads[0]


def test_auto_null_illegal_not_forwarded_at_service_level(chat, store):
    """契约 1（服务层归一化）：auto / None / 非法值 ⇒ 通道收到 effort=None（=不发）。"""
    pid = _translated_paper(store)
    sid = chat.create_session(pid)
    for i, val in enumerate((None, "auto", "AUTO", "medium", "", "  ")):
        list(chat.ask_stream(sid, f"问题 {i}", effort=val))
    assert chat.chat.efforts == [None] * 6


def test_legal_effort_forwarded_at_service_level(chat, store):
    """合法值透传 + 大小写/空白归一化。"""
    pid = _translated_paper(store)
    sid = chat.create_session(pid)
    list(chat.ask_stream(sid, "问题甲", effort="high"))
    list(chat.ask_stream(sid, "问题乙", effort=" LOW "))
    assert chat.chat.efforts == ["high", "low"]


def test_stream_api_passes_effort(monkeypatch):
    """POST /api/chat/stream 把 body.effort 透传到 ask_stream（缺省为 None）。"""
    seen: list[str | None] = []

    class _StubChatService:
        def ask_stream(self, session_id, question, effort=None):
            seen.append(effort)
            yield {"type": "delta", "text": "ok"}

    monkeypatch.setattr(container, "get_chat", lambda: _StubChatService())
    app = FastAPI()
    app.include_router(chat_api.router)
    client = TestClient(app)
    r = client.post("/api/chat/stream", json={"session_id": 1, "question": "q", "effort": "high"})
    assert r.status_code == 200 and "ok" in r.text
    client.post("/api/chat/stream", json={"session_id": 1, "question": "q"})
    assert seen == ["high", None]


# ---------------------------------------------------------------- ② 端点拒绝 → 降级重试
def test_endpoint_rejects_effort_degrades_and_retries(monkeypatch, caplog):
    """端点 400 拒绝 reasoning_effort ⇒ 去掉该参数重试一次并记 warning（不打死整轮）。"""
    cc, comp = _completer(monkeypatch, fail_on=lambda kw: "reasoning_effort" in kw)
    with caplog.at_level(logging.WARNING, logger="app.services.llm_service"):
        events = list(cc.stream_events("s:1", [{"role": "user", "content": "q"}],
                                       effort="high"))
    assert len(comp.payloads) == 2                       # 原请求 + 降级重试
    assert comp.payloads[0]["reasoning_effort"] == "high"
    assert "reasoning_effort" not in comp.payloads[1]    # 降级：去掉参数
    assert events == [{"type": "delta", "text": "hi"}]   # 降级后正常出流
    assert any("降级重试" in r.message for r in caplog.records)


def test_failure_without_effort_propagates(monkeypatch):
    """没发 reasoning_effort 时端点报错 ⇒ 原样抛出（不无限重试）。"""
    cc, comp = _completer(monkeypatch, fail_on=lambda kw: True)
    with pytest.raises(RuntimeError):
        list(cc.stream_events("s:1", [{"role": "user", "content": "q"}]))
    assert len(comp.payloads) == 1


# ---------------------------------------------------------------- ③ reasoning 事件
def test_extract_delta_reasoning_shapes():
    """属性 / model_extra（新版 SDK）两种取法 + 无字段返回空串。"""
    from app.services.llm_service import _extract_delta_reasoning, _extract_delta_text

    assert _extract_delta_reasoning(_Delta(reasoning_content="想")) == "想"
    assert _extract_delta_reasoning(
        SimpleNamespace(model_extra={"reasoning_content": "想"})) == "想"
    assert _extract_delta_reasoning(SimpleNamespace()) == ""
    # 正文语义未变：只取 content
    assert _extract_delta_text(_Delta(content="正文", reasoning_content="想")) == "正文"
    assert _extract_delta_text(_Delta(reasoning_content="想")) == ""


def test_stream_events_yields_reasoning_before_delta(monkeypatch):
    """reasoning_content 逐片转 reasoning 事件，且不混进 delta 正文。"""
    cc, _ = _completer(monkeypatch, chunks=[
        _Chunk(_Delta(reasoning_content="思考1")),
        _Chunk(_Delta(reasoning_content="思考2")),
        _Chunk(_Delta(content="正文")),
    ])
    events = list(cc.stream_events("s:1", [{"role": "user", "content": "q"}]))
    assert events == [{"type": "reasoning", "text": "思考1"},
                      {"type": "reasoning", "text": "思考2"},
                      {"type": "delta", "text": "正文"}]


def test_reasoning_events_reach_ask_stream_and_stay_out_of_answer(settings, store, engine):
    """服务层：reasoning 片段转 `{"type":"reasoning"}` 事件；入库正文只含正文。"""
    rc = _ReasoningChat()
    svc = ChatService(settings, store, engine, rc,
                      settings_service=_StubSettingsService(),
                      kbmeta=lambda: _StubKbMeta())
    sid = svc.create_session(_translated_paper(store))
    events = list(svc.ask_stream(sid, "问题", effort="high"))
    assert [e["type"] for e in events] == ["status", "start", "reasoning", "reasoning",
                                           "delta", "done"]
    assert "".join(e["text"] for e in events if e["type"] == "reasoning") == "先看摘要，再比方法。"
    assert events[0]["text"] == "正在检索知识库…"
    assert store.recent_messages(sid, 10)[-1]["content"] == "这是答案。"
    assert rc.efforts == ["high"]


def test_status_precedes_retrieval_for_paper_and_kb(chat, store):
    """status 在检索**开始前**发：论文路径（status→start）与知识库 qa 路径（start→status）。"""
    pid = _translated_paper(store)
    sid = chat.create_session(pid)
    ev = list(chat.ask_stream(sid, "问题"))
    assert ev[0] == {"type": "status", "text": "正在检索知识库…"}
    assert ev[1]["type"] == "start"

    gid = chat.create_session(None, kind="global", mode="qa")
    ev2 = list(chat.ask_stream(gid, "问题"))
    types = [e["type"] for e in ev2]
    assert types.index("status") < types.index("delta")
    assert types[0] == "start" and types[1] == "status"


# ---------------------------------------------------------------- ④ effort 进缓存 key
def test_effort_in_cache_key(chat, store):
    """同问题同档位 → 命中缓存；换档（high → low/None）→ 缓存不命中（重新请求）。"""
    pid = _translated_paper(store)
    sid = chat.create_session(pid)
    list(chat.ask_stream(sid, "同一个问题", effort="high"))
    n = len(chat.chat.calls)
    again = list(chat.ask_stream(sid, "同一个问题", effort="high"))
    assert again[0]["cached"] is True and len(chat.chat.calls) == n

    for val in ("low", None):
        ev = list(chat.ask_stream(sid, "同一个问题", effort=val))
        assert ev[0].get("cached") is not True   # 换档 ⇒ 键不同 ⇒ 不命中旧答案
    assert len(chat.chat.calls) == n + 2


def test_cache_key_effort_dimension_is_the_only_diff(chat):
    """键的直接证据：effort 不同 ⇒ hash 不同；effort 相同 ⇒ hash 相同。"""
    a = chat._cache_key("paper:1", "q", kind="paper", retrieval_mode="notes",
                        fingerprint="fp", effort="high")
    b = chat._cache_key("paper:1", "q", kind="paper", retrieval_mode="notes",
                        fingerprint="fp", effort="low")
    c = chat._cache_key("paper:1", "q", kind="paper", retrieval_mode="notes",
                        fingerprint="fp", effort="high")
    assert a != b and a == c


# ---------------------------------------------------------------- ⑤ 设置（服务 + API）
def test_settings_roundtrip_and_get_all(tmp_path):
    """get/save 往返 + get_all 回传；默认 auto；脏值读回兜底 auto。"""
    svc = SettingsService(Store(str(tmp_path / "t.db")))
    assert svc.get_chat_reasoning_effort() == "auto"
    assert svc.get_all()["chat_reasoning_effort"] == "auto"
    assert svc.save_chat_reasoning_effort("HIGH") == "high"
    assert svc.get_chat_reasoning_effort() == "high"
    assert svc.get_all()["chat_reasoning_effort"] == "high"
    svc.store.set_setting("chat_reasoning_effort", "ultra")   # 脏值（外部改库）
    assert svc.get_chat_reasoning_effort() == "auto"


def test_settings_save_rejects_illegal(tmp_path):
    """save 非法值抛 ValueError（API 层转 400）。"""
    svc = SettingsService(Store(str(tmp_path / "t.db")))
    for bad in ("ultra", "medium", "", None, "low high"):
        with pytest.raises(ValueError):
            svc.save_chat_reasoning_effort(bad)


@pytest.fixture
def settings_client(tmp_path, monkeypatch):
    """最小 app（settings 路由）+ 真实 SettingsService（临时 DB，不碰真实配置）。"""
    svc = SettingsService(Store(str(tmp_path / "api.db")))
    monkeypatch.setattr(container, "get_settings_service", lambda: svc)
    app = FastAPI()
    app.include_router(settings_api.router)
    return TestClient(app), svc


def test_settings_endpoint_ok_and_400(settings_client):
    """POST 合法值 → {"ok":true,"effort":...}；非法值 → 400（本端点选"拒绝"而非归一化）。"""
    client, svc = settings_client
    r = client.post("/api/settings/chat-reasoning-effort", json={"effort": "high"})
    assert r.status_code == 200 and r.json() == {"ok": True, "effort": "high"}
    assert svc.get_chat_reasoning_effort() == "high"
    r2 = client.post("/api/settings/chat-reasoning-effort", json={"effort": "ultra"})
    assert r2.status_code == 400 and "effort 仅支持" in r2.json()["detail"]
    assert svc.get_chat_reasoning_effort() == "high"          # 400 不落库
    # 缺省 body ⇒ auto（前端"自动"档等价于不带 effort）
    r3 = client.post("/api/settings/chat-reasoning-effort", json={})
    assert r3.json() == {"ok": True, "effort": "auto"}


def test_get_settings_exposes_chat_reasoning_effort(settings_client):
    """GET /api/settings 回传 chat_reasoning_effort（前端初始化下拉框）。"""
    client, svc = settings_client
    assert client.get("/api/settings").json()["chat_reasoning_effort"] == "auto"
    svc.save_chat_reasoning_effort("low")
    assert client.get("/api/settings").json()["chat_reasoning_effort"] == "low"
