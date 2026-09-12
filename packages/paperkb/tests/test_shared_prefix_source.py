# -*- coding: utf-8 -*-
"""共享全文前缀"同源"单测（2026-09-12 用户实测修复）。

用户实测现象：同一篇已编译文献，**首次提问缓存命中 0**（14834/16205 token 全按未命中计费）。
根因：编译/翻译读 `knowledge_base/<资源>/document.json`，问答读 `papers.doc_json`（library 正本），
复核写回（`review_service` → `_post_parse_clean` → `sanitize_document`）会在编译之后重写 library
⇒ 两侧 `shared_ctx` 从**第 317 个字符**分叉（公共前缀仅 ≈82 token）。

本文件锁定修复后的四条性质：
1) `api.shared_doc_json` = 唯一取用入口：kb 优先、未纳入 kb 时回退 library；
2) kb 副本陈旧（library 更新）时 `sync_source_to_kb` **刷新**派生副本（不再"存在即跳过"）；
3) 用户手改过的 kb 文件（`kb_edited`）不被刷新覆盖（P2）；
4) L2 提示词与 L1/L3 共用同一份全文前缀（既看得到全文，又能继承 L1 缓存）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from paperkb import api
from paperkb.compile import _prompt_l2
from paperkb.config import Roots
from paperkb.context import shared_ctx
from paperkb.doc import read_document
from paperkb.imports import sync_source_to_kb


@pytest.fixture()
def env(tmp_path: Path):
    roots = Roots(data_dir=tmp_path / "data",
                  library_dir=tmp_path / "library",
                  kb_dir=tmp_path / "kb").ensure()
    api.init_kb(roots)
    yield roots
    api._store = None        # noqa: SLF001
    api._journals = None     # noqa: SLF001
    api._compiler = None     # noqa: SLF001


def _write_doc(path: Path, doi: str, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "metadata": {"doi": doi, "title": "Prefix source paper"},
        "paragraphs": [{"para_id": "P001", "section": "Results",
                        "text_en": body, "is_heading": False}],
    }, ensure_ascii=False), encoding="utf-8")
    return path


DIRNAME = "10.1000_a.1"
DOI = "10.1000/a.1"


class TestSharedDocResolver:
    def test_prefers_kb_snapshot(self, env: Roots):
        lib = _write_doc(env.library_dir / DIRNAME / "document.json", DOI, "LIBRARY text")
        kb = _write_doc(env.kb_dir / DIRNAME / "document.json", DOI, "KB snapshot text")
        assert api.shared_doc_json(DOI) == str(kb)
        assert api.shared_doc_json(DIRNAME) == str(kb), "目录名键也要命中"
        assert lib.exists()

    def test_falls_back_to_library_when_not_in_kb(self, env: Roots):
        lib = _write_doc(env.library_dir / DIRNAME / "document.json", DOI, "only in library")
        assert api.shared_doc_json(DOI) == str(lib), "未编译/未纳入 kb 的文献要能带全文提问"

    def test_missing_both_returns_empty(self, env: Roots):
        assert api.shared_doc_json("10.9999/none") == ""


class TestStaleRefresh:
    def test_refreshes_when_library_is_newer(self, env: Roots):
        lib = _write_doc(env.library_dir / DIRNAME / "document.json", DOI, "NEW library text")
        kb = _write_doc(env.kb_dir / DIRNAME / "document.json", DOI, "OLD kb text")
        import os
        os.utime(kb, (lib.stat().st_atime - 600, lib.stat().st_mtime - 600))
        r = sync_source_to_kb(DOI, env, force=False, store=api._need_store())  # noqa: SLF001
        assert "document.json" in r["refreshed"], "陈旧副本应刷新而不是跳过"
        assert "NEW library text" in kb.read_text(encoding="utf-8")

    def test_keeps_fresh_target_untouched(self, env: Roots):
        """目标比源新（用户刚编辑过）→ 不动（原冻结语义）"""
        lib = _write_doc(env.library_dir / DIRNAME / "document.json", DOI, "library text")
        kb = _write_doc(env.kb_dir / DIRNAME / "document.json", DOI, "USER EDITED")
        r = sync_source_to_kb(DOI, env, force=False, store=api._need_store())  # noqa: SLF001
        assert r["refreshed"] == []
        assert kb.read_text(encoding="utf-8").find("USER EDITED") > 0
        assert lib.exists()

    def test_user_edited_target_survives_stale_source(self, env: Roots):
        """源更新但目标被用户在阅读器里编辑过（kb_edited）→ 保留用户版本"""
        lib = _write_doc(env.library_dir / DIRNAME / "document.json", DOI, "NEW library text")
        kb = _write_doc(env.kb_dir / DIRNAME / "document.json", DOI, "USER EDITED")
        import os
        os.utime(kb, (lib.stat().st_atime - 600, lib.stat().st_mtime - 600))
        store = api._need_store()      # noqa: SLF001
        # 用户编辑标记表由 backend Store 建在同一库文件；这里直接写入同一张表
        import sqlite3
        with sqlite3.connect(store.db_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS kb_edited(path TEXT PRIMARY KEY, ts TEXT)")
            conn.execute("INSERT OR REPLACE INTO kb_edited(path, ts) VALUES(?,?)",
                         (f"{DIRNAME}/document.json", "2026-09-12T10:00:00"))
        r = sync_source_to_kb(DOI, env, force=False, store=store)
        assert r["refreshed"] == [] and "document.json" in r["skipped"]
        assert "USER EDITED" in kb.read_text(encoding="utf-8")


class TestL2PromptSharesPrefix:
    def test_l2_prompt_starts_with_shared_fulltext(self, env: Roots):
        d = _write_doc(env.library_dir / DIRNAME / "document.json", DOI, "Body paragraph one.")
        doc = read_document(d)
        prompt = _prompt_l2({"title": "Prefix source paper"}, doc, "(L1 ctx)")
        assert prompt.startswith(shared_ctx(doc)), "L2 必须与 L1/L3 同一份共享全文前缀"
        assert "(L1 ctx)" in prompt and "章节片段" in prompt

    def test_l2_and_l1_prefix_bytes_identical(self, env: Roots):
        """L2 与 L1 的公共前缀 = shared_ctx（⇒ L2 可继承 L1 建立的提示词缓存）"""
        from paperkb.compile import _prompt_l1

        d = _write_doc(env.library_dir / DIRNAME / "document.json", DOI, "Body paragraph one.")
        doc = read_document(d)
        l1 = _prompt_l1({"title": "t"}, doc, "J")
        l2 = _prompt_l2({"title": "t"}, doc, "(L1 ctx)")
        n = min(len(l1), len(l2))
        i = 0
        while i < n and l1[i] == l2[i]:
            i += 1
        assert i >= len(shared_ctx(doc)), "两条提示词的公共前缀至少覆盖共享全文块"
