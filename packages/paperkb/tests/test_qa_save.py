# -*- coding: utf-8 -*-
"""save_qa 落盘位置与前置校验单测（E3 写回飞轮：问答 → 卡片）。

覆盖：
  a) 已纳入 kb 的 doi → 写入 kb/<DOI>/cards/qa-*.md 且返回 ok=True（含 notes_fts 索引）
  b) 空 doi → ok=False（不抛异常）
  c) 未纳入 kb 的 doi（无目录 / 目录无编译产物）→ ok=False
  d) scope='global' → 写 _global/cards/qa-*.md，notes_fts doi=_global（无需纳入 kb）
  e) scope='global' 忽略非空 doi（不绑定单篇）
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from paperkb import api
from paperkb.config import Roots
from paperkb.doi import doi_to_dirname
from paperkb.models import PaperMeta

DOI = "10.1000/ipmc.1"
QA_ERROR = "文献未纳入知识库，无法保存问答"


@pytest.fixture()
def env(tmp_path: Path):
    roots = Roots(data_dir=tmp_path / "data",
                  library_dir=tmp_path / "library",
                  kb_dir=tmp_path / "kb").ensure()
    api.init_kb(roots)
    yield roots
    api._store = None
    api._journals = None
    api._compiler = None
    api._settings = None


def _setup_compiled_paper(roots: Roots, doi: str = DOI) -> None:
    """入库 meta + kb/<DOI>/ 编译产物（document.json + _note.md）模拟已纳入知识库。"""
    meta = PaperMeta(doi=doi, title="3D printing of IPMC",
                     journal="SCIENCE CHINA-MATERIALS", year="2026",
                     abstract="IPMC actuators for soft robotics.",
                     source_file="test.bib")
    api._need_store().upsert_meta(meta)  # noqa: SLF001
    d = roots.kb_dir / doi_to_dirname(doi)
    d.mkdir(parents=True, exist_ok=True)
    doc = {"metadata": {"doi": doi, "title": meta.title},
           "sections": [], "paragraphs": []}
    (d / "document.json").write_text(json.dumps(doc), encoding="utf-8")
    (d / "_note.md").write_text("# 笔记\n内容已编译", encoding="utf-8")


def test_save_qa_compiled_doi_writes_card(env):
    """a) 已纳入 kb 的 doi → 写入 cards/qa-*.md 且返回 ok=True。"""
    roots = env
    _setup_compiled_paper(roots)
    r = api.save_qa("What drives IPMC?", "Ionic migration bends the actuator 169 deg.",
                    doi=DOI, sources=["P001", "P005"], tags=["actuator", "soft-robotics"])
    assert r["ok"] is True
    cards = roots.kb_dir / doi_to_dirname(DOI) / "cards"
    files = list(cards.glob("qa-*.md"))
    assert len(files) == 1, f"应恰好生成一张卡片，实际: {[f.name for f in files]}"
    assert r["path"] == f"{doi_to_dirname(DOI)}/cards/{files[0].name}"
    assert r["file"] == files[0].name
    assert r["slug"].startswith("qa-")
    assert r["scope"] == "paper"
    # 内容：卡片模板 frontmatter（type=card-qa/doi/scope/created/tags）+ 主体
    text = files[0].read_text(encoding="utf-8")
    assert "type: card-qa" in text
    assert f"doi: {DOI}" in text
    assert "scope: paper" in text
    assert "tags: [actuator, soft-robotics]" in text
    assert "# Q: What drives IPMC?" in text
    assert "Ionic migration bends" in text
    assert "## 来源" in text and "- P001" in text
    # 全局可检索：notes_fts 已按该篇 DOI 索引
    hits = api._need_store().search_notes("What drives IPMC?")
    assert any(h["doi"] == DOI for h in hits), f"索引缺失: {hits}"


def test_save_qa_global_writes_global_card(env):
    """d) scope='global'（不绑定单篇）→ 写入 _global/cards/qa-*.md，notes_fts doi=_global。

    无需文献纳入知识库（跳过 _kb_paper_compiled 校验）；frontmatter scope: global。
    """
    roots = env
    r = api.save_qa("知识库级问题", "全局知识库回答：这是不带单篇引用的知识。",
                    doi="", sources=["G001"], tags=["global-tag"], scope="global")
    assert r["ok"] is True
    assert r["scope"] == "global"
    gdir = roots.kb_dir / "_global" / "cards"
    files = list(gdir.glob("qa-*.md"))
    assert len(files) == 1, f"应恰好生成一张全局卡，实际: {[f.name for f in files]}"
    assert r["path"] == f"_global/cards/{files[0].name}"
    assert r["file"] == files[0].name
    text = files[0].read_text(encoding="utf-8")
    assert "type: card-qa" in text
    assert "doi: _global" in text
    assert "scope: global" in text
    assert "tags: [global-tag]" in text
    assert "# Q: 知识库级问题" in text
    # 全局可检索：notes_fts 以 _global 为命名空间键
    hits = api._need_store().search_notes("全局知识库回答")
    assert any(h["doi"] == "_global" for h in hits), f"全局索引缺失: {hits}"
    # _global 不要求文献纳入知识库（空库也能存）
    assert list(roots.kb_dir.glob("*"))  # 目录结构存在


def test_save_qa_global_ignores_doi(env):
    """e) scope='global' 时 doi 无论是否给空都写入 _global，不被非空 doi 绑定单篇。"""
    roots = env
    r = api.save_qa("q", "a", doi="10.9999/x.1", scope="global")
    assert r["ok"] is True
    assert r["scope"] == "global"
    assert (roots.kb_dir / "_global" / "cards" / r["file"]).exists()

def test_save_qa_empty_doi_returns_false(env):
    """b) 空 doi → ok=False（不抛异常）。"""
    r = api.save_qa("q", "a", doi="")
    assert r == {"ok": False, "error": QA_ERROR}


def test_save_qa_no_kb_dir_returns_false(env):
    """c1) 该 doi 无 kb 目录（未纳入知识库）→ ok=False。"""
    # 仅入库 meta，但无 kb/<DOI>/ 目录
    api._need_store().upsert_meta(PaperMeta(doi=DOI, title="t", year="2026",
                                            source_file="x.bib"))  # noqa: SLF001
    r = api.save_qa("q", "a", doi=DOI)
    assert r == {"ok": False, "error": QA_ERROR}


def test_save_qa_kb_dir_no_compiled_returns_false(env):
    """c2) kb 目录存在但无编译产物（空壳）→ 仍视为未纳入，ok=False。"""
    roots = env
    d = roots.kb_dir / doi_to_dirname(DOI)
    d.mkdir(parents=True, exist_ok=True)  # 只有空目录，无 document.json/en.md/_note
    r = api.save_qa("q", "a", doi=DOI)
    assert r["ok"] is False
    assert r["error"] == QA_ERROR
