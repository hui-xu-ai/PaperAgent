# -*- coding: utf-8 -*-
"""存储层测试。"""
from __future__ import annotations


def test_paper_crud(store):
    pid = store.create_paper(r"input\a.pdf", "Test")
    assert store.get_paper(pid)["status"] == "pending"
    store.update_paper(pid, status="parsed", doc_json=r"output\x\document.json")
    p = store.get_paper(pid)
    assert p["status"] == "parsed"
    assert p["doc_json"].endswith("document.json")
    assert len(store.list_papers()) == 1


def test_paper_md5_dedup(store):
    """G11：pdf_md5 记录 + 按 md5 查重。"""
    pid = store.create_paper(r"input\a.pdf", "T", pdf_md5="md5-abc")
    found = store.find_paper_by_md5("md5-abc")
    assert found is not None and found["id"] == pid
    assert store.find_paper_by_md5("md5-none") is None
    assert store.find_paper_by_md5("") is None


def test_task_flow(store):
    pid = store.create_paper(r"input\a.pdf")
    tid = store.create_task(pid, "pipeline")
    store.update_task(tid, state="running", progress=40, message="翻译中")
    t = store.get_task(tid)
    assert t["state"] == "running" and t["progress"] == 40
    assert store.latest_task(pid, "pipeline")["id"] == tid


def test_messages_order_and_tokens(store):
    pid = store.create_paper(r"input\a.pdf")
    sid = store.create_session(pid)
    store.add_message(sid, "user", "问题一", 10)
    store.add_message(sid, "assistant", "回答一", 20)
    store.add_message(sid, "user", "问题二", 10)
    msgs = store.recent_messages(sid, 10)
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert store.count_messages(sid) == 3
    assert store.tokens_total(sid) == 40
    # limit 生效（取最近 2 条）
    recent2 = store.recent_messages(sid, 2)
    assert [m["role"] for m in recent2] == ["assistant", "user"]


def test_answer_cache(store):
    store.set_cached_answer("k1", "问题", "答案")
    hit = store.get_cached_answer("k1")
    assert hit["answer"] == "答案"
    assert store.get_cached_answer("k2") is None
    # 覆盖更新
    store.set_cached_answer("k1", "问题", "新答案")
    assert store.get_cached_answer("k1")["answer"] == "新答案"
