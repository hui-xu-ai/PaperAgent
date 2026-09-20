# -*- coding: utf-8 -*-
"""queue() done 幂等的**产物存在性**判据单测（2026-09-11 bug 修复）。

实测 bug：`logs/paperagent.log` → `自动编译入队: key=10.1002/adma.202407106
queued={'status': 'skipped_done'}`，而 knowledge_base 下该篇目录已被清理 ——
compile_jobs 状态为 done 就跳过，产物永远不重建，用户永远看不到它进知识库。
修复后语义：done **且该等级产物在磁盘上** → skipped_done；done 但产物缺失 →
重新走 find_doc 检查并 upsert_job(status="queued")。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from paperkb import api
from paperkb.config import Roots
from paperkb.doi import doi_to_dirname
from paperkb.llm import FakeLLM
from paperkb.models import PaperMeta

L1_JSON = json.dumps({
    "one_liner": "产物缺失应重建",
    "background": {"text": "b", "paras": ["P001"]},
    "method": {"text": "m", "paras": ["P002"]},
    "result": {"text": "r", "paras": []},
    "conclusion": {"text": "c", "paras": []},
    "innovation": {"text": "i", "paras": []},
    "limitation": {"text": "l", "paras": []},
    "concepts": [],
    "tags": ["test"],
}, ensure_ascii=False)

DOI = "10.1000/rebuild.1"


@pytest.fixture()
def env(tmp_path: Path):
    roots = Roots(data_dir=tmp_path / "data",
                  library_dir=tmp_path / "library",
                  kb_dir=tmp_path / "kb").ensure()
    api.init_kb(roots)
    api.configure_llm(FakeLLM())
    yield roots
    api._store = None
    api._journals = None
    api._compiler = None
    api._settings = None
    from paperkb import llm
    llm._client = None  # noqa: SLF001


def _setup_paper(roots: Roots, doi: str = DOI) -> Path:
    """入库 meta + kb/<DOI>/document.json，返回 kb 目录。"""
    api._need_store().upsert_meta(PaperMeta(  # noqa: SLF001
        doi=doi, title="Rebuild me", journal="TEST J", year="2026",
        abstract="artifact missing", source_file="test.bib"))
    d = roots.kb_dir / doi_to_dirname(doi)
    d.mkdir(parents=True, exist_ok=True)
    (d / "document.json").write_text(json.dumps({
        "metadata": {"doi": doi, "title": "Rebuild me"},
        "sections": [{"section": "Introduction", "count": 1}],
        "paragraphs": [{"para_id": "P001", "section": "Introduction",
                        "text_en": "body text.", "is_heading": False}],
    }), encoding="utf-8")
    return d


def test_done_with_artifact_skips(env):
    """① done 且产物在 → 维持 skipped_done（原行为不变）。"""
    roots = env
    _setup_paper(roots)
    from paperkb import llm as llm_mod
    llm_mod.get_llm()._responses.append(L1_JSON)  # noqa: SLF001
    assert api.compile_now(DOI, "L1")["status"] == "done"
    note = roots.kb_dir / doi_to_dirname(DOI) / "_note.md"
    assert note.exists(), "前置：L1 产物应已生成"

    q = api.compile_queue(DOI, "L1")

    assert q == {"status": "skipped_done", "doi": DOI, "level": "L1"}


def test_done_without_artifact_requeues(env):
    """② done 但产物被删 → 视为需重建，重新 queued（bug 场景）。"""
    roots = env
    _setup_paper(roots)
    from paperkb import llm as llm_mod
    llm_mod.get_llm()._responses.append(L1_JSON)  # noqa: SLF001
    api.compile_now(DOI, "L1")
    note = roots.kb_dir / doi_to_dirname(DOI) / "_note.md"
    assert api._need_store().get_job(DOI, "L1")["status"] == "done"  # noqa: SLF001

    note.unlink()  # 模拟 knowledge_base 下该篇被清理（真实 bug 成因）

    q = api.compile_queue(DOI, "L1")

    assert q == {"status": "queued", "doi": DOI, "level": "L1"}, "产物缺失必须重建"
    assert api._need_store().get_job(DOI, "L1")["status"] == "queued"  # noqa: SLF001


def test_done_without_artifact_and_without_doc_skips_no_doc(env):
    """产物缺失但仍无 document.json（bib-only）→ 仍返回 skipped_no_doc（不入队）。"""
    roots = env
    doi = "10.1000/rebuild.nodoc"
    api._need_store().upsert_meta(PaperMeta(  # noqa: SLF001
        doi=doi, title="No doc", journal="TEST J", year="2026",
        abstract="x", source_file="test.bib"))
    api._need_store().upsert_job(doi, "L1", status="done")  # noqa: SLF001

    assert api.compile_queue(doi, "L1") == {
        "status": "skipped_no_doc", "doi": doi, "level": "L1"}


def test_l2_artifact_existence(env):
    """L2（原 L3）以 _wiki.md 判定产物存在性。"""
    roots = env
    d = _setup_paper(roots)
    level = "L2"
    api._need_store().upsert_job(DOI, level, status="done")  # noqa: SLF001

    assert api.compile_queue(DOI, level)["status"] == "queued", "产物缺失应重建"

    (d / "_wiki.md").write_text("# 产物\n", encoding="utf-8")
    api._need_store().upsert_job(DOI, level, status="done")  # noqa: SLF001
    assert api.compile_queue(DOI, level)["status"] == "skipped_done", "产物在应跳过"
