# -*- coding: utf-8 -*-
"""paperlit P2 测试：多源元数据补全（mock API，验证来源标签与合并逻辑）。"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from paperlit.config import Roots
from paperlit.db import LitStore
from paperlit.models import Paper
from paperlit.ingest.enrich import enrich_one, enrich_batch, enrich_all_pending


@pytest.fixture
def lit_store(tmp_path):
    roots = Roots(data_dir=tmp_path / "data", lit_dir=tmp_path / "lit")
    roots.ensure()
    store = LitStore(roots)
    store.init_schema()
    return store


def _oa_work(doi: str, **overrides) -> dict:
    """构造 OpenAlex 返回的 parsed dict。"""
    base = {
        "doi": doi,
        "title": "OpenAlex Title for " + doi,
        "abstract": "Abstract from OpenAlex about solid electrolytes.",
        "authors": ["Alice Smith", "Bob Jones"],
        "affiliations": ["MIT", "Stanford University"],
        "corresponding": ["Alice Smith"],
        "journal": "Nature Energy",
        "year": "2024",
        "issn": "2058-7546",
        "times_cited": 120,
        "keywords": ["battery", "electrolyte"],
        "research_areas": ["Electrochemistry"],
        "source": "openalex",
    }
    base.update(overrides)
    return base


def _cr_work(doi: str, **overrides) -> dict:
    """构造 CrossRef 返回的 parsed dict。"""
    base = {
        "doi": doi,
        "title": "CrossRef Title for " + doi,
        "abstract": "Abstract from CrossRef about polymer membranes.",
        "journal": "Advanced Materials",
        "year": "2023",
        "issn": "1521-4095",
        "times_cited": 80,
        "authors": ["Carol White"],
        "source": "crossref",
    }
    base.update(overrides)
    return base


class TestEnrichOne:
    def test_fills_from_openalex(self, lit_store):
        doi = "10.1038/test-enrich"
        lit_store.upsert_paper(Paper(doi=doi))

        with (
            patch("paperlit.ingest.enrich.openalex.fetch_one",
                  return_value=_oa_work(doi)),
            patch("paperlit.ingest.enrich.crossref.fetch_one") as cr_mock,
        ):
            result = enrich_one(doi, lit_store)

        assert result["ok"] is True
        assert "title" in result["filled"]
        assert "abstract" in result["filled"]
        # CrossRef should NOT be called when OpenAlex fills title+abstract
        cr_mock.assert_not_called()

        paper = lit_store.get_paper(doi)
        assert paper.title == "OpenAlex Title for " + doi
        assert paper.source_abstract == "openalex"
        assert paper.is_enriched is True

    def test_crossref_fallback_when_openalex_missing_abstract(self, lit_store):
        doi = "10.1038/test-fallback"
        lit_store.upsert_paper(Paper(doi=doi))

        oa_no_abstract = _oa_work(doi, abstract="")

        with (
            patch("paperlit.ingest.enrich.openalex.fetch_one",
                  return_value=oa_no_abstract),
            patch("paperlit.ingest.enrich.crossref.fetch_one",
                  return_value=_cr_work(doi)),
        ):
            result = enrich_one(doi, lit_store)

        assert result["ok"] is True
        paper = lit_store.get_paper(doi)
        # Title from OpenAlex (was filled), abstract from CrossRef (OA had none)
        assert paper.title.startswith("OpenAlex")
        assert paper.abstract.startswith("Abstract from CrossRef")
        assert paper.source_abstract == "crossref"

    def test_wos_data_not_overwritten(self, lit_store):
        doi = "10.1038/test-wos-keep"
        lit_store.upsert_paper(Paper(
            doi=doi,
            title="Original WoS Title",
            abstract="Original WoS Abstract",
            source_main="wos",
        ))

        with (
            patch("paperlit.ingest.enrich.openalex.fetch_one",
                  return_value=_oa_work(doi)),
            patch("paperlit.ingest.enrich.crossref.fetch_one"),
        ):
            result = enrich_one(doi, lit_store)

        paper = lit_store.get_paper(doi)
        assert paper.title == "Original WoS Title"
        assert paper.abstract == "Original WoS Abstract"
        assert "title" not in result["filled"]
        assert "abstract" not in result["filled"]

    def test_already_enriched_skipped(self, lit_store):
        doi = "10.1038/test-already"
        lit_store.upsert_paper(Paper(doi=doi, is_enriched=True, title="Done"))

        with (
            patch("paperlit.ingest.enrich.openalex.fetch_one") as oa_mock,
            patch("paperlit.ingest.enrich.crossref.fetch_one") as cr_mock,
        ):
            result = enrich_one(doi, lit_store)

        assert result["ok"] is True
        assert result["filled"] == []
        assert result["source"] == "cached"
        oa_mock.assert_not_called()
        cr_mock.assert_not_called()

    def test_not_in_db(self, lit_store):
        result = enrich_one("10.9999/nonexistent", lit_store)
        assert result["ok"] is False
        assert result["reason"] == "not_in_db"

    def test_empty_doi(self, lit_store):
        result = enrich_one("", lit_store)
        assert result["ok"] is False
        assert result["reason"] == "no_doi"

    def test_source_main_set_from_openalex(self, lit_store):
        doi = "10.1038/test-source-main"
        lit_store.upsert_paper(Paper(doi=doi, source_main="wos_ref"))

        with (
            patch("paperlit.ingest.enrich.openalex.fetch_one",
                  return_value=_oa_work(doi)),
            patch("paperlit.ingest.enrich.crossref.fetch_one"),
        ):
            enrich_one(doi, lit_store)

        paper = lit_store.get_paper(doi)
        assert paper.source_main == "openalex"

    def test_times_cited_filled_when_zero(self, lit_store):
        doi = "10.1038/test-cited"
        lit_store.upsert_paper(Paper(doi=doi, times_cited=0))

        with patch("paperlit.ingest.enrich.openalex.fetch_one",
                   return_value=_oa_work(doi, times_cited=55)):
            result = enrich_one(doi, lit_store)

        assert "times_cited" in result["filled"]
        paper = lit_store.get_paper(doi)
        assert paper.times_cited == 55

    def test_list_fields_filled(self, lit_store):
        doi = "10.1038/test-list"
        lit_store.upsert_paper(Paper(doi=doi))

        with patch("paperlit.ingest.enrich.openalex.fetch_one",
                   return_value=_oa_work(doi)):
            result = enrich_one(doi, lit_store)

        assert "authors" in result["filled"]
        assert "affiliations" in result["filled"]
        assert "keywords" in result["filled"]
        paper = lit_store.get_paper(doi)
        assert paper.authors == ["Alice Smith", "Bob Jones"]
        assert paper.affiliations == ["MIT", "Stanford University"]
        assert paper.keywords == ["battery", "electrolyte"]


class TestEnrichBatch:
    def test_batch_enrichment(self, lit_store):
        dois = ["10.1038/batch-1", "10.1038/batch-2", "10.1038/batch-3"]
        for d in dois:
            lit_store.upsert_paper(Paper(doi=d))

        oa_results = {
            "10.1038/batch-1": _oa_work("10.1038/batch-1"),
            "10.1038/batch-2": _oa_work("10.1038/batch-2"),
        }

        with (
            patch("paperlit.ingest.enrich.openalex.fetch_batch",
                  return_value=oa_results),
            patch("paperlit.ingest.enrich.crossref.fetch_one",
                  return_value=_cr_work("10.1038/batch-3")),
        ):
            result = enrich_batch(dois, lit_store)

        assert result["total"] == 3
        assert result["enriched"] == 3
        assert result["sources"]["openalex"] >= 2

    def test_batch_skips_already_enriched(self, lit_store):
        lit_store.upsert_paper(Paper(doi="10.1038/done", is_enriched=True))
        lit_store.upsert_paper(Paper(doi="10.1038/todo"))

        with (
            patch("paperlit.ingest.enrich.openalex.fetch_batch",
                  return_value={"10.1038/todo": _oa_work("10.1038/todo")}),
            patch("paperlit.ingest.enrich.crossref.fetch_one"),
        ):
            result = enrich_batch(["10.1038/done", "10.1038/todo"], lit_store)

        assert result["already_done"] == 1
        assert result["enriched"] == 1

    def test_batch_empty_input(self, lit_store):
        result = enrich_batch([], lit_store)
        assert result["total"] == 0
        assert result["enriched"] == 0

    def test_batch_all_already_done(self, lit_store):
        lit_store.upsert_paper(Paper(doi="10.1038/a", is_enriched=True))
        result = enrich_batch(["10.1038/a"], lit_store)
        assert result["enriched"] == 0
        assert result["already_done"] == 1


class TestEnrichAllPending:
    def test_enrich_all(self, lit_store):
        for i in range(3):
            lit_store.upsert_paper(Paper(doi=f"10.1038/all-{i}"))

        call_count = 0

        def mock_batch(dois, rate_limit=5.0):
            nonlocal call_count
            call_count += 1
            results = {}
            for d in dois:
                results[d.lower()] = _oa_work(d)
            return results

        with (
            patch("paperlit.ingest.enrich.openalex.fetch_batch",
                  side_effect=mock_batch),
            patch("paperlit.ingest.enrich.crossref.fetch_one"),
        ):
            result = enrich_all_pending(lit_store, batch_size=10)

        assert result["enriched"] == 3
        assert result["rounds"] >= 1

        stats = lit_store.stats()
        assert stats["enriched"] == 3

    def test_enrich_all_stops_when_no_progress(self, lit_store):
        lit_store.upsert_paper(Paper(doi="10.1038/stuck"))

        with (
            patch("paperlit.ingest.enrich.openalex.fetch_batch",
                  return_value={}),
            patch("paperlit.ingest.enrich.crossref.fetch_one",
                  return_value=None),
        ):
            result = enrich_all_pending(lit_store)

        assert result["enriched"] == 0
        assert result["rounds"] == 1


class _FakeMapper:
    """记录 add_mapping / bulk_add_mappings 调用，模拟 JournalMapper。"""

    def __init__(self):
        self.added: list[tuple[str, str, str]] = []
        self.bulk: list[dict] = []

    def add_mapping(self, abbreviation, full_name, source="manual"):
        self.added.append((abbreviation, full_name, source))
        return True

    def bulk_add_mappings(self, mappings, source="openalex"):
        self.bulk.append(dict(mappings))
        return len(mappings)


class TestJournalMappingLearning:
    def test_enrich_one_learns_abbrev_to_full(self, lit_store):
        doi = "10.1038/learn-1"
        lit_store.upsert_paper(Paper(doi=doi, journal="NAT ENERGY"))
        mapper = _FakeMapper()

        with (
            patch("paperlit.ingest.enrich.openalex.fetch_one",
                  return_value=_oa_work(doi, journal="Nature Energy")),
            patch("paperlit.ingest.enrich.crossref.fetch_one"),
        ):
            enrich_one(doi, lit_store, journal_mapper=mapper)

        assert ("NAT ENERGY", "Nature Energy", "enrich") in mapper.added
        # 补全只补空字段：journal 保持原缩写，替换交给规范化
        assert lit_store.get_paper(doi).journal == "NAT ENERGY"

    def test_no_mapping_when_journal_empty(self, lit_store):
        doi = "10.1038/learn-2"
        lit_store.upsert_paper(Paper(doi=doi, journal=""))
        mapper = _FakeMapper()

        with (
            patch("paperlit.ingest.enrich.openalex.fetch_one",
                  return_value=_oa_work(doi, journal="Nature Energy")),
            patch("paperlit.ingest.enrich.crossref.fetch_one"),
        ):
            enrich_one(doi, lit_store, journal_mapper=mapper)

        assert mapper.added == []

    def test_no_mapping_when_same_case_insensitive(self, lit_store):
        doi = "10.1038/learn-3"
        lit_store.upsert_paper(Paper(doi=doi, journal="Nature Energy"))
        mapper = _FakeMapper()

        with (
            patch("paperlit.ingest.enrich.openalex.fetch_one",
                  return_value=_oa_work(doi, journal="NATURE ENERGY")),
            patch("paperlit.ingest.enrich.crossref.fetch_one"),
        ):
            enrich_one(doi, lit_store, journal_mapper=mapper)

        assert mapper.added == []

    def test_batch_learns_and_bulk_saves(self, lit_store):
        dois = ["10.1038/bl-1", "10.1038/bl-2"]
        lit_store.upsert_paper(Paper(doi="10.1038/bl-1", journal="NAT ENERGY"))
        lit_store.upsert_paper(Paper(doi="10.1038/bl-2", journal="ADV MATER"))
        mapper = _FakeMapper()

        oa_results = {
            "10.1038/bl-1": _oa_work("10.1038/bl-1", journal="Nature Energy"),
            "10.1038/bl-2": _oa_work("10.1038/bl-2", journal="Advanced Materials"),
        }
        with (
            patch("paperlit.ingest.enrich.openalex.fetch_batch",
                  return_value=oa_results),
            patch("paperlit.ingest.enrich.crossref.fetch_one"),
        ):
            result = enrich_batch(dois, lit_store, journal_mapper=mapper)

        assert result["journal_mappings_learned"] == 2
        assert len(mapper.bulk) == 1
        assert mapper.bulk[0] == {
            "NAT ENERGY": "Nature Energy",
            "ADV MATER": "Advanced Materials",
        }

    def test_no_mapper_is_noop(self, lit_store):
        doi = "10.1038/nomap"
        lit_store.upsert_paper(Paper(doi=doi, journal="NAT ENERGY"))
        with (
            patch("paperlit.ingest.enrich.openalex.fetch_one",
                  return_value=_oa_work(doi, journal="Nature Energy")),
            patch("paperlit.ingest.enrich.crossref.fetch_one"),
        ):
            result = enrich_one(doi, lit_store)
        assert result["ok"] is True
