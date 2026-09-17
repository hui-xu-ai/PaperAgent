# -*- coding: utf-8 -*-
"""P12F 复核门控测试：待处理计数 / 解析后挂起 / 就绪批量翻译 / 设置默认值。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import Settings
from app.services.review_service import ReviewService
from app.services.store import Store


@pytest.fixture
def env(tmp_path):
    work = tmp_path / "dual"
    rules = tmp_path / "rules"
    (rules / "learned").mkdir(parents=True)
    settings = Settings(
        db_path=str(tmp_path / "t.db"),
        engine_work_root=str(tmp_path / "library"),
        dual_work_root=str(work),
        rules_dir=str(rules),
    )
    store = Store(settings.db_path)
    return {"tmp": tmp_path, "work": work, "settings": settings, "store": store}


def _make_dual(work: Path, stem: str, pending: int, total: int = 2):
    """构造 work/dual/<stem>/review.json：pending 个待处理项。"""
    d = work / stem
    d.mkdir(parents=True, exist_ok=True)
    items = []
    for i in range(total):
        handled = i >= pending
        it = {"report_idx": i, "page": 1, "dice": 0.9,
              "mineru": {"block_id": f"M{i:04d}", "kind": "body",
                         "text": f"mineru text {i}"},
              "paddleocr": {"block_id": f"P{i:04d}", "kind": "body",
                            "text": f"paddle text {i}"}}
        if handled:
            it["ai"] = {"verdict": "paddleocr", "confidence": 0.95,
                        "reason": "", "applied": True}
        else:
            it["ai"] = {"verdict": "unresolved", "confidence": 0.0,
                        "reason": "", "applied": False}
        items.append(it)
    (d / "review.json").write_text(json.dumps(
        {"count": total, "items": items, "ai": {}}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    (d / "mineru_blocks.json").write_text("[]", encoding="utf-8")
    (d / "paddleocr_blocks.json").write_text("[]", encoding="utf-8")


def test_pending_review_count(env):
    _make_dual(env["work"], "p1", pending=1, total=3)
    svc = ReviewService(env["settings"])
    paper = {"id": 1, "pdf_path": str(env["tmp"] / "p1.pdf")}
    assert svc.pending_review_count(paper) == 1
    _make_dual(env["work"], "p2", pending=0, total=3)
    assert svc.pending_review_count({"id": 2, "pdf_path": str(env["tmp"] / "p2.pdf")}) == 0
    # 无双通道产物 → 0（不阻塞）
    assert svc.pending_review_count({"id": 3, "pdf_path": str(env["tmp"] / "p3.pdf")}) == 0


def test_gate_hooks_waiting_review(env, monkeypatch):
    """解析完成且有待复核项（策略=wait）→ status=waiting_review，不进入翻译。"""
    _make_dual(env["work"], "p1", pending=1)
    store = env["store"]
    # ★2026-09-17：`_run_pipeline` 现在会先自愈/校验输入 PDF（重试入口缺陷修复），
    # 故这里的登记路径必须**真实存在**（本测试只验门控，用占位内容即可）。
    pdf = env["tmp"] / "p1.pdf"
    pdf.write_bytes(b"%PDF-1.4 test stub")
    paper_id = store.create_paper(str(pdf), title="p1")
    store.update_paper(paper_id, doc_json=str(env["tmp"] / "doc.json"),
                       parse_source="dual")

    class FakeEngine:
        def parse_pdf(self, pdf_path, **kw):   # P15：接收 cancel_check/on_wait
            return {"document_json": str(env["tmp"] / "doc.json"),
                    "parse_source": "dual", "warnings": []}
        def combined_translate(self, *a, **k):
            raise AssertionError("门控生效时不应调用翻译")
        def export(self, *a, **k):
            raise AssertionError("门控生效时不应调用导出")

    from app.services.task_service import TaskManager
    from app.services.event_bus import EventBus
    import threading
    bus = EventBus()
    # 关闭真实 worker 线程（手动驱动）
    tm = TaskManager.__new__(TaskManager)
    tm.settings = env["settings"]; tm.store = store
    tm.engine = FakeEngine(); tm.event_bus = bus
    tm._queue = None  # type: ignore[assignment]
    tm._cancel_evt = threading.Event()   # P15：__new__ 绕过 __init__，需手动补

    # 直接驱动 _run_pipeline（worker 不参与）
    monkeypatch.setattr(tm, "_translate_gate", lambda: "wait")
    monkeypatch.setattr(tm, "_pending_review", lambda p: 1)
    tm._run_pipeline(paper_id)
    assert store.get_paper(paper_id)["status"] == "waiting_review"
    assert any(e["category"] == "waiting_review" for e in bus.history())


def test_continue_translate_blocks_and_runs(env, monkeypatch):
    """续跑：仍有待复核 → 拒绝；就绪 → 翻译+导出。"""
    _make_dual(env["work"], "p1", pending=0)   # 全部已处理 → 就绪
    store = env["store"]
    doc_json = env["tmp"] / "doc.json"
    doc_json.write_text("{}", encoding="utf-8")
    paper_id = store.create_paper(str(env["tmp"] / "p1.pdf"), title="p1")
    store.update_paper(paper_id, doc_json=str(doc_json),
                       parse_source="dual", status="waiting_review")

    calls = {"translate": 0, "export": 0}

    class FakeEngine:
        def combined_translate(self, *a, **k):
            calls["translate"] += 1
        def export(self, *a, **k):
            calls["export"] += 1
            return {"en_md": "x"}

    from app.services.task_service import TaskManager
    tm = TaskManager.__new__(TaskManager)
    tm.settings = env["settings"]; tm.store = store
    tm.engine = FakeEngine(); tm.event_bus = None
    tm._queue = None  # type: ignore[assignment]
    tm._ensure_paper_session = lambda pid: None   # 免 DB 会话逻辑
    monkeypatch.setattr(tm, "_pending_review", lambda p: 0)
    import app.services.container as _cont
    monkeypatch.setattr(_cont, "llm_ready", lambda: True)
    monkeypatch.setattr(_cont, "get_guard", lambda: None)

    # 还有待复核 → 拒绝
    monkeypatch.setattr(tm, "_pending_review", lambda p: 2)
    assert "仍有 2 个待复核项" in tm.continue_translate(paper_id, force=False)
    assert calls["translate"] == 0
    # 就绪 → 执行
    monkeypatch.setattr(tm, "_pending_review", lambda p: 0)
    assert tm.continue_translate(paper_id) == "ok"
    assert calls["translate"] == 1 and calls["export"] == 1
    assert store.get_paper(paper_id)["status"] == "translated"


def test_review_queue_and_ready(env, monkeypatch):
    from app.services.task_service import TaskManager
    from app.services.review_service import ReviewService
    store = env["store"]
    # 两篇挂起：一篇待复核 1、一篇就绪 0
    _make_dual(env["work"], "p1", pending=1)
    _make_dual(env["work"], "p2", pending=0)
    id1 = store.create_paper(str(env["tmp"] / "p1.pdf"), title="p1")
    id2 = store.create_paper(str(env["tmp"] / "p2.pdf"), title="p2")
    store.update_paper(id1, doc_json=str(env["tmp"] / "d1.json"), status="waiting_review")
    store.update_paper(id2, doc_json=str(env["tmp"] / "d2.json"), status="waiting_review")

    svc = ReviewService(env["settings"])
    tm = TaskManager.__new__(TaskManager)
    tm.store = store
    monkeypatch.setattr(tm, "_pending_review",
                        lambda p: svc.pending_review_count(p))
    q = tm.review_queue()
    assert q["ready_count"] == 1 and q["pending_total"] == 1
    assert len(q["waiting"]) == 2
    # translate_ready_all：只启动就绪篇
    started = tm.translate_ready_all()
    assert started["started"] == 1 and started["ready"] == [id2]


def test_parse_settings_defaults(env):
    from app.services.settings_service import SettingsService
    svc = SettingsService(env["store"])
    p = svc.get_parse()
    assert p["translate_gate"] == "wait"
    assert p["parse_interval_sec"] == 8
    assert p["skip_review_batch"] is False
    # 保存/读取
    svc.save_parse({"mode": "dual", "ai_review": True,
                    "translate_gate": "auto", "parse_interval_sec": 30,
                    "skip_review_batch": True,
                    "paddleocr": {}})
    p2 = svc.get_parse()
    assert p2["translate_gate"] == "auto"
    assert p2["parse_interval_sec"] == 30
    assert p2["skip_review_batch"] is True
