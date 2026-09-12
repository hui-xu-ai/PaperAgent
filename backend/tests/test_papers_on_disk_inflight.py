# -*- coding: utf-8 -*-
"""用户反馈修复（2026-09-12）单测：/api/papers 的 on_disk 在**解析进行中**不得判失效。

现象：导入 PDF 开始解析后，文献库会短暂冒出「⚠ 产物已丢失 / 🧹 清理这些记录」
（前端 5s 轮询 loadPapers，于是全程反复闪）。
根因：解析中 `papers.doc_json` 仍是空串（解析成功才写入）⇒ `on_disk` 恒 False，
而 False 的语义在前端是"产物已被手删/移走"。
修法：任务在跑（task.state=pending/running 或 paper.status=pending/parsing）且尚无
doc_json 时，视为"还没生成"而非"已丢失"。

不碰真实库：FakeStore/FakeEngine 替换 container 单例。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.papers as papers_api
from app.services import container


class FakeStore:
    def __init__(self, rows, tasks=None):
        self.rows = list(rows)
        self.tasks = tasks or {}

    def list_papers(self, limit=50):
        return self.rows[:limit]

    def latest_task(self, paper_id, kind):
        return self.tasks.get(paper_id)


class FakeEngine:
    def doc_summary(self, doc_json):
        return {}


def _row(pid, status="pending", doc_json=""):
    return {"id": pid, "title": f"p{pid}.pdf", "status": status, "error": "",
            "doc_json": doc_json, "pdf_name": f"p{pid}.pdf",
            "created_at": "2026-01-01 00:00:00", "md_template": "", "parse_source": "",
            "pipeline_mode": "parse_compile"}


@pytest.fixture()
def make(monkeypatch):
    def _make(rows, tasks=None):
        monkeypatch.setattr(container, "_store", FakeStore(rows, tasks))
        monkeypatch.setattr(container, "_engine", FakeEngine())
        app = FastAPI()
        app.include_router(papers_api.router)
        return TestClient(app)
    return _make


def test_running_parse_is_not_reported_as_lost(make):
    """解析进行中（doc_json 还没写）→ on_disk 不得为 False（否则前端闪失效提示）。"""
    client = make([_row(1, status="parsing")],
                  {1: {"state": "running", "progress": 20}})
    item = client.get("/api/papers").json()["papers"][0]
    assert item["on_disk"] is True, '解析中不能判「产物已丢失」'
    assert item["task_state"] == "running"


def test_pending_task_is_not_reported_as_lost(make):
    """刚入队（pending）同理。"""
    client = make([_row(2, status="pending")], {2: {"state": "pending", "progress": 5}})
    assert client.get("/api/papers").json()["papers"][0]["on_disk"] is True


def test_finished_but_missing_product_is_reported_as_lost(make):
    """解析已完成、doc_json 指向的文件真的没了 → 仍必须报失效（原来的正确行为不能丢）。"""
    client = make([_row(3, status="parsed", doc_json=r"D:\nope\library\x\document.json")],
                  {3: {"state": "done", "progress": 100}})
    item = client.get("/api/papers").json()["papers"][0]
    assert item["on_disk"] is False


def test_no_task_and_no_doc_json_still_lost(make):
    """没有任务记录（历史遗留）且无 doc_json → 保持失效判定。"""
    client = make([_row(4, status="parsed")], {})
    assert client.get("/api/papers").json()["papers"][0]["on_disk"] is False
