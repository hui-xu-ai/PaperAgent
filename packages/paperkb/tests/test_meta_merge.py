# -*- coding: utf-8 -*-
"""回归（用户提问 2026-09-12）：`upsert_meta` 是整行替换，不能把"应用自维护列"清空。

背景：`upsert_meta` 用 `INSERT OR REPLACE` 写全部 23 列，来者没提供的列会回落 `PaperMeta`
默认值。bib 导入不携带 `paper_id` / `kind` / `journal_override` 三列，于是整行替换会把它们
清空 —— 其中 `journal_override` 一清，journals.db 又匹配不上，**IF 档再次丢失**。
修法：这三列空值不覆盖既有值；其余书目列仍按 bib 权威整行替换。
"""
from __future__ import annotations

import pytest

from paperkb.models import PaperMeta

DOI = "10.1002/adma.202407106"


@pytest.fixture()
def store(tmp_path):
    from paperkb import api
    from paperkb.config import Roots

    roots = Roots(data_dir=tmp_path / "data", library_dir=tmp_path / "library",
                  kb_dir=tmp_path / "kb").ensure()
    api.init_kb(roots)          # 建表/迁移（KBStore 构造本身不建表）
    yield api._need_store()     # noqa: SLF001
    for attr in ("_store", "_journals", "_compiler", "_settings"):
        setattr(api, attr, None)


def test_app_owned_columns_survive_bib_style_upsert(store):
    """先写入三列 → 再用"bib 风格"（不带这三列）upsert → 三者必须保住。"""
    store.upsert_meta(PaperMeta(doi=DOI, title="T1", journal="ADVANCED MATERIALS",
                                year="2024", paper_id=5, kind="book",
                                journal_override="ADVANCED MATERIALS"))
    rid = store.upsert_meta(PaperMeta(doi=DOI, title="T2", journal="ADVANCED MATERIALS",
                                      year="2024"))   # bib 不提供 paper_id/kind/override
    m = store.get_meta(rid)
    assert m is not None
    assert m.title == "T2", "书目列仍应按 bib 权威替换"
    assert m.paper_id == 5, "paper_id（解析篇关联）被 bib 导入清空了"
    assert m.kind == "book", "kind（资源类型）被清空了"
    assert m.journal_override == "ADVANCED MATERIALS", "journal_override 被清空 → IF 会再次丢失"


def test_explicit_values_still_override(store):
    """调用方**显式给了**新值时必须覆盖（合并只针对"空值"）。"""
    store.upsert_meta(PaperMeta(doi=DOI, title="T1", paper_id=5, kind="book",
                                journal_override="OLD J"))
    rid = store.upsert_meta(PaperMeta(doi=DOI, title="T1", paper_id=9, kind="thesis",
                                      journal_override="NEW J"))
    m = store.get_meta(rid)
    assert (m.paper_id, m.kind, m.journal_override) == (9, "thesis", "NEW J")


def test_first_write_still_sets_values(store):
    """首次写入（无旧行）不受影响。"""
    rid = store.upsert_meta(PaperMeta(doi=DOI, title="T1", paper_id=7, kind="std",
                                      journal_override="J"))
    m = store.get_meta(rid)
    assert (m.paper_id, m.kind, m.journal_override) == (7, "std", "J")
