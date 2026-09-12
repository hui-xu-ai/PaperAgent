# -*- coding: utf-8 -*-
"""类型（kind）过滤 + 附件 API 单测（P0-B step3 T2/T3，2026-09-12）。

覆盖 F1/A2（kind / has_attachment 过滤与计数）与附件端点（列/读/导入/越权）：
- 服务端过滤语义：all / none（无编号） / 具体类型 / has_attachment；
- 芯片计数与列表**必须一致**（含"kb 目录独有、bib 元数据未导入"的资源——
  旧行为会让芯片显示 0 篇而列表里有东西，同一屏自相矛盾）；
- 附件导入走轻量管线（落位 + 本机抽文本 + 索引），**不调 LLM**。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from paperkb import api
from paperkb.config import Roots
from paperkb.doi import doi_to_dirname, make_rid
from paperkb.models import PaperMeta

DOI_A = "10.1002/adma.202407106"
DOI_B = "10.1016/j.cej.2025.167798"


@pytest.fixture()
def roots(tmp_path: Path) -> Roots:
    return Roots(data_dir=tmp_path / "data",
                 library_dir=tmp_path / "library",
                 kb_dir=tmp_path / "kb").ensure()


@pytest.fixture()
def kbsetup(roots: Roots):
    api.init_kb(roots)
    yield api
    for attr in ("_store", "_settings", "_journals", "_compiler"):
        setattr(api, attr, None)


def _seed(store, roots: Roots) -> dict:
    """三篇资源：论文（有 SI 附件）/ 书 / 无编号；各建 library + kb 目录。"""
    paper_rid = store.upsert_meta(PaperMeta(doi=DOI_A, title="Paper A", journal="Adv Mater"))
    book_rid = store.upsert_meta(PaperMeta(rid="book__isbn-9783527345678",
                                           title="A Book", kind="book"))
    nd_rid = store.upsert_meta(PaperMeta(title="No-id note"))
    for base in (roots.library_dir, roots.kb_dir):
        (base / doi_to_dirname(DOI_A)).mkdir(parents=True, exist_ok=True)
        (base / book_rid).mkdir(parents=True, exist_ok=True)
        (base / nd_rid).mkdir(parents=True, exist_ok=True)
    api.kb_attachment_import(paper_rid, "si", "si.md", b"supporting info body", index=True)
    return {"paper": paper_rid, "book": book_rid, "nd": nd_rid}


def test_kind_filter_matches_chip_counts(kbsetup, roots):
    store = kbsetup._store                       # noqa: SLF001
    _seed(store, roots)
    chips = {c["kind"]: c["count"] for c in kbsetup.kind_options()["chips"]}

    def listed(kind: str = "", has: bool = False) -> int:
        return kbsetup.kb_list(compile_status="", page_size=100, kind=kind,
                               has_attachment=has)["total"]

    assert listed() == chips["all"], "芯片『全部』与列表 total 必须一致"
    for k, n in chips.items():
        if k in ("has_attachment",):
            assert listed(has=True) == n, f"『有附件』芯片计数 {n} 与列表不符"
        elif k != "all":
            assert listed(kind=k) == n, f"芯片 {k} 计数 {n} 与列表不符"


def test_kind_none_means_no_identifier(kbsetup, roots):
    store = kbsetup._store                       # noqa: SLF001
    ids = _seed(store, roots)
    items = kbsetup.kb_list(compile_status="", page_size=100, kind="none")["items"]
    assert [it["rid"] for it in items] == [ids["nd"]], "无编号 = RID 前缀 nd-/md5 目录名"


def test_has_attachment_filters_and_reports_count(kbsetup, roots):
    store = kbsetup._store                       # noqa: SLF001
    _seed(store, roots)
    items = kbsetup.kb_list(compile_status="", page_size=100, has_attachment=True)["items"]
    assert len(items) == 1 and items[0]["attachments"] == 1
    assert items[0]["has_attachment"] is True
    assert items[0]["kind"] == "paper"
    # 无附件资源不被选中
    other = kbsetup.kb_list(compile_status="", page_size=100, kind="book")["items"]
    assert other and other[0]["attachments"] == 0


def test_disk_only_resource_counted_in_chips(kbsetup, roots):
    """bib 未导入、磁盘有 kb 目录时，芯片计数不能是 0（否则与列表自相矛盾）。"""
    (roots.kb_dir / doi_to_dirname(DOI_B)).mkdir(parents=True, exist_ok=True)
    chips = {c["kind"]: c["count"] for c in kbsetup.kind_options()["chips"]}
    total = kbsetup.kb_list(compile_status="", page_size=100)["total"]
    assert total == 1 and chips["all"] == 1


def test_attachment_import_and_read(kbsetup, roots):
    store = kbsetup._store                       # noqa: SLF001
    rid = store.upsert_meta(PaperMeta(doi=DOI_A, title="Paper A"))
    (roots.library_dir / doi_to_dirname(DOI_A)).mkdir(parents=True, exist_ok=True)
    out = kbsetup.kb_attachment_import(rid, "review", "comments.md",
                                       b"reviewer says revise", index=True)
    assert out["ok"] and out["parented"] is True
    assert out["path"] == "review/comments.md"
    assert (roots.library_dir / doi_to_dirname(DOI_A) / "attachments" / "review" /
            "comments.md").exists()
    lst = kbsetup.kb_attachments(rid)
    assert lst["count"] == 1 and lst["files"][0]["indexed"] is True
    rd = kbsetup.kb_attachment_read(rid, "review/comments.md")
    assert rd["ok"] and "revise" in rd["text"]
    # 越权
    assert kbsetup.kb_attachment_read(rid, "../../x.md")["ok"] is False


def test_attachment_import_does_not_touch_library_products(kbsetup, roots):
    """轻量管线只写 attachments/，不动解析产物（职责边界）。"""
    store = kbsetup._store                       # noqa: SLF001
    rid = store.upsert_meta(PaperMeta(doi=DOI_A, title="Paper A"))
    d = roots.library_dir / doi_to_dirname(DOI_A)
    d.mkdir(parents=True, exist_ok=True)
    (d / "en.md").write_text("original body", encoding="utf-8")
    kbsetup.kb_attachment_import(rid, "data", "raw.csv", b"a,b\n1,2", index=True)
    assert (d / "en.md").read_text(encoding="utf-8") == "original body"


def test_make_rid_for_book_and_thesis():
    assert make_rid("book", isbn="978-3-527-34567-8").startswith("book__")
    assert make_rid("thesis", cnki="CDFD2019012345").startswith("thesis__")


def test_attachment_key_accepts_rid_and_bare_doi(kbsetup, roots):
    """三种键写法（RID / 裸 DOI / 目录名）必须落到**同一个**资源目录。

    回归：前端按 DOI 传键（`10.1002/adma…` 含 `/`）时曾落到独立根
    `attachments/<DOI>/`，用户在文献目录下看不到自己的 SI。
    """
    store = kbsetup._store                       # noqa: SLF001
    rid = store.upsert_meta(PaperMeta(doi=DOI_A, title="Paper A"))
    (roots.library_dir / doi_to_dirname(DOI_A)).mkdir(parents=True, exist_ok=True)
    out = kbsetup.kb_attachment_import(DOI_A, "si", "from-doi.md", b"bare doi key", index=True)
    assert out["parented"] is True, "裸 DOI 传参也必须解析到既有资源目录"
    assert (roots.library_dir / doi_to_dirname(DOI_A) / "attachments" / "si" /
            "from-doi.md").exists()
    out2 = kbsetup.kb_attachment_import(rid, "si", "from-rid.md", b"rid key", index=True)
    assert out2["root"] == out["root"], "两种键写法的落位根必须一致"
    assert not (roots.library_dir.parent / "attachments" / DOI_A).exists(), \
        "不得在独立根留下平行目录"
