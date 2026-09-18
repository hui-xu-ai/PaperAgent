# -*- coding: utf-8 -*-
"""期刊名规范化测试：进度回调、映射缓存跳过、批量回写。"""
from __future__ import annotations

import pytest

from paperlit.config import Roots
from paperlit.db import LitStore
from paperlit.ingest.normalize import normalize_journals
from paperlit.models import Paper


@pytest.fixture
def store(tmp_path):
    roots = Roots(data_dir=tmp_path / "data", lit_dir=tmp_path / "lit")
    roots.ensure()
    s = LitStore(roots)
    s.init_schema()
    return s


class _FakeMapper:
    def __init__(self, mappings=None):
        self.m = dict(mappings or {})
        self.saved = None

    def lookup(self, name):
        return self.m.get(name) or self.m.get(name.upper())

    def bulk_add_mappings(self, mappings, source="openalex"):
        self.saved = mappings
        return len(mappings)


@pytest.fixture
def two_journals(store):
    store.upsert_paper(Paper(doi="10.1/a", title="t", journal="ADV MATER"))
    store.upsert_paper(Paper(doi="10.1/b", title="t", journal="NAT COMMUN"))
    return store


def test_progress_cb_and_batch_update(two_journals, monkeypatch):
    calls = []

    def fake_fetch(doi):
        calls.append(doi)
        return {"journal": "Advanced Materials"} if doi == "10.1/a" else {"journal": ""}

    monkeypatch.setattr("paperlit.ingest.normalize.openalex.fetch_one", fake_fetch)
    progress = []
    r = normalize_journals(two_journals, rate_limit=10000, max_retries=0,
                           progress_cb=lambda c, t, name: progress.append((c, t, name)))
    assert [p[0] for p in progress] == [1, 2] and progress[0][1] == 2
    assert r["resolved"] == 1 and r["failed"] == 1 and r["updated"] == 1
    assert two_journals.get_paper("10.1/a").journal == "Advanced Materials"
    assert two_journals.get_paper("10.1/b").journal == "NAT COMMUN"


def test_mapper_cache_skips_network(two_journals, monkeypatch):
    calls = []

    def fake_fetch(doi):
        calls.append(doi)
        return {"journal": "Nature Communications"}

    monkeypatch.setattr("paperlit.ingest.normalize.openalex.fetch_one", fake_fetch)
    mapper = _FakeMapper({"ADV MATER": "Advanced Materials"})
    r = normalize_journals(two_journals, rate_limit=10000, max_retries=0,
                           journal_mapper=mapper)
    assert calls == ["10.1/b"], "已有映射的期刊不应走网络"
    assert r["resolved"] == 2 and r["updated"] == 2
    assert mapper.saved == {"NAT COMMUN": "Nature Communications"}
    assert two_journals.get_paper("10.1/a").journal == "Advanced Materials"
