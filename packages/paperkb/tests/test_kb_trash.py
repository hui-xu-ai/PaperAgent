# -*- coding: utf-8 -*-
"""回收站（2026-09-12 用户需求）单测：移出知识库 = 产物保留 + 退出显示/检索 + 可恢复。

用户原话："改成移除知识库即可，或者说是移除到回收站，编译产物这些都统一保留，
只是不再会被知识库检索，回收站这些文献在主界面添加一个恢复按钮。在回收站的文献不再知识库内显示。"
"""
from __future__ import annotations

import json

import pytest

from paperkb import api
from paperkb.config import Roots

DOI = "10.1000/trash.1"
DIR = "10.1000_trash.1"


@pytest.fixture()
def env(tmp_path):
    roots = Roots(data_dir=tmp_path / "data", library_dir=tmp_path / "library",
                  kb_dir=tmp_path / "kb").ensure()
    api.init_kb(roots)
    yield roots
    for attr in ("_store", "_journals", "_compiler", "_settings"):
        setattr(api, attr, None)


def _mk(roots):
    """造一篇已编译的 kb 资源（_note.md + document.json）。"""
    d = roots.kb_dir / DIR
    d.mkdir(parents=True, exist_ok=True)
    (d / "_note.md").write_text("# 笔记\ntype: paper-note\n一句话：磁响应人工肌肉。\n",
                                encoding="utf-8")
    (d / "document.json").write_text(json.dumps(
        {"metadata": {"doi": DOI, "title": "回收站测试篇"},
         "paragraphs": [{"para_id": "P001", "section": "S", "text_en": "body"}]},
        ensure_ascii=False), encoding="utf-8")
    store = api._need_store()  # noqa: SLF001
    store.index_notes(DOI, [{"filename": "_note.md",
                             "content": (d / "_note.md").read_text(encoding="utf-8")}])
    return d


def test_trash_move_hides_and_keeps_files(env):
    """移出知识库：目录搬到 `.trash/`、知识库列表不再显示、检索也摘除，**文件一个不删**。"""
    roots = env
    d = _mk(roots)
    store = api._need_store()  # noqa: SLF001
    assert store.search_notes("磁响应", doi=DOI), "前置条件：移出前应能检索到"

    r = api.kb_trash_move(DOI)
    assert r["status"] == "trashed" and r["dirname"] == DIR
    # ① 磁盘：原目录没了，但内容原样在 `.trash/`
    assert not d.exists()
    moved = roots.kb_dir / ".trash" / DIR
    assert moved.is_dir() and (moved / "_note.md").is_file()
    assert (moved / "document.json").is_file(), "产物必须全部保留"
    # ② 列表：不再显示
    keys = {it.get("doi") or it.get("rid") for it in api.kb_list()["items"]}
    assert DOI not in keys and DIR not in keys
    # ③ 检索：摘除
    assert store.search_notes("磁响应", doi=DOI) == []
    assert store.search_fulltext("磁响应") == []
    # ④ 回收站清单里有它（key 存 rid 形态 `doi-…`，dirname 用于原样搬回；恢复接口两种写法都吃）
    items = api.kb_trash_list()["items"]
    assert items and items[0]["dirname"] == DIR and items[0]["title"]
    assert api.kb_trash_restore(items[0]["key"])["status"] == "restored", "rid 形态也须能恢复"


def test_trash_restore_brings_it_back(env):
    """恢复：目录搬回、列表重新显示、索引重建（可再检索）。"""
    roots = env
    _mk(roots)
    api.kb_trash_move(DOI)
    r = api.kb_trash_restore(DOI)

    assert r["status"] == "restored"
    d = roots.kb_dir / DIR
    assert d.is_dir() and (d / "_note.md").is_file()
    store = api._need_store()  # noqa: SLF001
    assert store.search_notes("磁响应", doi=DOI), "恢复后索引必须重建"
    assert api.kb_trash_list()["items"] == []
    keys = {it.get("doi") or it.get("rid") for it in api.kb_list()["items"]}
    assert DOI in keys or DIR in keys


def test_full_reindex_skips_trash_dir(env):
    """全量重建索引**不得**把 `.trash/` 里的资源又索引回来（否则回收站失效）。"""
    roots = env
    _mk(roots)
    api.kb_trash_move(DOI)
    store = api._need_store()  # noqa: SLF001
    store.reindex_from_kb()
    assert store.search_notes("磁响应", doi=DOI) == [], "`.trash/` 被重建索引扫进去了"


def test_double_trash_is_idempotent(env):
    _mk(env)
    assert api.kb_trash_move(DOI)["status"] == "trashed"
    assert api.kb_trash_move(DOI)["status"] == "already_trashed"


def test_restore_missing_raises(env):
    with pytest.raises(ValueError):
        api.kb_trash_restore(DOI)


def test_trash_move_without_dir_still_hides_meta_row(env):
    """只有元数据行、磁盘无目录（例如产物已手删）→ 仍能登记"已移出"，列表不再显示。"""
    from paperkb.models import PaperMeta

    store = api._need_store()  # noqa: SLF001
    store.upsert_meta(PaperMeta(doi=DOI, title="只有元数据的篇"))
    r = api.kb_trash_move(DOI)
    assert r["status"] == "trashed_without_dir"
    keys = {it.get("doi") or it.get("rid") for it in api.kb_list()["items"]}
    assert DOI not in keys, "只有 meta 行的条目也必须从知识库列表消失"
