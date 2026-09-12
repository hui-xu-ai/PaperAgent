# -*- coding: utf-8 -*-
"""DOI → 元数据补全（2026-09-12）单测：解析与"只补空字段"的合并逻辑。

不连网：`_get_json` 被替换成固定响应。真实链路另由 `tools/dbg_doi_meta_probe.py`
与 `POST /api/kb-meta/meta/enrich` 验证（实测 ADVANCED MATERIALS → 3.47 分 / L2）。
"""
from __future__ import annotations

import pytest

from app.services import doi_meta as dm

CROSSREF = {"message": {
    "title": ["Reinforced Magnetic-Responsive Electro-Ionic Artificial Muscles"],
    "container-title": ["Advanced Materials"],
    "ISSN": ["0935-9648", "1521-4095"],
    "type": "journal-article",
    "is-referenced-by-count": 10,
    "published-print": {"date-parts": [[2024, 7, 1]]},
    "author": [{"given": "Zhenjin", "family": "Xu"}, {"given": "Keqi", "family": "Deng"}],
    "abstract": "<jats:title>Abstract</jats:title><jats:p>Efficient ion transport …</jats:p>",
}}

OPENALEX = {
    "title": "OA title",
    "publication_year": 2024,
    "cited_by_count": 7,
    "type": "article",
    "primary_location": {"source": {"display_name": "Advanced Materials",
                                    "issn_l": "0935-9648",
                                    "issn": ["0935-9648", "1521-4095"]}},
    "abstract_inverted_index": {"Ion": [0], "transport": [1], "works": [2]},
}


def test_clean_abstract_strips_jats_tags():
    out = dm._clean_abstract("<jats:title>Abstract</jats:title><jats:p>Hello  world</jats:p>")
    assert "<" not in out and "jats" not in out
    assert out.startswith("Hello") and "  " not in out


def test_parse_crossref_fields(monkeypatch):
    monkeypatch.setattr(dm, "_get_json", lambda url, timeout=20: CROSSREF)
    got = dm.fetch_doi_metadata("10.1002/adma.202407106")
    assert got["source"] == "crossref"
    assert got["journal"] == "Advanced Materials"
    assert got["issn"] == "0935-9648" and got["eissn"] == "1521-4095"
    assert got["year"] == "2024" and got["times_cited"] == 10
    assert got["authors"] == ["Zhenjin Xu", "Keqi Deng"]
    assert "jats" not in got["abstract"]


def test_openalex_fallback_when_crossref_empty(monkeypatch):
    """Crossref 拿不到 → 用 OpenAlex（含倒排摘要还原）。"""
    def fake(url, timeout=20):
        return OPENALEX if "openalex" in url else {}

    monkeypatch.setattr(dm, "_get_json", fake)
    got = dm.fetch_doi_metadata("10.1/x")
    assert got["source"] == "openalex"
    assert got["journal"] == "Advanced Materials"
    assert got["times_cited"] == 7
    assert got["abstract"] == "Ion transport works"


def test_all_sources_down_returns_none(monkeypatch):
    monkeypatch.setattr(dm, "_get_json", lambda url, timeout=20: None)
    assert dm.fetch_doi_metadata("10.1/x") is None


def test_empty_doi_returns_none():
    assert dm.fetch_doi_metadata("") is None


class _FakeStore:
    """最小 KBStore 替身：只提供 enrich 用到的三个方法。"""

    def __init__(self, meta=None):
        self.meta = meta
        self.written = None

    def get_meta(self, key):
        return self.meta

    def upsert_meta(self, meta, rid=""):
        self.written = meta
        return "doi-10.1002_adma.202407106"


@pytest.fixture()
def fake_env(monkeypatch):
    """替换门面单例 + 元数据源 + kbmeta 懒初始化，隔离网络与真实库。"""
    from app.services import kbmeta_service
    from paperkb import api as kbapi

    class _Kb:
        def ensure(self):
            return None

    monkeypatch.setattr(kbmeta_service, "get_kbmeta", lambda: _Kb())
    monkeypatch.setattr(dm, "_get_json", lambda url, timeout=20: CROSSREF)
    return kbapi


def test_enrich_only_fills_empty_fields(fake_env, monkeypatch):
    """已有 bib 权威值的字段**不被覆盖**；只补空字段，并标 source_file。"""
    from paperkb.models import PaperMeta

    existing = PaperMeta(doi="10.1002/adma.202407106", title="bib 权威标题",
                         journal="", year="", source_file="refs.bib", paper_id=5)
    store = _FakeStore(existing)
    monkeypatch.setattr(fake_env, "_need_store", lambda: store)

    r = dm.enrich_paper_meta("10.1002/adma.202407106", paper_id=5)
    assert r["ok"] and r["source"] == "crossref"
    w = store.written
    assert w.title == "bib 权威标题", "bib 已有标题不该被 Crossref 覆盖"
    assert w.journal == "Advanced Materials" and w.year == "2024", "空字段应被补上"
    assert w.times_cited == 10 and w.paper_id == 5
    assert w.source_file == "refs.bib", "已有来源（bib）不能被 DOI 来源覆盖"
    assert "title" in r["kept"] and "journal" in r["filled"]


def test_enrich_records_source_when_absent(fake_env, monkeypatch):
    """没有元数据行时：标 `crossref:<doi>` 作为来源（同时是评分 has_bib 的判据）。"""
    store = _FakeStore(None)
    monkeypatch.setattr(fake_env, "_need_store", lambda: store)
    r = dm.enrich_paper_meta("10.1002/adma.202407106")
    assert r["ok"]
    assert store.written.source_file == "crossref:10.1002/adma.202407106"
    assert "level" in r and "score" in r
