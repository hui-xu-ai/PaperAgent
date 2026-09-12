# -*- coding: utf-8 -*-
"""问答稳定前缀的**取源**回归测试（2026-09-12 用户实测修复）。

实测现象：同一篇已编译文献首次提问 **缓存命中 0**。根因是"编译读 kb 快照、问答读
library 正本"，而复核写回会重写 library ⇒ 两份 `shared_ctx` 从第 317 个字符分叉。
本测试锁定：`_paper_shared_ctx` 必须取 `kbmeta.shared_doc_json()` 返回的那份
（kb 优先），即与编译/翻译同源；kb 无副本时才回退 `papers.doc_json`。
"""
from __future__ import annotations

import json

from app.services.chat_service import ChatService


class _StubSettings:
    paper_fulltext_prefix_chars = 60000


class _StubKbMeta:
    def __init__(self, shared: str = ""):
        self.shared = shared

    def shared_doc_json(self, key: str) -> str:
        return self.shared


def _svc(shared: str) -> ChatService:
    svc = object.__new__(ChatService)
    svc.settings = _StubSettings()
    svc._kbmeta = _StubKbMeta(shared)          # noqa: SLF001 - 直接注入替身
    return svc


def _write_doc(path, doi: str, body: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "metadata": {"doi": doi, "title": "T"},
        "paragraphs": [{"para_id": "P001", "section": "R", "text_en": body}],
    }, ensure_ascii=False), encoding="utf-8")
    return path


def test_prefix_uses_kb_snapshot_not_library(tmp_path):
    """kb 快照与 library 正本内容不同 → 前缀必须取 kb 快照（= 编译实际发送的那份）"""
    lib = _write_doc(tmp_path / "library" / "D" / "document.json", "10.1/a", "LIBRARY text")
    kb = _write_doc(tmp_path / "kb" / "D" / "document.json", "10.1/a", "KB SNAPSHOT text")
    paper = {"doc_json": str(lib), "id": 1}

    ctx = _svc(str(kb))._paper_shared_ctx(paper)

    assert "KB SNAPSHOT text" in ctx and "LIBRARY text" not in ctx


def test_prefix_falls_back_to_library_without_kb_copy(tmp_path):
    """未编译/未纳入 kb → 回退 papers.doc_json（提问照样带全文）"""
    lib = _write_doc(tmp_path / "library" / "D" / "document.json", "10.1/a", "LIBRARY text")
    paper = {"doc_json": str(lib), "id": 1}

    ctx = _svc("")._paper_shared_ctx(paper)

    assert "LIBRARY text" in ctx


def test_prefix_disabled_when_cfg_zero(tmp_path):
    lib = _write_doc(tmp_path / "library" / "D" / "document.json", "10.1/a", "LIBRARY text")
    svc = _svc(str(lib))
    svc.settings = type("S", (), {"paper_fulltext_prefix_chars": 0})()
    assert svc._paper_shared_ctx({"doc_json": str(lib)}) == ""


def test_prefix_not_truncated_by_default(tmp_path):
    """批3 修复：cap=-1（新默认）= **不截断**——问答前缀与编译侧发送的 `shared_ctx` 全文
    逐字节一致，否则命中上限会被白白砍掉（实测旧默认 60000 而全文 69181 ⇒ 少共享 ~2,312 token）。"""
    body = "A" * 70_000                     # 超过旧默认 60000
    doc = _write_doc(tmp_path / "library" / "D" / "document.json", "10.1/a", body)
    svc = _svc(str(doc))
    svc.settings = type("S", (), {"paper_fulltext_prefix_chars": -1})()

    from paperkb.context import shared_ctx
    from paperkb.doc import read_document

    full = shared_ctx(read_document(str(doc)))
    got = svc._paper_shared_ctx({"doc_json": str(doc)})
    assert got == full and len(got) > 60_000


def test_prefix_truncated_when_cap_positive(tmp_path):
    """显式正数仍按字符截断（仅明确要压 token 时使用；会牺牲与编译侧的前缀一致性）。"""
    doc = _write_doc(tmp_path / "library" / "D" / "document.json", "10.1/a", "A" * 70_000)
    svc = _svc(str(doc))
    svc.settings = type("S", (), {"paper_fulltext_prefix_chars": 500})()

    assert len(svc._paper_shared_ctx({"doc_json": str(doc)})) == 500
