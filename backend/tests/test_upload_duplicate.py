# -*- coding: utf-8 -*-
"""重复导入检测 + 覆盖重解析单测（2026-09-12 用户拍板）。

用户要求：「同一 PDF 的重复解析与入库风险，改成**检测提醒**；如果用户坚决再次导入，
可以**覆盖**之前的结果」。
本测试锁定 `/api/papers/batch` 的两条行为：
1) md5 命中已导入篇目且 overwrite=False → 返回 `duplicates` 明细（提醒），**不创建新记录**、
   **不提交任务**（避免重复花钱解析）；
2) overwrite=True → 删除旧记录（含会话/任务级联）后重新创建并提交任务。
不碰真实库：FakeStore/FakeTasks 替换 container 单例。
"""
from __future__ import annotations

import hashlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.papers as papers_api
from app.services import container


class FakeStore:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.deleted: list[int] = []
        self.created: list[dict] = []
        self._next = 100

    # --- 上传路径用到的方法 ---
    def find_paper_by_md5(self, md5):
        for r in self.rows:
            if r.get("pdf_md5") == md5:
                return dict(r)
        return None

    def create_paper(self, pdf_path, title="", pdf_md5="", pdf_name=""):
        self._next += 1
        row = {"id": self._next, "title": title, "pdf_name": pdf_name,
               "pdf_path": pdf_path, "pdf_md5": pdf_md5, "status": "pending"}
        self.rows.append(row)
        self.created.append(row)
        return self._next

    def update_paper(self, pid, **fields):
        for r in self.rows:
            if r["id"] == pid:
                r.update(fields)

    def delete_paper(self, pid):
        self.deleted.append(int(pid))
        self.rows = [r for r in self.rows if r["id"] != pid]
        return {"deleted": True, "paper_id": pid}


class FakeTasks:
    def __init__(self):
        self.submitted: list[int] = []

    def submit_pipeline(self, pid):
        self.submitted.append(pid)


class FakeSettings:
    def __init__(self, root):
        self.engine_input_root = str(root)     # 必须指向 tmp_path，否则测试会往真实 work/upload 落文件
        # 2026-09-12 批2：上传预检读 MinerU Key（无 Key 直接 400）⇒ 替身必须带上该字段，
        # 否则与真实 Settings 契约漂移（AttributeError）。
        self.mineru_api_key = "sk-test-mineru"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    store, tasks = FakeStore(), FakeTasks()
    monkeypatch.setattr(container, "get_store", lambda: store, raising=False)
    monkeypatch.setattr(container, "get_tasks", lambda: tasks, raising=False)
    monkeypatch.setattr(container, "get_settings", lambda: FakeSettings(tmp_path / "upload"),
                        raising=False)
    monkeypatch.setattr(container, "get_event_bus", lambda: _FakeBus(), raising=False)
    app = FastAPI()
    app.include_router(papers_api.router)
    return TestClient(app), store, tasks


class _FakeBus:
    def publish(self, *a, **k):
        return None


PDF = b"%PDF-1.4 fake content for md5"
MD5 = hashlib.md5(PDF).hexdigest()


def _post(cli, overwrite=None, name="paper.pdf"):
    files = [("files", (name, PDF, "application/pdf"))]
    data = {"mode": "parse_compile"}
    if overwrite is not None:
        data["overwrite"] = "true" if overwrite else "false"
    return cli.post("/api/papers/batch", files=files, data=data)


def test_first_import_creates_record(client):
    cli, store, tasks = client
    r = _post(cli)
    assert r.status_code == 200
    body = r.json()
    assert len(body["processed"]) == 1 and body["duplicates"] == []
    assert len(store.created) == 1 and len(tasks.submitted) == 1


def test_duplicate_import_is_reported_not_silently_skipped(client):
    cli, store, tasks = client
    store.rows.append({"id": 7, "title": "paper.pdf", "pdf_name": "paper.pdf",
                       "pdf_md5": MD5, "status": "parsed", "doc_title": "A paper"})
    r = _post(cli)
    body = r.json()
    assert body["processed"] == [], "重复导入不得再建记录/再解析"
    assert len(body["duplicates"]) == 1
    d = body["duplicates"][0]
    assert d["paper_id"] == 7 and d["status"] == "parsed" and d["doc_title"] == "A paper"
    assert body["skipped"] and "待确认" in body["skipped"][0]["reason"]
    assert tasks.submitted == [] and store.deleted == []


def test_overwrite_replaces_previous_result(client):
    cli, store, tasks = client
    store.rows.append({"id": 7, "title": "paper.pdf", "pdf_name": "paper.pdf",
                       "pdf_md5": MD5, "status": "parsed"})
    r = _post(cli, overwrite=True)
    body = r.json()
    assert store.deleted == [7], "覆盖必须先删旧记录（会话/任务级联）"
    assert len(body["processed"]) == 1 and body["duplicates"] == []
    assert len(tasks.submitted) == 1 and store.created[0]["id"] != 7


def test_failed_record_is_retried_without_warning(client):
    """失败/待处理的旧记录仍按"重新处理"复用（不打扰用户）"""
    cli, store, tasks = client
    store.rows.append({"id": 9, "title": "paper.pdf", "pdf_name": "paper.pdf",
                       "pdf_md5": MD5, "status": "failed"})
    body = _post(cli).json()
    assert body["duplicates"] == [] and len(body["processed"]) == 1
    assert body["processed"][0]["paper_id"] == 9 and "上次失败" in body["processed"][0]["note"]
    assert store.deleted == []
