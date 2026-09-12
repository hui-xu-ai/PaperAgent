# -*- coding: utf-8 -*-
"""M5-C assemble 单测：解析完成后**不再复制**原文层进 kb（2026-09-11 用户模型）；
只登记目录映射 + 视 auto_compile 入队 L1；真正"纳入 kb"发生在编译入口（见
packages/paperkb/tests/test_compile_ensure_source.py）。仅解析模式不触发。"""
from __future__ import annotations

import json

from app.services import kbmeta_service
from app.services.task_service import TaskManager


class _StubKB:
    def __init__(self):
        self.calls = []

    def source_sync(self, doi, force=False):
        self.calls.append((doi, force))
        return {"doi": doi, "copied": ["en.md"], "skipped": []}


def _tm() -> TaskManager:
    """绕过 __init__（不启动 worker 线程），仅测 _assemble_kb 纯逻辑。"""
    tm = object.__new__(TaskManager)
    tm.store = None
    return tm


def test_assemble_kb_does_not_copy_files(tmp_path, monkeypatch):
    """2026-09-11 用户模型：解析完成**不再**复制原文层进 kb（编译才是纳入入口）。

    旧行为（解析即 source_sync 复制）会产生"翻译前冻结副本"，编译读到过期正文
    （cej 实测少 87 段译文）。现在只登记映射 + 视 auto_compile 入队。
    """
    stub = _StubKB()
    monkeypatch.setattr(kbmeta_service, "get_kbmeta", lambda: stub)
    dj = tmp_path / "document.json"
    dj.write_text(json.dumps({"metadata": {"doi": "10.1000/a.1"}}), encoding="utf-8")
    _tm()._assemble_kb(str(dj))
    assert stub.calls == [], "解析完成不应再调用 source_sync（改由编译入口按需纳入）"


def test_assemble_kb_skips_without_doi(tmp_path, monkeypatch):
    stub = _StubKB()
    monkeypatch.setattr(kbmeta_service, "get_kbmeta", lambda: stub)
    dj = tmp_path / "document.json"
    dj.write_text(json.dumps({"metadata": {}}), encoding="utf-8")
    _tm()._assemble_kb(str(dj))
    assert stub.calls == [], "无 DOI 不应触发 sync"


def test_assemble_kb_failure_not_blocking(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db locked")
    monkeypatch.setattr(kbmeta_service, "get_kbmeta", boom)
    dj = tmp_path / "document.json"
    dj.write_text(json.dumps({"metadata": {"doi": "10.1000/a.1"}}), encoding="utf-8")
    _tm()._assemble_kb(str(dj))  # 不抛异常（失败仅告警）


def test_assemble_kb_triggers_compile_queue(tmp_path, monkeypatch):
    """Q5：解析/翻译完成后自动纳入 kb + 编译 L1 入队（auto_compile 默认开）。"""
    import json

    import app.services.container as _cont

    calls = []

    class _KB:
        def source_sync(self, doi, force=False):
            return {"doi": doi, "copied": ["en.md"], "skipped": []}

        def compile_queue(self, doi, level):
            calls.append((doi, level))
            return {"status": "queued", "doi": doi, "level": level}

    monkeypatch.setattr(kbmeta_service, "get_kbmeta", lambda: _KB())

    class _SettingsSvc:
        def get_auto_compile(self):
            return True

    monkeypatch.setattr(_cont, "get_settings_service", lambda: _SettingsSvc())
    dj = tmp_path / "document.json"
    dj.write_text(json.dumps({"metadata": {"doi": "10.1000/a.1"}}), encoding="utf-8")
    _tm()._assemble_kb(str(dj))
    assert calls == [("10.1000/a.1", "L1")], "应自动 compile_queue(doi, L1)"


# ------------------------------------------------- 编译入队状态透传（2026-09-11 文案如实化）


def _settings(monkeypatch, auto: bool) -> None:
    import app.services.container as _cont

    class _SettingsSvc:
        def get_auto_compile(self):
            return auto

    monkeypatch.setattr(_cont, "get_settings_service", lambda: _SettingsSvc())


def _kb_with_status(status: str):
    """返回 (kb stub, calls)：compile_queue 恒定返回给定 status。"""
    calls = []

    class _KB:
        def compile_queue(self, doi, level):
            calls.append((doi, level))
            return {"status": status, "doi": doi, "level": level}

    return _KB(), calls


def _dj(tmp_path):
    import json

    p = tmp_path / "document.json"
    p.write_text(json.dumps({"metadata": {"doi": "10.1000/a.1"}}), encoding="utf-8")
    return str(p)


def test_assemble_kb_returns_queued_status(tmp_path, monkeypatch):
    """`_assemble_kb` 须回报 compile_queue 的**真实**返回值（供完成文案使用）。"""
    kb, _ = _kb_with_status("queued")
    monkeypatch.setattr(kbmeta_service, "get_kbmeta", lambda: kb)
    _settings(monkeypatch, True)
    st = _tm()._assemble_kb(_dj(tmp_path))
    assert st["key"] == "10.1000/a.1"
    assert st["compile"] == "queued"
    assert st["result"]["status"] == "queued"


def test_assemble_kb_passes_through_skipped_done(tmp_path, monkeypatch):
    """done 幂等（含产物在）→ 状态如实透传为 skipped_done，不再谎报"已编译"。"""
    kb, _ = _kb_with_status("skipped_done")
    monkeypatch.setattr(kbmeta_service, "get_kbmeta", lambda: kb)
    _settings(monkeypatch, True)
    assert _tm()._assemble_kb(_dj(tmp_path))["compile"] == "skipped_done"


def test_assemble_kb_auto_compile_disabled(tmp_path, monkeypatch):
    """auto_compile 关闭 → 不入队，状态为 disabled（文案须提示用户可开启）。"""
    kb, calls = _kb_with_status("queued")
    monkeypatch.setattr(kbmeta_service, "get_kbmeta", lambda: kb)
    _settings(monkeypatch, False)
    st = _tm()._assemble_kb(_dj(tmp_path))
    assert st["compile"] == "disabled" and calls == []


def test_parse_compile_done_msg_is_truthful():
    """完成文案按真实状态生成（旧实现恒定"已纳入知识库并编译 L1"= 说了没做）。"""
    from app.services.task_service import parse_compile_done_msg

    assert parse_compile_done_msg("MinerU v4", {"compile": "queued"}) == \
        "解析+编译完成（来源：MinerU v4，未翻译，已入队 L1 编译（后台编译中））"
    assert "已在知识库（跳过重复编译）" in parse_compile_done_msg("x", {"compile": "skipped_done"})
    assert "⚠ 未找到解析产物 document.json，未入队编译" in \
        parse_compile_done_msg("x", {"compile": "skipped_no_doc"})
    assert "⚠ 自动编译已关闭（设置中心-解析 可开启），本次未入队" in \
        parse_compile_done_msg("x", {"compile": "disabled"})
    assert "⚠" in parse_compile_done_msg("x", None), "状态缺失不得谎报成功"
    assert "⚠" in parse_compile_done_msg("x", {"compile": "some_new_status"})



# ---------------------------------------------------------------- T6：目录↔DOI/md5 映射登记

class _MapKB:
    def __init__(self):
        self.rows = []

    def set_doi_md5_map(self, key, doi="", pdf_md5="", paper_id=0):
        self.rows.append((key, doi, pdf_md5, paper_id))


class _PaperStore:
    def __init__(self, pdf_md5=""):
        self.pdf_md5 = pdf_md5

    def get_paper(self, paper_id):
        return {"pdf_md5": self.pdf_md5} if paper_id == 7 else None


def test_write_doi_md5_map_no_doi(tmp_path, monkeypatch):
    """T6：无 DOI 文献 → 目录名 = md5(PDF 全文)；doi 存 ''，pdf_md5 取 store 权威值。"""
    import hashlib

    import app.services.kbmeta_service as kms

    kb = _MapKB()
    monkeypatch.setattr(kms, "get_kbmeta", lambda: kb)
    tm = _tm()
    tm.store = _PaperStore(pdf_md5="b" * 32)
    src = tmp_path / "real.pdf"
    src.write_bytes(b"pdf")
    dj = tmp_path / "document.json"
    dj.write_text(json.dumps({"metadata": {"doi": "", "source_pdf": str(src)}}),
                  encoding="utf-8")
    tm._write_doi_md5_map(str(dj), 7)
    assert len(kb.rows) == 1
    key, doi, pdf_md5, pid = kb.rows[0]
    assert key == hashlib.md5(b"pdf").hexdigest()
    assert doi == "" and pdf_md5 == "b" * 32 and pid == 7


def test_write_doi_md5_map_with_doi(tmp_path, monkeypatch):
    """T6：有 DOI 文献 → 目录名 = doi_to_dirname（10.xxxx_yyy），doi 原样登记。"""
    import app.services.kbmeta_service as kms

    kb = _MapKB()
    monkeypatch.setattr(kms, "get_kbmeta", lambda: kb)
    tm = _tm()
    tm.store = _PaperStore(pdf_md5="c" * 32)
    dj = tmp_path / "document.json"
    dj.write_text(json.dumps({"metadata": {"doi": "10.1000/a.1", "source_pdf": "x.pdf"}}),
                  encoding="utf-8")
    tm._write_doi_md5_map(str(dj), 7)
    assert kb.rows == [("10.1000_a.1", "10.1000/a.1", "c" * 32, 7)]


def test_write_doi_md5_map_failure_not_blocking(tmp_path, monkeypatch):
    """T6：映射写失败仅告警不抛异常（document.json 损坏等）。"""
    import app.services.kbmeta_service as kms

    def boom(*a, **k):
        raise RuntimeError("db locked")
    monkeypatch.setattr(kms, "get_kbmeta", boom)
    tm = _tm()
    tm.store = _PaperStore()
    dj = tmp_path / "document.json"
    dj.write_text(json.dumps({"metadata": {"doi": "10.1000/a.1"}}), encoding="utf-8")
    tm._write_doi_md5_map(str(dj), 7)  # 不抛异常
