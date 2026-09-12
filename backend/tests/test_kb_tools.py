# -*- coding: utf-8 -*-
"""kb_tools 单测：工具 schema 完整性 + 执行器分发/截断（stub container）。"""
from __future__ import annotations

import pytest

from backend.app.services import kb_tools
from backend.app.services.kb_tools import _trim, run_tool, tool_specs


class _Stub:
    """最小 stub：覆盖测试涉及的 kbmeta_service 方法。"""
    def recall(self, q, top_k=6):
        return [{"doi": "10.1000/abc", "file": "_note.md", "snippet": "s" * 300}]
    def list_scores(self):
        return [{"doi": "10.1000/abc", "score": 3.5, "level": "L2"}]
    def search(self, q, limit=20):
        return [{"doi": "10.1000/abc", "title": "T"}]
    def get_paper(self, doi):
        return {"doi": doi, "title": "T"} if doi == "10.1000/abc" else None
    def citations_for(self, doi):
        return {"references": [], "citing": []}
    def source_status(self, doi):
        return {"doi": doi, "library": {}, "kb": {}}
    def compile_now(self, doi, level):
        return {"doi": doi, "level": level, "status": "done"}
    def compile_queue(self, doi, level):
        return {"doi": doi, "level": level, "status": "queued"}
    def compile_queue_all(self):
        return {"queued": 1}
    def compile_process(self, limit):
        return [{"doi": "10.1000/abc", "status": "done"}]
    def compile_status(self, status):
        return [{"doi": "10.1000/abc", "level": "L1", "status": "done"}]
    def set_journal_override(self, doi, journal_name):
        return {"doi": doi, "journal_override": journal_name}
    def value_score(self, doi):
        if doi != "10.1000/abc":
            return None
        return {"score": 3.5, "level": "L2", "parts": {"if": {"value": 5.0}}}
    def bib_preview(self, path):
        return {"records": 2, "valid": 2, "no_doi": [], "doi_dups": [],
                "missing_fields": []}
    def translate_now(self, doc_json):
        return {"translated": 3, "segments": 80}
    def journals_stats(self):
        return {"jcr_rows": 1, "cas_rows": 1}
    def missing_dois(self):
        return {"missing": ["10.1000/xyz"], "queries": ["DO=(10.1000/xyz)"]}
    def bib_import(self, path):
        return {"imported": 1, "updated": 0}
    def kb_status(self):
        return [{"dir": "10.1000_abc", "en.md": True}]
    def source_sync(self, doi, force):
        return {"doi": doi, "copied": ["en.md"], "skipped": [], "force": force}
    def verify_doc(self, doi, base="kb"):
        return {"ok": True, "expected_count": 1, "found_count": 1}


@pytest.fixture()
def stub(monkeypatch):
    from backend.app.services import kbmeta_service
    monkeypatch.setattr(kbmeta_service, "get_kbmeta", lambda: _Stub())


def test_tool_specs_schema_complete():
    specs = tool_specs()
    names = [s["function"]["name"] for s in specs]
    assert len(names) == len(set(names)), "工具名重复"
    assert len(specs) >= 15, "工具数不足"
    for s in specs:
        assert s["type"] == "function"
        f = s["function"]
        assert f["name"] and f["description"]
        assert "parameters" in f and "properties" in f["parameters"]


def test_trim():
    assert _trim("x" * 900) == "x" * 800 + "…[截断 100 字符]"
    assert _trim({"a": 1}) == '{"a": 1}'
    assert len(_trim({"a": 1}, 5)) <= 60


def test_unknown_tool(stub):
    r = run_tool("no_such_tool", {})
    assert r["ok"] is False and "未知工具" in r["result"]


def test_recall_tool(stub):
    r = run_tool("kb_recall", {"query": "ipmc", "top_k": 6})
    assert r["ok"] is True
    assert "10.1000/abc" in r["result"]
    assert len(r["result"]) <= 4000


def test_compile_and_override_tools(stub):
    r = run_tool("kb_compile_now", {"doi": "10.1000/abc", "level": "L2"})
    assert r["ok"] is True and "done" in r["result"]
    r = run_tool("kb_journal_override",
                 {"doi": "10.1000/abc", "journal_name": "Nature"})
    assert r["ok"] is True and "Nature" in r["result"]


def test_write_tools_flagged_in_description(stub):
    """写操作（编译/纠正/导入/同步/翻译）描述须注明用户明确意图（防 LLM 擅自执行）。"""
    specs = {s["function"]["name"]: s["function"]["description"] for s in tool_specs()}
    for name in ("kb_compile_now", "kb_compile_queue", "kb_compile_queue_all",
                 "kb_compile_process", "kb_journal_override", "kb_import_bib",
                 "kb_source_sync", "kb_translate_paper"):
        assert "【写】" in specs[name], f"{name} 缺写操作标记"


def test_new_tools(stub):
    r = run_tool("kb_import_bib_preview", {"path": "x.bib"})
    assert r["ok"] is True and "records" in r["result"]
    r = run_tool("kb_paper_scores", {"doi": "10.1000/abc"})
    assert r["ok"] is True and "score" in r["result"]
    r = run_tool("kb_paper_scores", {"doi": "10.9999/nope"})
    assert r["ok"] is False
    # 翻译：无 document.json 时明确报错（不臆造路径）
    r = run_tool("kb_translate_paper", {"doi": "10.1000/abc"})
    assert r["ok"] is False and "document.json" in r["result"]


def test_description_length_budget(stub):
    """描述精简（省 token 注入）：单条 ≤120 字符（非硬性，超限预警）。"""
    specs = {s["function"]["name"]: s["function"]["description"] for s in tool_specs()}
    long_ones = {n: len(d) for n, d in specs.items() if len(d) > 120}
    assert len(specs) >= 18, "工具数不足（应 ≥18）"
    assert not long_ones, f"描述超长: {long_ones}"


def test_bib_import_and_missing_tools(stub):
    r = run_tool("kb_import_bib", {"path": "x.bib"})
    assert r["ok"] is True and "imported" in r["result"]
    r = run_tool("kb_missing_dois", {})
    assert r["ok"] is True and "10.1000/xyz" in r["result"]


def test_paper_detail_missing_doi(stub):
    r = run_tool("kb_paper_detail", {"doi": "10.9999/nope"})
    assert r["ok"] is False


# ---------------------------------------------------------- kb_write_report（P0 综述落盘）
def _reports_fixture(monkeypatch, tmp_path):
    from backend.app.services import kb_tools
    reports = tmp_path / "kb" / "_reports"
    monkeypatch.setattr(kb_tools, "_reports_dir", lambda: reports)
    return reports


def test_kb_write_report_chinese_title(stub, monkeypatch, tmp_path):
    """中文标题写为 _reports/<标题>.md，返回 ok/path/bytes。"""
    import json
    reports = _reports_fixture(monkeypatch, tmp_path)
    r = run_tool("kb_write_report",
                 {"title": "人工肌肉 文献综述", "content": "# 综述\n正文 [[6G01]]\n"})
    assert r["ok"] is True
    info = json.loads(r["result"])
    assert reports.exists()
    f = reports / "人工肌肉_文献综述.md"
    assert f.exists(), f"应写入 {f}"
    assert f.read_text(encoding="utf-8") == "# 综述\n正文 [[6G01]]\n"
    assert info["path"] == "人工肌肉_文献综述.md"
    assert info["bytes"] > 0
    assert info["saved"] is True


def test_kb_write_report_subpath(stub, monkeypatch, tmp_path):
    """指定相对子路径（综述/人工肌肉.md）落盘。"""
    import json
    reports = _reports_fixture(monkeypatch, tmp_path)
    r = run_tool("kb_write_report",
                 {"title": "t", "content": "c", "path": "综述/人工肌肉.md"})
    assert r["ok"] is True
    info = json.loads(r["result"])
    f = reports / "综述" / "人工肌肉.md"
    assert f.exists() and f.read_text(encoding="utf-8") == "c"
    assert info["path"] == "综述/人工肌肉.md"


def test_kb_write_report_traversal_rejected(stub, monkeypatch, tmp_path):
    """目录穿越（../ 或 越过 _reports 根）被拒绝，不落盘。"""
    reports = _reports_fixture(monkeypatch, tmp_path)
    r = run_tool("kb_write_report", {"title": "t", "content": "x", "path": "../a/b.md"})
    assert r["ok"] is False and "非法" in r["result"]
    r = run_tool("kb_write_report", {"title": "t", "content": "x", "path": "a/../../b.md"})
    assert r["ok"] is False
    # 绝对路径/穿越标题 → 落盘落在 _reports 内（文件名清洗），不逃逸
    r = run_tool("kb_write_report", {"title": "../../恶意", "content": "x"})
    assert r["ok"] is True
    written = [p for p in reports.rglob("*.md")]
    assert written and all(p.resolve().is_relative_to(reports.resolve()) for p in written)


def test_kb_write_report_flagged_write(stub):
    """kb_write_report 属写工具，描述须标注【写】+ 返回空内容拒绝。"""
    specs = {s["function"]["name"]: s["function"]["description"] for s in tool_specs()}
    assert "【写】" in specs["kb_write_report"]
    r = run_tool("kb_write_report", {"title": "t", "content": ""})
    assert r["ok"] is False
