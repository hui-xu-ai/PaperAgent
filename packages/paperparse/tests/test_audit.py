#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: tests/test_audit.py
功能: T3 审计监督单元测试（JSONL 落盘 / 查询过滤 / 报告渲染 / 断点恢复）
对外接口: 无（测试）
版本: v1.0.0 (2025-xx-xx)
版本历史:
  v1.0.0 初始版本
"""
import json

from paperparse.middleware.audit import Auditor, new_run_id


def test_new_run_id():
    rid = new_run_id()
    assert len(rid) > 10
    assert "-" in rid


def test_log_and_query_and_disk(tmp_work):
    a = Auditor("run-test", tmp_work / "audit")
    a.log_event("S0", "output", summary="blocks", payload={"count": 5})
    a.log_event("S1", "error", summary="fail", payload={"code": "PAPER-0010"})
    a.log_event("S1", "warning", summary="warn")
    assert len(a.query()) == 3
    assert len(a.query(stage="S1")) == 2
    assert len(a.query(kind="error")) == 1

    lines = (tmp_work / "audit" / "events.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    first = json.loads(lines[0])
    assert first["stage"] == "S0"
    assert first["payload"]["count"] == 5


def test_load_from_disk(tmp_work):
    a = Auditor("run-x", tmp_work / "audit")
    a.log_event("S0", "info", summary="hi")
    b = Auditor("run-x", tmp_work / "audit")
    events = b.load_from_disk()
    assert len(events) == 1
    assert events[0].summary == "hi"


def test_render_report(tmp_work):
    a = Auditor("run-y", tmp_work / "audit")
    a.log_event("S0", "input", summary="pdf")
    a.log_event("S1", "degrade", summary="fallback to pymupdf", payload={"to": "pymupdf"})
    rep = a.render_report()
    assert rep.exists()
    html = rep.read_text(encoding="utf-8")
    assert "run-y" in html
    assert "fallback to pymupdf" in html


def test_log_helpers(tmp_work):
    a = Auditor("run-z", tmp_work / "audit")
    a.log_warning("S3", "PAPER-0101", "低置信段落")
    a.log_degrade("S1", "mineru 不可达", {"to": "pymupdf"})
    kinds = {e.kind for e in a.query()}
    assert kinds == {"warning", "degrade"}
