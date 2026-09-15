# -*- coding: utf-8 -*-
"""知识库服务测试（R1 收敛）：文件管理 + 原文层同步 + V04 生成逻辑已删除的不变量。

R1：kb_service 不再生成 _note/_details/_index —— 一级/二级笔记由 paperkb Compiler
产出，索引由 paperkb regenerate_index 重建。本文件只验证保留的文件管理职能，
并断言旧 V04 生成方法（generate/backfill/_build_note/_build_details/_index_row/
_update_index）已被移除。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from conftest import ENGINE_DOC


def _make_kb(tmp_path, store, monkeypatch):
    """组装：真实 kb_service 实例（root() 指向 tmp kb 目录）+ 容器 stub。"""
    from app.config import Settings
    from app.services.engine_service import EngineService
    from app.services.kb_service import KnowledgeBaseService

    settings = Settings(db_path=str(tmp_path / "t.db"),
                        engine_work_root=str(tmp_path / "library"))
    eng = EngineService(settings)
    kb = KnowledgeBaseService(settings, store, eng)

    class StubSS:  # 设置服务 stub：默认纳入清单/副本模式
        def get_kb_path(self):
            return ""

        def get_kb_copy_mode(self):
            return "copy"

    import app.services.container as c
    monkeypatch.setattr(c, "get_settings_service", lambda: StubSS())
    monkeypatch.setattr(c, "get_store", lambda: store)

    kb_root = tmp_path / "kb"
    kb_root.mkdir()
    monkeypatch.setattr(kb, "root", lambda: kb_root)
    return kb, kb_root


def _make_library_paper(tmp_path, store, doi="10.1002_adma.202407106"):
    """在 tmp 规范库 library/<DOI>/intermediate/ 落 document.json，并登记论文。"""
    settings_path = tmp_path / "settings"
    from app.config import Settings
    from app.services.engine_service import EngineService

    settings = Settings(db_path=str(tmp_path / "t.db"),
                        engine_work_root=str(tmp_path / "library"))
    eng = EngineService(settings)
    lib = Path(settings.engine_work_root)
    doc_dir = lib / doi / "intermediate"
    doc_dir.mkdir(parents=True)
    doc = doc_dir / "document.json"
    shutil.copy2(ENGINE_DOC, doc)
    eng._inplace_export(doc)
    pid = store.create_paper(str(lib / "x.pdf"), title="t")
    store.update_paper(pid, doc_json=str(doc), status="parsed")
    return pid


# ---------------------------------------------------------------- path / 命名

def test_paper_dir_naming():
    from app.services.kb_service import KnowledgeBaseService
    kb = KnowledgeBaseService.__new__(KnowledgeBaseService)
    assert kb.paper_dir("10.1002/adma.202407106") == "10.1002_adma.202407106"
    # 空 DOI → run_id
    assert kb.paper_dir("", run_id="abc") == "run_abc"
    # 空 DOI/run_id 且无 fallback → paper 兜底
    assert kb.paper_dir("", run_id="", fallback="") == "paper"
    # fallback 净化（非法字符 → _），调用方负责提前去扩展名，这里返回净化后的名字
    assert kb.paper_dir("", run_id="", fallback="my.pdf") == "my.pdf"
    assert kb.paper_dir("", run_id="", fallback="a/b:c?.pdf") == "a_b_c_.pdf"


# ---------------------------------------------------------------- 原文层同步

def test_link_originals_syncs_source_layer(tmp_path, store, monkeypatch):
    """R1：_link_originals 只同步原文层（en.md/document.json/images/），变体不复制。"""
    from app.services.kb_service import KnowledgeBaseService

    kb, kb_root = _make_kb(tmp_path, store, monkeypatch)
    pid = _make_library_paper(tmp_path, store)
    paper = store.get_paper(pid)
    doc = Path(paper["doc_json"]).resolve()
    base = doc.parent.parent  # library/<DOI>/
    (base / "en.md").write_text("# en", encoding="utf-8")
    img_src = base / "images"
    img_src.mkdir(exist_ok=True)
    (img_src / "F001.png").write_bytes(b"png")
    # 预置 library 旧变体残留 → 不应被复制进 kb
    (base / "10.1002_adma.202407106.md").write_text("stale", encoding="utf-8")

    folder = kb_root / "10.1002_adma.202407106"
    folder.mkdir()
    kb._link_originals(paper, folder)

    assert (folder / "en.md").exists()
    assert (folder / "document.json").exists()
    assert (folder / "images" / "F001.png").exists()
    assert not (folder / "en_zh.md").exists()
    assert not (folder / "zh.md").exists()
    assert not (folder / "summary.md").exists()


# ---------------------------------------------------------------- 变体回退（P0-B）

def test_variant_reads_fall_back_to_library(tmp_path, store, monkeypatch):
    """变体（zh.md/en_zh.md）读取语义（2026-09-16 更新为方案 A）。

    · kb **缺**该文件 → 回退读 library（迁移前旧文献的中文变体只存在于 library，阅读器不能空）；
    · kb **有**该文件 → **直接读 kb**（kb 是唯一定版；旧行为"两者都有时按 mtime 取较新"会让
      读到的内容取决于文件时间戳，与编译/翻译的读写基准不一致 —— 审计 C12，已改）。
    """
    kb, kb_root = _make_kb(tmp_path, store, monkeypatch)
    folder = kb_root / "10.1002_adma.202407106"
    folder.mkdir()
    (folder / "en.md").write_text("EN", encoding="utf-8")
    # library 侧（APP_DATA_DIR 被 patch 到 tmp_path → library/ 在 tmp_path 下）
    import app.config as cfg
    monkeypatch.setattr(cfg, "APP_DATA_DIR", tmp_path)
    lib = tmp_path / "library" / "10.1002_adma.202407106"
    lib.mkdir(parents=True)
    (lib / "zh.md").write_text("中文全译", encoding="utf-8")
    (lib / "en_zh.md").write_text("EN\n中文", encoding="utf-8")

    assert kb.read_file("10.1002_adma.202407106/zh.md")["content"] == "中文全译"
    assert kb.read_file("10.1002_adma.202407106/en_zh.md")["content"] == "EN\n中文"
    names = {f["name"] for fo in kb.tree()["folders"] for f in fo["files"]}
    assert {"zh.md", "en_zh.md"} <= names, "树视图也要列出 library 侧变体"

    # kb 里也有该文件 → **kb 优先**（即使 library 那份"更新"也不改判据）
    import os
    import time

    (folder / "zh.md").write_text("定版译本", encoding="utf-8")
    os.utime(folder / "zh.md", (time.time() - 600, time.time() - 600))
    (lib / "zh.md").write_text("library 较新但非定版", encoding="utf-8")
    assert kb.read_file("10.1002_adma.202407106/zh.md")["content"] == "定版译本"


# ---------------------------------------------------------------- 文件管理

def test_file_management_ops(tmp_path, store, monkeypatch):
    """R1：保留的 create_dir/create_file/write_file/read_file/rename/delete/tree。"""
    kb, kb_root = _make_kb(tmp_path, store, monkeypatch)

    assert kb.create_dir("10.1000/abc")["created"] is True
    assert kb.create_file("10.1000/abc/note.md", content="# hi")["created"] is True
    # 重复创建报错
    with pytest.raises(FileExistsError):
        kb.create_file("10.1000/abc/note.md")

    # 读/写
    r = kb.write_file("10.1000/abc/edit.md", content="edited")
    assert r["saved"] is True
    assert kb.read_file("10.1000/abc/edit.md")["content"] == "edited"
    # 目录穿越拒绝
    with pytest.raises((ValueError, FileNotFoundError)):
        kb.read_file("../outside.md")
    with pytest.raises(ValueError):
        kb.write_file("../outside.md", "x")

    # tree
    tree = kb.tree()
    assert tree["root"] == str(kb_root)
    assert any(f["doi_dir"] == "10.1000" for f in tree["folders"])

    # rename
    kb.rename("10.1000/abc/note.md", "renamed.md")
    assert (kb_root / "10.1000" / "abc" / "renamed.md").exists()

    # delete
    kb.delete("10.1000/abc/renamed.md")
    assert not (kb_root / "10.1000" / "abc" / "renamed.md").exists()


# ---------------------------------------------------------------- 图片路径

def test_images_path_and_list(tmp_path, store, monkeypatch):
    kb, kb_root = _make_kb(tmp_path, store, monkeypatch)
    img = kb_root / "10.1000/abc/images"
    img.mkdir(parents=True)
    (img / "F001.png").write_bytes(b"png")
    (img / "F002.jpg").write_bytes(b"jpg")
    (img / "bad.txt").write_text("x", encoding="utf-8")

    assert len(kb.list_images("10.1000/abc")) == 2
    p = kb.image_path("10.1000/abc/images/F001.png")
    assert p is not None and p.exists()
    # 非图片 / 越界 → None
    assert kb.image_path("10.1000/abc/images/bad.txt") is None
    assert kb.image_path("../outside.png") is None


# ---------------------------------------------------------------- R1 不变量

def test_v04_generation_methods_removed():
    """R1 不变量：kb_service 不再有任何 _note/_details 组装与索引写入者。"""
    from app.services import kb_service
    from app.services.kb_service import KnowledgeBaseService

    gone = ("generate", "backfill", "_build_note", "_build_details",
            "_index_row", "_update_index")
    for name in gone:
        assert not hasattr(KnowledgeBaseService, name), f"V04 方法残留: {name}"
    assert not hasattr(kb_service, "NOTE_TEMPLATE"), "V04 NOTE_TEMPLATE 残留"
