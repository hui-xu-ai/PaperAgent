# -*- coding: utf-8 -*-
"""O5：QA 存入 scope=paper/global 的 backend 透传层单测。

QaSaveRequest.scope 默认 paper；/qa/save 透传 scope 到 service.save_qa；
KbMetaService.save_qa 透传 scope 到 kbapi.save_qa。
全部 monkeypatch，不触发真实知识库初始化（ROOTS 指向真实 knowledge_base，避免动存量）。
"""
from __future__ import annotations

import fastapi
import pytest

from app.api import kbmeta as kbmeta_api
from app.services.kbmeta_service import KbMetaService


def test_qa_save_request_scope_default_paper():
    r = kbmeta_api.QaSaveRequest(question="q", answer="a")
    assert r.scope == "paper"


def test_qa_save_request_accepts_global_scope():
    r = kbmeta_api.QaSaveRequest(question="q", answer="a", doi="", scope="global")
    assert r.scope == "global"


def test_qa_save_blank_question_400(monkeypatch):
    """q/a 空 → HTTPException(400)（不触达 service）。"""
    with pytest.raises(fastapi.HTTPException) as ei:
        kbmeta_api.qa_save(kbmeta_api.QaSaveRequest(question=" ", answer="a"))
    assert ei.value.status_code == 400


def test_qa_save_passes_scope_to_service(monkeypatch):
    """qa_save 把 scope 透传给 service.save_qa（paper 默认 / global 原样）。"""
    recorded: dict = {}

    class StubSvc:
        def save_qa(self, question, answer, doi="", sources=None, tags=None, scope="paper"):
            recorded.update(locals())
            return {"ok": True, "scope": scope}

    monkeypatch.setattr(kbmeta_api.container, "get_kbapi", lambda: StubSvc())

    # global 透传
    req = kbmeta_api.QaSaveRequest(question="why?", answer="because", doi="", scope="global")
    out = kbmeta_api.qa_save(req)
    assert out["scope"] == "global"
    assert recorded["scope"] == "global"
    assert recorded["question"] == "why?"
    assert recorded["answer"] == "because"
    assert recorded["doi"] == ""
    assert recorded["sources"] == []

    # paper 默认透传
    req2 = kbmeta_api.QaSaveRequest(question="q2", answer="a2", doi="10.1/x")
    out2 = kbmeta_api.qa_save(req2)
    assert out2["scope"] == "paper"
    assert recorded["scope"] == "paper"
    assert recorded["doi"] == "10.1/x"


def test_service_save_qa_passes_scope(monkeypatch):
    """KbMetaService.save_qa 把 scope 透传给 kbapi.save_qa（不触发真实 init）。"""
    import paperkb.api as kbapi

    recorded: dict = {}

    def fake_save_qa(question, answer, doi="", sources=None, tags=None, scope="paper"):
        recorded.update(locals())
        return {"ok": True, "scope": scope}

    monkeypatch.setattr(kbapi, "save_qa", fake_save_qa)
    svc = KbMetaService()
    svc._ready = True  # 跳过真实 init_kb（避免动真实 knowledge_base/.system）
    out = svc.save_qa("q3", "a3", doi="", sources=["X"], scope="global")
    assert out["scope"] == "global"
    assert recorded["scope"] == "global"
    assert recorded["doi"] == ""
    assert recorded["sources"] == ["X"]
