# -*- coding: utf-8 -*-
"""T5：删除论文导入记录（DELETE /api/papers/{paper_id}）单测。

覆盖：
- 删除接口返回 200 + 级联删除 tasks/sessions/messages。
- 删除后 find_paper_by_md5 不再命中 → 同 md5 可重新创建（解除"该 PDF 已处理过"）。
- store.delete_paper 幂等（已删除 → 0 计数，安全）。
- 论文不存在 → 404。

真实 SQLite（conftest store fixture），不触碰真实引擎；最小 app 仅挂 papers 路由。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.papers as papers_api
from app.services import container


class FakeEventBus:
    """记录 publish，供删除路由调用（不触发真实事件订阅）。"""

    def __init__(self):
        self.calls: list[tuple] = []

    def publish(self, *args, **kwargs):
        self.calls.append(args)


@pytest.fixture()
def env(store, monkeypatch):
    """最小 app + 真实 Store/FakeEventBus；monkeypatch 容器单例自动恢复。"""
    monkeypatch.setattr(container, "_store", store)
    monkeypatch.setattr(container, "_event_bus", FakeEventBus())
    app = FastAPI()
    app.include_router(papers_api.router)
    return TestClient(app), store


def _make_translated_paper(store, md5="md5-abc"):
    """造一篇已处理的论文（含 task/session/message），并置为 translated。"""
    pid = store.create_paper(r"input\a.pdf", title="Test Paper", pdf_md5=md5)
    store.update_paper(pid, status="translated", doc_json=r"output\x\document.json")
    store.create_task(pid, "pipeline")
    sid = store.create_session(pid, kind="paper")
    store.add_message(sid, "user", "问题", 5)
    store.add_message(sid, "assistant", "答案", 10)
    return pid


def test_delete_import_record(env):
    client, store = env
    pid = _make_translated_paper(store)
    assert store.find_paper_by_md5("md5-abc")["id"] == pid

    r = client.delete(f"/api/papers/{pid}")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True and j["paper_id"] == pid
    # 级联删除行数
    assert j["removed_tasks"] == 1
    assert j["removed_sessions"] == 1
    assert j["removed_messages"] == 2

    # store 行删除；md5 不再命中
    assert store.get_paper(pid) is None
    assert store.find_paper_by_md5("md5-abc") is None
    assert store.list_tasks(pid) == []
    assert store.list_sessions(pid) == []


def test_reimport_same_md5(env):
    """删除后同 md5 重新 create 成功（模拟重新导入）。"""
    client, store = env
    pid = _make_translated_paper(store)
    assert client.delete(f"/api/papers/{pid}").status_code == 200
    assert store.find_paper_by_md5("md5-abc") is None

    # 重新导入：同 md5 新建成功，且能查到原记录（不被"已处理"拦截）
    new_pid = store.create_paper(r"input\b.pdf", title="Reimport", pdf_md5="md5-abc")
    assert new_pid == 2
    found = store.find_paper_by_md5("md5-abc")
    assert found is not None and found["id"] == new_pid


def test_delete_missing_404(env):
    client, _ = env
    assert client.delete("/api/papers/999").status_code == 404


def test_store_delete_idempotent(env):
    """store.delete_paper 幂等：再删一次返回全 0 计数，不报错。"""
    client, store = env
    pid = _make_translated_paper(store)
    assert client.delete(f"/api/papers/{pid}").status_code == 200
    again = store.delete_paper(pid)
    assert again["removed"] == 0
    assert again["removed_tasks"] == 0
    assert again["removed_sessions"] == 0
    assert again["removed_messages"] == 0
