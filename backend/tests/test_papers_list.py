# -*- coding: utf-8 -*-
"""E3：/api/papers 分页单测（最小 FastAPI + TestClient + monkeypatch container 单例）。

不触碰真实 DB/引擎：FakeStore/FakeEngine 替换 container 模块级单例；
最小 app 仅挂 papers 路由、无 lifespan → 不触发 init_container（不会碰真实库/启动 worker）。
走真实 HTTP 管线 → Query 默认值与参数校验（page≥1、page_size 1..200）一并覆盖。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.papers as papers_api
from app.services import container


class FakeStore:
    def __init__(self, rows):
        self.rows = list(rows)
        self.list_calls: list[int] = []

    def list_papers(self, limit=50):
        self.list_calls.append(limit)
        return self.rows[:limit]

    def latest_task(self, paper_id, kind):
        return None


class FakeEngine:
    def doc_summary(self, doc_json):
        return {"title": "SumT", "doi": "10.1/x",
                "paragraph_count": 3, "translated_paragraphs": 1}


def _row(pid, title, created_at, pdf_name="", doc_json=""):
    return {"id": pid, "title": title, "status": "pending", "error": "",
            "doc_json": doc_json, "pdf_name": pdf_name,
            "created_at": created_at, "md_template": "", "parse_source": "",
            "pipeline_mode": "full"}


@pytest.fixture()
def env(monkeypatch):
    """最小 app（仅 papers 路由）+ FakeStore/FakeEngine；monkeypatch 自动恢复。"""
    rows = [
        _row(1, "Alpha paper", "2024-01-01 00:00:00"),
        _row(2, "Beta paper", "2024-01-02 00:00:00"),            # 无 pdf_name → 回落 title
        _row(3, "Quantum dots", "2024-01-03 00:00:00"),
        _row(4, "Gamma ray", "2024-01-04 00:00:00", pdf_name="Quantum-notes.pdf"),
        _row(5, "Delta wave", "2024-01-05 00:00:00", doc_json="doc.json"),
    ]
    store = FakeStore(rows)
    monkeypatch.setattr(container, "_store", store)
    monkeypatch.setattr(container, "_engine", FakeEngine())
    app = FastAPI()
    app.include_router(papers_api.router)
    return TestClient(app), store


# ---------------------------------------------------------------- 默认与字段兼容

def test_defaults_full_fields(env):
    client, store = env
    r = client.get("/api/papers")
    assert r.status_code == 200
    j = r.json()
    assert j["page"] == 1 and j["page_size"] == 50 and j["total"] == 5
    assert len(j["papers"]) == 5
    # 分页需全量取：必须放大 store limit（不能落回默认 50）
    assert store.list_calls and store.list_calls[-1] >= 100000
    # 兼容旧调用：renderPapers 依赖的字段全部保留
    p = j["papers"][0]
    for k in ("id", "title", "status", "error", "doc_title", "doi", "filename",
              "paragraph_count", "translated_paragraphs", "task_state",
              "task_progress", "task_message", "created_at", "md_template",
              "parse_source", "pipeline_mode"):
        assert k in p, f"缺字段 {k}"
    # doc_json 存在 → doc_summary 摘要透传
    assert j["papers"][0]["doc_title"] == "SumT" and j["papers"][0]["doi"] == "10.1/x"
    # filename 字段：pdf_name 优先 / 缺失回落 title（papers 按 created_at 降序 → 末位为 id1）
    assert j["papers"][1]["filename"] == "Quantum-notes.pdf"
    assert j["papers"][4]["filename"] == "Alpha paper"


# ---------------------------------------------------------------- 参数校验

def test_param_validation(env):
    client, _ = env
    assert client.get("/api/papers?page=0").status_code == 422
    assert client.get("/api/papers?page_size=0").status_code == 422
    assert client.get("/api/papers?page_size=500").status_code == 422
    assert client.get("/api/papers?page=1&page_size=200").status_code == 200


# ---------------------------------------------------------------- 排序 / 分页

def test_sort_created_at_desc(env):
    client, _ = env
    j = client.get("/api/papers").json()
    got = [p["created_at"] for p in j["papers"]]
    assert got == sorted(got, reverse=True)
    assert [p["id"] for p in j["papers"]] == [5, 4, 3, 2, 1]


def test_pagination_slices(env):
    client, _ = env
    r1 = client.get("/api/papers?page=1&page_size=2").json()
    assert [p["id"] for p in r1["papers"]] == [5, 4] and r1["total"] == 5
    r2 = client.get("/api/papers?page=2&page_size=2").json()
    assert [p["id"] for p in r2["papers"]] == [3, 2] and r2["total"] == 5
    r3 = client.get("/api/papers?page=3&page_size=2").json()
    assert [p["id"] for p in r3["papers"]] == [1] and r3["total"] == 5
    r4 = client.get("/api/papers?page=4&page_size=2").json()
    assert r4["papers"] == [] and r4["total"] == 5 and r4["page"] == 4


# ---------------------------------------------------------------- q 过滤

def test_q_filter_case_insensitive(env):
    client, _ = env
    # 标题命中 + 文件名（pdf_name）命中，大小写不敏感
    r = client.get("/api/papers?q=QUANTUM").json()
    assert r["total"] == 2
    assert {p["id"] for p in r["papers"]} == {3, 4}
    # 过滤后再分页
    r2 = client.get("/api/papers?q=quantum&page=1&page_size=1").json()
    assert len(r2["papers"]) == 1 and r2["total"] == 2 and r2["page_size"] == 1


def test_q_matches_title_fallback(env):
    client, _ = env
    # Beta 无 pdf_name → 回落 title 匹配
    r = client.get("/api/papers?q=beta").json()
    assert [p["id"] for p in r["papers"]] == [2] and r["total"] == 1


def test_q_filter_no_match(env):
    client, _ = env
    r = client.get("/api/papers?q=%E4%B8%8D%E5%AD%98%E5%9C%A8").json()  # 不存在
    assert r["papers"] == [] and r["total"] == 0
