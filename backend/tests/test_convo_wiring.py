# -*- coding: utf-8 -*-
"""对话式一次流转的**接线守卫**（2026-09-13 两次实测事故后补）。

事故记录（都在 `agent_feedback` 的教训里）：
1. `_try_conversation_flow` 写成 `from .container import get_kbmeta`（container 无此名）→ ImportError
   被宽 `except` 吞成一行 warning ⇒ 线上静默回退老流程；
2. 键只读 `paper["doi"]`，而 papers 表该列为 **NULL**（DOI 在 document.json/papers_meta）⇒
   静默 return False，连日志都没有。
本测试用最小 stub + **真实的小 document.json** 真跑这条分支：import 名错、属性错、键取错都会当场炸。
"""
from __future__ import annotations

import json

import pytest

import app.services.task_service as ts


@pytest.fixture()
def doc_json(tmp_path):
    """真实的小 document.json（键取自 metadata.doi——与 `_assemble_kb` 同一约定）。"""
    p = tmp_path / "document.json"
    p.write_text(json.dumps({"metadata": {"doi": "10.1/a"}, "paragraphs": []},
                            ensure_ascii=False), encoding="utf-8")
    return str(p)


class _FakeKb:
    def __init__(self, note_exists: bool = False, level: str = "L2", boom: str = ""):
        self._note_exists = note_exists
        self._level = level
        self._boom = boom
        self.calls: list[tuple] = []

    def _need_compiler(self):
        note_exists = self._note_exists

        class _P:
            @staticmethod
            def exists():
                return note_exists

        class _C:
            @staticmethod
            def _note_path(_key):
                return _P()
        return _C()

    def value_score(self, key):
        return {"level": self._level}

    def conversation_compile(self, key, levels=("L1",), translate=True, l3=False):
        if self._boom:
            raise RuntimeError(self._boom)
        self.calls.append((key, tuple(levels), translate, l3))
        return {"status": "done", "conversation": True, "calls": 2}


def _patch(monkeypatch, kb, provider_id="p_P_P_3FEA2D40"):
    monkeypatch.setattr("app.services.container.get_kbapi", lambda: kb, raising=True)
    monkeypatch.setattr(
        "app.services.container.get_settings_service",
        lambda: type("S", (), {"get_active_provider":
                               lambda self, masked=False: {"id": provider_id, "name": "智谱"}})(),
        raising=True)


def _mgr(monkeypatch, engine=None):
    mgr = ts.TaskManager.__new__(ts.TaskManager)      # 不跑 __init__
    mgr.engine = engine or type("E", (), {
        "combined_translate": lambda self, *a, **kw: {"ok": True}})()
    return mgr


def test_conversation_flow_runs_for_non_deepseek(monkeypatch, doc_json):
    kb = _FakeKb(level="L2")
    _patch(monkeypatch, kb)
    mgr = _mgr(monkeypatch)
    assert mgr._try_conversation_flow({"id": 1}, doc_json) is True
    assert kb.calls == [("10.1/a", ("L1", "L2"), True, False)]


def test_conversation_flow_passes_l3_flag(monkeypatch, doc_json):
    kb = _FakeKb(level="L3")
    _patch(monkeypatch, kb)
    mgr = _mgr(monkeypatch)
    assert mgr._try_conversation_flow({"id": 1}, doc_json) is True
    assert kb.calls[0][1] == ("L1", "L2") and kb.calls[0][3] is True


def test_conversation_flow_skips_deepseek_official(monkeypatch, doc_json):
    kb = _FakeKb(level="L2")
    _patch(monkeypatch, kb, provider_id="deepseek")
    mgr = _mgr(monkeypatch)
    assert mgr._try_conversation_flow({"id": 1}, doc_json) is False
    assert kb.calls == []


def test_conversation_flow_skips_already_compiled(monkeypatch, doc_json):
    kb = _FakeKb(note_exists=True)
    _patch(monkeypatch, kb)
    mgr = _mgr(monkeypatch)
    assert mgr._try_conversation_flow({"id": 1}, doc_json) is False
    assert kb.calls == []


def test_conversation_flow_falls_back_on_error(monkeypatch, doc_json):
    kb = _FakeKb(boom="模拟失败")
    _patch(monkeypatch, kb)
    mgr = _mgr(monkeypatch)
    assert mgr._try_conversation_flow({"id": 1}, doc_json) is False


def test_conversation_flow_env_kill_switch(monkeypatch, doc_json):
    kb = _FakeKb()
    _patch(monkeypatch, kb)
    monkeypatch.setenv("PAPERAGENT_CONVO_FLOW", "0")
    mgr = _mgr(monkeypatch)
    assert mgr._try_conversation_flow({"id": 1}, doc_json) is False


def test_convo_key_prefers_document_doi(monkeypatch, doc_json):
    """键必须来自 document.json 的 metadata.doi（papers 表 doi 为 NULL 时也要能用）。"""
    mgr = _mgr(monkeypatch)
    assert mgr._convo_key({"id": 1}, doc_json) == "10.1/a"


def test_kbmeta_wrapper_signature_matches_paperkb():
    """**包装层签名契约**（2026-09-13 事故：kbmeta 包一层时漏了 `l3` 参数，
    调用方传 l3 直接 `unexpected keyword argument` → 被宽 except 吞成静默回退）。

    规则：`KbMetaService.conversation_compile` 的关键字参数必须**覆盖** paperkb 侧同名函数的
    关键字参数（名字与默认值一致），且能逐参透传。
    """
    import inspect

    from app.services.kbmeta_service import KbMetaService
    from paperkb import convo as _convo

    p_params = {n: p for n, p in
                inspect.signature(_convo.conversation_compile).parameters.items()
                if n != "self" and p.kind is inspect.Parameter.KEYWORD_ONLY
                or p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD}
    w_params = inspect.signature(KbMetaService.conversation_compile).parameters
    missing = [n for n in p_params if n not in ("key",) and n not in w_params]
    assert not missing, f"包装层漏参：{missing}"
    for name in p_params:
        if name in w_params and p_params[name].default is not inspect.Parameter.empty:
            assert w_params[name].default == p_params[name].default, f"{name} 默认值不一致"


def test_kbmeta_wrapper_forwards_l3(monkeypatch):
    """包一层必须把 l3 透传到 paperkb（否则 L3 永远不触发）。"""
    from app.services.kbmeta_service import KbMetaService

    seen: dict = {}

    def _fake_convo(key, levels=("L1",), translate=True, l3=False, context="compile"):
        seen.update({"key": key, "levels": levels, "translate": translate, "l3": l3,
                     "context": context})
        return {"ok": True}

    import paperkb.convo as _c

    monkeypatch.setattr(_c, "conversation_compile", _fake_convo, raising=True)
    svc = KbMetaService.__new__(KbMetaService)
    svc._ready = True                                   # 跳过 _ensure 的真实初始化
    monkeypatch.setattr(KbMetaService, "_ensure", lambda self: None, raising=True)
    out = svc.conversation_compile("10.1/a", levels=("L1", "L2"), translate=True, l3=True)
    assert out == {"ok": True}
    assert seen == {"key": "10.1/a", "levels": ("L1", "L2"), "translate": True,
                    "l3": True, "context": "compile"}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
