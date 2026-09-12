# -*- coding: utf-8 -*-
"""M5 单测：markdown 导入 + 简版 document.json 分段器 + kb 原文层四件复制 + 一致性校验。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from paperkb import api
from paperkb.config import Roots
from paperkb.doi import doi_to_dirname, normalize_doi
from paperkb.imports import (detect_doi, import_markdown, kb_status,
                             resync_source, segment_markdown, source_status,
                             sync_source_to_kb, verify_kb_doc)

MD_SAMPLE = """# Ionic liquid-enhanced Nafion-based ionic polymer sensors

Gangqiang Tang <sup>a,1</sup>, Mingfei Jiang <sup>a,1</sup>

<sup>a</sup> Hohai University

## A R T I C L E I N F O

Keywords: Ionic polymer sensor Ionic liquid

## A B S T R A C T

Flexible sensors have broad application prospects.

## 1. Introduction

In the era of digitization, humans rely on flexible sensors. <sup>[1]</sup>

![](images/F001.png)

Fig. 1. Working principle of IPS.

## 2. Results

The results are shown below.

## References

<sup>[1]</sup> M. Doyle, High-temperature proton exchange membranes.

<sup>[2]</sup> J. Lee, Multiday operation.
"""


@pytest.fixture()
def roots(tmp_path: Path) -> Roots:
    return Roots(data_dir=tmp_path / "data",
                 library_dir=tmp_path / "library",
                 kb_dir=tmp_path / "kb").ensure()


@pytest.fixture()
def store(roots: Roots):
    api.init_kb(roots)
    yield api._store  # noqa: SLF001
    api._store = None
    api._settings = None  # noqa: SLF001


# ---------------------------------------------------------------- 分段器

def test_segment_basic():
    doc = segment_markdown(MD_SAMPLE, doi="10.1016/j.cej.2025.167798")
    assert doc["metadata"]["doi"] == "10.1016/j.cej.2025.167798"
    assert doc["metadata"]["title"].startswith("Ionic liquid-enhanced")
    paras = doc["paragraphs"]
    # 标题段：is_heading=False 且保留 "# " 前缀（与真实 document.json 一致）
    assert paras[0]["text_en"].startswith("# ")
    assert paras[0]["is_heading"] is False
    # 章节标题：is_heading=True、section=剥前缀
    heads = [p for p in paras if p["is_heading"]]
    assert any(p["section"] == "A R T I C L E I N F O" for p in heads)
    assert any(p["section"] == "1. Introduction" for p in heads)
    # para_id 顺序递增
    ids = [p["para_id"] for p in paras]
    assert ids == [f"P{i:03d}" for i in range(1, len(paras) + 1)]
    # 图注 heuristic
    cap = next(p for p in paras if p["text_en"].startswith("Fig. 1."))
    assert cap["is_caption"] is True
    # 参考文献条目 → section="References"（标题段本身也算 References 区）
    refs = [p for p in paras if p["section"] == "References" and p["is_reference"]]
    assert len(refs) == 2
    assert all(p["is_reference"] for p in refs)
    # ai_summary 留空（D16：翻译/编译均不再生成；六维总结合成到 L1 编译 _note.md）
    assert doc["ai_summary"] == {}
    # sections 统计
    assert any(s["section"] == "1. Introduction" for s in doc["sections"])


def test_segment_meta_fill_and_missing_flag():
    meta = {"doi": "10.1016/j.cej.2025.167798", "title": "T",
            "journal": "Chemical Engineering Journal", "year": "2025"}
    doc = segment_markdown(MD_SAMPLE, doi=meta["doi"], meta=meta)
    assert doc["metadata"]["journal"] == "Chemical Engineering Journal"
    assert doc["metadata"]["year"] == "2025"
    assert doc["metadata"].get("meta_source") == "papers_meta"
    assert "needs_bib" not in doc["metadata"]

    doc2 = segment_markdown(MD_SAMPLE, doi="10.1000/abc")
    assert doc2["metadata"]["meta_source"] == "md_only"
    assert doc2["metadata"]["needs_bib"] is True          # "待 bib 确认"标记


def test_detect_doi():
    assert detect_doi("10.1016_j.cej.2025.167798.md") == "10.1016/j.cej.2025.167798"
    assert detect_doi("x.md", "see doi 10.1000/abc.1 here") == "10.1000/abc.1"
    assert detect_doi("savedrecs.txt") == ""
    # 下划线形态（MinerU 网页下载 后缀不含合法 DOI 字符 → 不误判）
    assert detect_doi("10.1002_adma.202407106_Mineru网页下载.md") == ""


# ---------------------------------------------------------------- 导入

def test_import_markdown_writes_library(roots: Roots):
    md = roots.data_dir / "paper.md"
    md.write_text(MD_SAMPLE, encoding="utf-8")
    r = import_markdown(md, roots, doi="10.1016/j.cej.2025.167798")
    d = roots.library_dir / "10.1016_j.cej.2025.167798"
    assert r["doi"] == "10.1016/j.cej.2025.167798"
    assert (d / "en.md").read_text(encoding="utf-8") == MD_SAMPLE
    doc = json.loads((d / "document.json").read_text(encoding="utf-8"))
    assert doc["metadata"]["doi"] == "10.1016/j.cej.2025.167798"
    assert len(doc["paragraphs"]) > 5


def test_import_markdown_no_doi_raises(roots: Roots):
    md = roots.data_dir / "no_doi.md"
    md.write_text("# Just a title\n\nBody.", encoding="utf-8")
    with pytest.raises(ValueError, match="无法识别 DOI"):
        import_markdown(md, roots)


# ---------------------------------------------------------------- kb 四件复制

def _make_library_paper(roots: Roots, doi: str = "10.1016/j.cej.2025.167798") -> Path:
    d = roots.library_dir / doi_to_dirname(doi)
    d.mkdir(parents=True, exist_ok=True)
    (d / "en.md").write_text(MD_SAMPLE, encoding="utf-8")
    (d / "document.json").write_text(
        json.dumps(segment_markdown(MD_SAMPLE, doi=doi), ensure_ascii=False),
        encoding="utf-8")
    (d / "source.pdf").write_bytes(b"pdf")
    (d / "images").mkdir(exist_ok=True)
    (d / "images" / "F001.png").write_bytes(b"png")
    return d


def test_sync_source_to_kb_four_items(roots: Roots):
    doi = "10.1016/j.cej.2025.167798"
    _make_library_paper(roots, doi)
    r = sync_source_to_kb(doi, roots)
    assert set(r["copied"]) == {"source.pdf", "en.md", "document.json", "images"}
    kb = roots.kb_dir / doi_to_dirname(doi)
    assert (kb / "source.pdf").exists() and (kb / "en.md").exists()
    assert (kb / "document.json").exists() and (kb / "images" / "F001.png").exists()
    assert r["verify"] is not None and r["verify"]["ok"] is True


def test_sync_freeze_and_force(roots: Roots):
    doi = "10.1016/j.cej.2025.167798"
    _make_library_paper(roots, doi)
    sync_source_to_kb(doi, roots)
    # 冻结：再次 sync 不覆盖（skipped）
    (roots.library_dir / doi_to_dirname(doi) / "en.md").write_text(
        "# CHANGED\n\nBody.", encoding="utf-8")
    r2 = sync_source_to_kb(doi, roots)
    assert "en.md" in r2["skipped"]
    # force（手动重新同步）→ 覆盖
    r3 = resync_source(doi, roots)
    assert "en.md" in r3["copied"]
    assert (roots.kb_dir / doi_to_dirname(doi) / "en.md").read_text(
        encoding="utf-8").startswith("# CHANGED")


def test_source_status_and_kb_status(roots: Roots):
    doi = "10.1016/j.cej.2025.167798"
    _make_library_paper(roots, doi)
    st = source_status(doi, roots)
    assert st["library"]["en.md"] is True
    assert st["kb"]["exists"] is False
    sync_source_to_kb(doi, roots)
    st2 = source_status(doi, roots)
    assert st2["kb"]["document.json"] is True
    kbs = kb_status(roots)
    assert len(kbs) == 1 and kbs[0]["note"] is False


# ---------------------------------------------------------------- 一致性校验

def test_verify_ok_roundtrip(roots: Roots):
    doi = "10.1016/j.cej.2025.167798"
    _make_library_paper(roots, doi)
    sync_source_to_kb(doi, roots)
    v = verify_kb_doc(doi, roots, base="kb")
    assert v["ok"] is True, v
    # library 侧同样通过
    assert verify_kb_doc(doi, roots, base="library")["ok"] is True


def test_verify_decorative_heading_equiv(roots: Roots):
    """装饰标题（A B S T R A C T ↔ Abstract）不判为不一致（render_clean 规则）。"""
    d = roots.kb_dir / "10.1016_j.cej.2025.167798"
    d.mkdir(parents=True, exist_ok=True)
    en = "# Title\n\n## Abstract\n\nBody text.\n"
    (d / "en.md").write_text(en, encoding="utf-8")
    doc = {"metadata": {"doi": "10.1016/j.cej.2025.167798"},
           "paragraphs": [
               {"para_id": "P001", "section": "", "text_en": "# Title"},
               {"para_id": "P002", "section": "A B S T R A C T",
                "text_en": "## A B S T R A C T", "is_heading": True},
               {"para_id": "P003", "section": "A B S T R A C T",
                "text_en": "Body text.", "is_heading": False},
           ]}
    (d / "document.json").write_text(json.dumps(doc), encoding="utf-8")
    v = verify_kb_doc("10.1016/j.cej.2025.167798", roots, base="kb")
    assert v["ok"] is True, v


def test_verify_refs_skipped_both_sides(roots: Roots):
    """doc 侧含 References 段（render 剔除）↔ en.md 无 → 仍一致。"""
    d = roots.kb_dir / "10.1016_j.cej.2025.167798"
    d.mkdir(parents=True, exist_ok=True)
    en = "# Title\n\nBody text.\n"
    (d / "en.md").write_text(en, encoding="utf-8")
    doc = {"metadata": {"doi": "10.1016/j.cej.2025.167798"},
           "paragraphs": [
               {"para_id": "P001", "section": "", "text_en": "# Title"},
               {"para_id": "P002", "section": "Body", "text_en": "Body text."},
               {"para_id": "P003", "section": "References",
                "text_en": "<sup>[1]</sup> M. Doyle, Ref.", "is_reference": True},
           ]}
    (d / "document.json").write_text(json.dumps(doc), encoding="utf-8")
    v = verify_kb_doc("10.1016/j.cej.2025.167798", roots, base="kb")
    assert v["ok"] is True, v


def test_verify_mismatch_detected(roots: Roots):
    d = roots.kb_dir / "10.1016_j.cej.2025.167798"
    d.mkdir(parents=True, exist_ok=True)
    (d / "en.md").write_text("# Title\n\nBody text A.\n", encoding="utf-8")
    doc = {"metadata": {"doi": "10.1016/j.cej.2025.167798"},
           "paragraphs": [
               {"para_id": "P001", "section": "", "text_en": "# Title"},
               {"para_id": "P002", "section": "Body", "text_en": "Body text B."},
           ]}
    (d / "document.json").write_text(json.dumps(doc), encoding="utf-8")
    v = verify_kb_doc("10.1016/j.cej.2025.167798", roots, base="kb")
    assert v["ok"] is False
    assert len(v["mismatches"]) >= 1


def test_verify_missing_files(roots: Roots):
    v = verify_kb_doc("10.1016/j.cej.2025.167798", roots, base="kb")
    assert v["ok"] is False and "缺少" in v.get("error", "")


# ---------------------------------------------------------------- api 门面

def test_api_import_markdown_end_to_end(roots: Roots, store):
    md = roots.data_dir / "10.1016_j.cej.2025.167798.md"   # 文件名带 DOI（识别链）
    md.write_text(MD_SAMPLE, encoding="utf-8")
    r = api.import_markdown(str(md))
    assert r["doi"] == "10.1016/j.cej.2025.167798"
    assert r["sync"]["verify"]["ok"] is True
    # kb 四件就位 → kb_status 可见
    assert len(api.kb_status()) == 1
    assert api.source_status(r["doi"])["kb"]["document.json"] is True
    # 一致性校验 API
    assert api.verify_kb_doc(r["doi"])["ok"] is True


def test_backfill_kb(roots: Roots, store):
    """backfill：扫 library 补齐全部 kb 原文层四件（冻结不覆盖）。"""
    from paperkb.imports import segment_markdown

    d = roots.library_dir / "10.1016_j.cej.2025.167798"
    d.mkdir(parents=True, exist_ok=True)
    (d / "en.md").write_text(MD_SAMPLE, encoding="utf-8")
    (d / "document.json").write_text(
        json.dumps(segment_markdown(MD_SAMPLE, doi="10.1016/j.cej.2025.167798")),
        encoding="utf-8")
    (d / "source.pdf").write_bytes(b"pdf")
    (d / "images").mkdir(exist_ok=True)
    (d / "images" / "F001.png").write_bytes(b"png")
    assert not (roots.kb_dir / "10.1016_j.cej.2025.167798").exists()

    r = api.backfill_kb()
    assert r["count"] == 1 and len(r["done"]) == 1
    assert r["done"][0]["verify"] is True
    kb = roots.kb_dir / "10.1016_j.cej.2025.167798"
    assert (kb / "source.pdf").exists() and (kb / "images" / "F001.png").exists()

    # 再次 backfill：冻结（无 copied，进 skipped）
    r2 = api.backfill_kb()
    assert len(r2["skipped"]) == 1 and not r2["done"]
