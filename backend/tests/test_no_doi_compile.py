# -*- coding: utf-8 -*-
"""无 DOI 文献可编译单测（P0-B step4，2026-09-12）。

用户模型：**编译只需要「有正文 + 一份元数据」**，DOI 只是可选标识。
旧实现把 DOI 当主键，于是无 DOI 文献：
  - `task_service._assemble_kb` 直接 return（永远进不了知识库）；
  - `Compiler.queue` 返回 skipped_no_doc（document.json 在 library、kb 里没有）；
  - 键解析只认 `doi_to_dirname(doi)`，md5 目录名 / RID / 目录名都命不中。
本文件锁定修复后的四条性质：
1. `resource.resource_key` 把 DOI / RID / 目录名 / md5 目录统一成稳定键；
2. `resource.find_doc` 能在 library 与 kb 两侧定位 document.json；
3. `Compiler.queue` 对"document.json 只在 library"的资源**可入队**；
4. `Compiler._ensure_source` 按 md5 目录名把原文层纳入 kb（不产生平行目录）。
"""
from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from paperkb import api
from paperkb.compile import Compiler
from paperkb.config import Roots
from paperkb.db import KBStore
from paperkb.models import PaperMeta
from paperkb.resource import candidate_dirnames, find_doc, resource_key, resources_dir

MD5 = hashlib.md5(b"fake-pdf-bytes").hexdigest()
RID = "nd-" + MD5[:12]


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


def _mk_library(roots: Roots, dirname: str, *, doi: str = "", title: str = "无编号文献") -> Path:
    d = roots.library_dir / dirname
    d.mkdir(parents=True, exist_ok=True)
    (d / "en.md").write_text("# Title\n\nbody text\n", encoding="utf-8")
    (d / "document.json").write_text(
        json.dumps({"metadata": {"doi": doi, "title": title, "pdf_md5": MD5},
                    "paragraphs": [{"para_id": "P001", "text_en": "body text"}]}),
        encoding="utf-8")
    return d


# ---------------------------------------------------------------- 键解析

def test_resource_key_unifies_all_key_forms(kbsetup, roots):
    store = kbsetup._store                       # noqa: SLF001
    # 裸 DOI 与 RID 归一到同一资源键
    rid = store.upsert_meta(PaperMeta(doi="10.1000/abc.1", title="T"))
    assert rid == "doi-10.1000_abc.1"
    assert resource_key(store, "10.1000/abc.1") == rid
    assert resource_key(store, "10.1000_abc.1") == rid, "目录名写法也要命中"
    # md5 目录名（无 DOI 文献）→ nd-<指纹12>，且**不返回空串**
    store.set_doi_md5_map(MD5, doi="", pdf_md5=MD5, paper_id=7)
    assert resource_key(store, MD5) == RID
    assert resource_key(store, "", dirname=MD5) == RID


def test_candidate_dirnames_cover_doi_rid_and_md5(kbsetup):
    store = kbsetup._store                       # noqa: SLF001
    assert candidate_dirnames("doi-10.1000/abc.1", store)[-1] == "10.1000_abc.1"
    assert MD5 in candidate_dirnames(MD5, store)


def test_find_doc_on_both_sides(kbsetup, roots):
    store = kbsetup._store                       # noqa: SLF001
    _mk_library(roots, MD5)
    assert find_doc(roots.library_dir, MD5, store) is not None
    assert find_doc(roots.kb_dir, MD5, store) is None, "kb 里还没有"
    # 兜底：键与目录名无字面关系，但 document.json 的 pdf_md5 能对上
    assert find_doc(roots.kb_dir, RID, store, search_root=roots.library_dir) is not None


# ---------------------------------------------------------------- 无 DOI 入队与纳入

def test_queue_accepts_library_only_document(kbsetup, roots):
    """document.json 只在 library（未纳入 kb）也要能入队——旧实现返回 skipped_no_doc。"""
    store = kbsetup._store                       # noqa: SLF001
    _mk_library(roots, MD5)
    c = api._need_compiler()                     # noqa: SLF001
    out = c.queue(RID, "L1")
    assert out["status"] == "queued", out
    assert store.get_job(RID, "L1") is not None, "编译任务键 = 资源 RID"


def test_ensure_source_copies_md5_dir_into_kb(kbsetup, roots):
    """按 md5 目录名纳入 kb：目录名沿用 library 侧（不产生平行目录）。"""
    _mk_library(roots, MD5)
    c = api._need_compiler()                     # noqa: SLF001
    c._ensure_source(RID)                        # noqa: SLF001
    assert (roots.kb_dir / MD5 / "document.json").exists()
    assert (roots.kb_dir / MD5 / "en.md").exists()
    assert not (roots.kb_dir / RID).exists(), "不得另建 nd- 平行目录"


def test_no_doi_registration_gives_stable_key(kbsetup, roots):
    """无 DOI 文献登记：同 md5 → 同 RID（重复解析不产生第二份资源）。"""
    doc = _mk_library(roots, MD5) / "document.json"
    k1 = kbsetup.ensure_paper_registered(str(doc), paper_id=7, pdf_md5=MD5)
    k2 = kbsetup.ensure_paper_registered(str(doc), paper_id=7, pdf_md5=MD5)
    assert k1 == k2 == RID
    meta = kbsetup._store.get_meta(RID)          # noqa: SLF001
    assert meta is not None
    # 资源键 → 目录：nd- 键与 md5 目录名无字面关系，靠"内容指纹"反查
    store = kbsetup._store                       # noqa: SLF001
    hit = find_doc(roots.library_dir, RID, store, search_root=roots.library_dir)
    assert hit is not None and hit.parent == roots.library_dir / MD5


def test_doi_path_unchanged(kbsetup, roots):
    """有 DOI 时既有行为**完全不变**（键保持 DOI 形态、目录名与任务键都不受影响）。"""
    store = kbsetup._store                       # noqa: SLF001
    _mk_library(roots, "10.1000_abc.1", doi="10.1000/abc.1", title="有 DOI")
    key = kbsetup.ensure_paper_registered(
        str(roots.library_dir / "10.1000_abc.1" / "document.json"))
    assert key == "doi-10.1000_abc.1", "元数据键 = RID"
    c = api._need_compiler()                     # noqa: SLF001
    assert c.queue("10.1000/abc.1", "L1")["status"] == "queued"
    c._ensure_source("10.1000/abc.1")            # noqa: SLF001
    assert (roots.kb_dir / "10.1000_abc.1" / "document.json").exists()
    assert store.get_job("10.1000/abc.1", "L1") is not None, "DOI 键保持原样（存量兼容）"


def test_missing_everywhere_still_skips(kbsetup):
    """两侧都没有 document.json 时仍不入队（不产生垃圾任务）。"""
    c = api._need_compiler()                     # noqa: SLF001
    out = c.queue("nd-nothinghere", "L1")
    assert out["status"] == "skipped_no_doc"
