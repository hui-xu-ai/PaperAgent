# -*- coding: utf-8 -*-
"""用户反馈修复（2026-09-12）单测：/api/papers 的 DOI 兜底。

背景：`doi`/`doc_title` 来自 `engine.doc_summary(doc_json)`——读的是 **library 产物**。
用户手删 `library/<dir>/` 后（"失效记录"场景）doc_summary 读不出来 → doi 变空串，
前端 `resourceKeyOf()` 随之退化成空：文献卡上的「📎 添加附件」无法预选父资源、
知识库关联也断。兜底改用**解析时登记的** `doi_md5_map`（目录名 → DOI，与磁盘无关）。

不碰真实库：FakeStore/FakeEngine 替换 container 单例；get_kbmeta 被替身接管。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.papers as papers_api
from app.services import container
from app.services import kbmeta_service

DOC = r"D:\PaperAgent\library\10.1002_adma.202407106\document.json"
FALLBACK_DOI = "10.1002/adma.202407106"


class FakeStore:
    def __init__(self, rows):
        self.rows = list(rows)

    def list_papers(self, limit=50):
        return self.rows[:limit]

    def latest_task(self, paper_id, kind):
        return None


class FakeEngineEmpty:
    """模拟 library 产物已丢失：doc_summary 什么也读不出来。"""

    def doc_summary(self, doc_json):
        return {}


class FakeEngineWithDoi:
    def doc_summary(self, doc_json):
        return {"doi": "10.1111/from-doc", "title": "FromDoc"}


class FakeKbMeta:
    def __init__(self, mapping):
        self.mapping = mapping
        self.asked: list[str] = []

    def get_doi_md5_map(self, key):
        self.asked.append(key)
        return self.mapping.get(key)


def _row(pid, doc_json):
    return {"id": pid, "title": f"p{pid}.pdf", "status": "parsed", "error": "",
            "doc_json": doc_json, "pdf_name": f"p{pid}.pdf",
            "created_at": "2026-01-01 00:00:00", "md_template": "", "parse_source": "",
            "pipeline_mode": "parse_compile"}


@pytest.fixture()
def client_factory(monkeypatch):
    def _make(engine, mapping):
        kb = FakeKbMeta(mapping)
        monkeypatch.setattr(container, "_store", FakeStore([_row(3, DOC)]))
        monkeypatch.setattr(container, "_engine", engine)
        monkeypatch.setattr(kbmeta_service, "get_kbmeta", lambda: kb)
        app = FastAPI()
        app.include_router(papers_api.router)
        return TestClient(app), kb

    return _make


def test_doi_falls_back_to_md5_map_when_doc_unreadable(client_factory):
    """产物丢了 → 用 doi_md5_map（键 = document.json 的父目录名）兜回 DOI。"""
    client, kb = client_factory(FakeEngineEmpty(),
                                {"10.1002_adma.202407106": {"doi": FALLBACK_DOI}})
    r = client.get("/api/papers")
    assert r.status_code == 200, r.text
    item = r.json()["papers"][0]
    assert item["doi"] == FALLBACK_DOI
    assert item["on_disk"] is False          # 产物确实已不在（前置条件成立）
    assert kb.asked == ["10.1002_adma.202407106"]


def test_doi_from_doc_wins_over_fallback(client_factory):
    """doc_summary 能读出 DOI → 不用兜底（不额外查映射表）。"""
    client, kb = client_factory(FakeEngineWithDoi(),
                                {"10.1002_adma.202407106": {"doi": FALLBACK_DOI}})
    item = client.get("/api/papers").json()["papers"][0]
    assert item["doi"] == "10.1111/from-doc"
    assert kb.asked == []


def test_doi_stays_empty_when_no_mapping(client_factory):
    """映射表也没有 → 保持空串，不抛异常、不影响列表返回。"""
    client, _ = client_factory(FakeEngineEmpty(), {})
    item = client.get("/api/papers").json()["papers"][0]
    assert item["doi"] == ""
