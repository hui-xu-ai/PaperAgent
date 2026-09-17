# -*- coding: utf-8 -*-
"""paperlit P1 端到端测试：bib 解析 → 去重 → 入库 → 查询。"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from paperlit.config import Roots, LitSettings
from paperlit.db import LitStore
from paperlit.ingest.bib_ingest import parse_bib, parse_bib_file, raw_to_paper, ingest_papers
from paperlit.ingest.dedup import deduplicate, _normalize_doi, _normalize_title
from paperlit.models import Paper, Citation


SAMPLE_BIB = r"""
@article{WOS:0001,
Title = {Solid polymer electrolytes for lithium batteries},
Abstract = {This review covers recent advances in solid polymer electrolytes
   with high ionic conductivity for all-solid-state lithium batteries.},
Author = {Zhang, Wei and Li, Ming and Wang, Hua},
Journal = {ADVANCED ENERGY MATERIALS},
Year = {2023},
Volume = {13},
Pages = {2203456},
DOI = {10.1002/aenm.202203456},
Keywords = {polymer electrolyte; lithium battery; ionic conductivity},
Research-Areas = {Electrochemistry; Materials Science},
Web-of-Science-Categories = {Electrochemistry; Materials Science, Multidisciplinary},
Times-Cited = {45},
Unique-Id = {WOS:0001},
Affiliation = {Zhang, Wei (Corresponding Author), Tsinghua Univ, Beijing, China
   Li, Ming, Peking Univ, Beijing, China},
Cited-References = {Smith J, 2020, NATURE ENERGY, V5, P123, DOI 10.1038/nenergy.2020.123
   Chen X, 2021, CHEM REV, V121, P456, DOI 10.1021/acs.chemrev.0c01234
   Wang Y, 2019, ENERGY ENVIRON SCI, V12, P789, DOI 10.1039/C8EE03456A}
}

@article{WOS:0002,
Title = {Sulfide-based solid electrolytes: synthesis and electrochemical performance},
Abstract = {Sulfide-based solid electrolytes exhibit superior ionic conductivity
   and are promising candidates for next-generation batteries.},
Author = {Liu, Yang and Chen, Xiao},
Journal = {JOURNAL OF THE AMERICAN CHEMICAL SOCIETY},
Year = {2024},
Volume = {146},
DOI = {10.1021/jacs.3c12345},
Times-Cited = {12},
Unique-Id = {WOS:0002},
Cited-References = {Zhang W, 2023, ADV ENERG MATER, V13, DOI 10.1002/aenm.202203456
   Smith J, 2020, NATURE ENERGY, V5, P123, DOI 10.1038/nenergy.2020.123}
}

@article{WOS:0003,
Title = {Machine learning guided discovery of solid electrolytes},
Author = {Park, Joon and Kim, Soo},
Journal = {NATURE MATERIALS},
Year = {2024},
DOI = {10.1038/s41563-024-01234},
Times-Cited = {5},
Unique-Id = {WOS:0003},
Cited-References = {Liu Y, 2024, J AM CHEM SOC, V146, DOI 10.1021/jacs.3c12345}
}
"""


@pytest.fixture
def lit_store(tmp_path):
    roots = Roots(data_dir=tmp_path / "data", lit_dir=tmp_path / "lit")
    roots.ensure()
    store = LitStore(roots)
    store.init_schema()
    return store


@pytest.fixture
def bib_file(tmp_path):
    bib_path = tmp_path / "test_export.bib"
    bib_path.write_text(SAMPLE_BIB, encoding="utf-8")
    return bib_path


class TestBibParser:
    def test_parse_bib_count(self):
        records = parse_bib(SAMPLE_BIB)
        assert len(records) == 3

    def test_parse_bib_fields(self):
        records = parse_bib(SAMPLE_BIB)
        r0 = records[0]
        assert r0["_key"] == "WOS:0001"
        assert "polymer electrolytes" in r0.get("title", "").lower()
        assert r0.get("doi") == "10.1002/aenm.202203456"
        assert r0.get("year") == "2023"

    def test_raw_to_paper(self):
        records = parse_bib(SAMPLE_BIB)
        paper = raw_to_paper(records[0], source_file="test.bib")
        assert paper.doi == "10.1002/aenm.202203456"
        assert len(paper.authors) == 3
        assert paper.authors[0] == "Zhang, Wei"
        assert paper.times_cited == 45
        assert len(paper.references) == 3
        assert paper.references[0].doi == "10.1038/nenergy.2020.123"
        assert paper.source_main == "wos"

    def test_parse_bib_file(self, bib_file):
        papers = parse_bib_file(bib_file)
        assert len(papers) == 3
        assert all(p.doi for p in papers)

    def test_cited_ref_parsing(self):
        records = parse_bib(SAMPLE_BIB)
        paper = raw_to_paper(records[0])
        refs = paper.references
        assert len(refs) == 3
        assert refs[0].doi == "10.1038/nenergy.2020.123"
        assert refs[1].doi == "10.1021/acs.chemrev.0c01234"
        assert refs[2].doi == "10.1039/C8EE03456A"


class TestDedup:
    def test_doi_dedup(self, lit_store):
        papers = [
            Paper(doi="10.1002/aenm.202203456", title="Paper A"),
            Paper(doi="10.1002/AENM.202203456", title="Paper A duplicate"),
            Paper(doi="10.1021/jacs.3c12345", title="Paper B"),
        ]
        unique, dups = deduplicate(papers, lit_store)
        assert len(unique) == 2
        assert dups == 1

    def test_title_year_dedup(self, lit_store):
        papers = [
            Paper(doi="10.1002/a", title="Same Title Paper", year="2023"),
            Paper(doi="10.1002/b", title="same title paper", year="2023"),
        ]
        unique, dups = deduplicate(papers, lit_store)
        assert len(unique) == 1
        assert dups == 1

    def test_author_year_journal_dedup(self, lit_store):
        papers = [
            Paper(doi="10.1002/a", title="Title A",
                  authors=["Zhang, Wei"], year="2023", journal="Nature"),
            Paper(doi="10.1002/b", title="Title B different",
                  authors=["Zhang, Wei"], year="2023", journal="Nature"),
        ]
        unique, dups = deduplicate(papers, lit_store)
        assert len(unique) == 1
        assert dups == 1

    def test_no_false_dedup(self, lit_store):
        papers = [
            Paper(doi="10.1002/a", title="Paper A", year="2023"),
            Paper(doi="10.1002/b", title="Paper A", year="2024"),
        ]
        unique, dups = deduplicate(papers, lit_store)
        assert len(unique) == 2
        assert dups == 0

    def test_existing_paper_dedup(self, lit_store):
        lit_store.upsert_paper(Paper(doi="10.1002/a", title="Existing"))
        papers = [Paper(doi="10.1002/a", title="Existing duplicate")]
        unique, dups = deduplicate(papers, lit_store)
        assert len(unique) == 0
        assert dups == 1

    def test_normalize_doi(self):
        assert _normalize_doi("https://doi.org/10.1002/aenm") == "10.1002/aenm"
        assert _normalize_doi("DOI: 10.1002/aenm.") == "10.1002/aenm"
        assert _normalize_doi("10.1002/AENM") == "10.1002/aenm"


class TestIngest:
    def test_ingest_papers(self, lit_store, bib_file):
        papers = parse_bib_file(bib_file)
        unique, _ = deduplicate(papers, lit_store)
        result = ingest_papers(unique, lit_store, source_file="test.bib")

        assert result["new_papers"] == 3
        assert result["new_citations"] > 0
        assert result["new_refs"] > 0

        stats = lit_store.stats()
        assert stats["total_papers"] == 3 + result["new_refs"]
        assert stats["main_papers"] == 3
        assert stats["reference_papers"] == result["new_refs"]
        assert stats["citations"] == result["new_citations"]

    def test_citation_graph(self, lit_store, bib_file):
        papers = parse_bib_file(bib_file)
        unique, _ = deduplicate(papers, lit_store)
        ingest_papers(unique, lit_store)

        cites_for = lit_store.get_citations_for("10.1002/aenm.202203456")
        assert len(cites_for) == 3

        cited_by = lit_store.get_cited_by("10.1038/nenergy.2020.123")
        assert "10.1002/aenm.202203456" in cited_by
        assert "10.1021/jacs.3c12345" in cited_by

    def test_ref_stub_creation(self, lit_store, bib_file):
        papers = parse_bib_file(bib_file)
        unique, _ = deduplicate(papers, lit_store)
        result = ingest_papers(unique, lit_store)

        ref_doi = "10.1038/nenergy.2020.123"
        stub = lit_store.get_paper(ref_doi)
        assert stub is not None
        assert stub.is_reference is True
        assert stub.source_main == "wos_ref"

    def test_idempotent_reingest(self, lit_store, bib_file):
        papers = parse_bib_file(bib_file)
        unique, _ = deduplicate(papers, lit_store)
        r1 = ingest_papers(unique, lit_store)
        r2 = ingest_papers(unique, lit_store)
        assert r2["new_papers"] == 0
        assert r2["new_citations"] == 0
        assert r2["new_refs"] == 0

    def test_search_fts(self, lit_store, bib_file):
        papers = parse_bib_file(bib_file)
        unique, _ = deduplicate(papers, lit_store)
        ingest_papers(unique, lit_store)

        results = lit_store.search_fts("polymer electrolyte")
        assert len(results) >= 1
        assert any("polymer" in r.title.lower() for r in results)

    def test_search_fts_chinese(self, lit_store):
        lit_store.upsert_paper(Paper(
            doi="10.1002/test",
            title="固态电解质研究进展",
            abstract="本文综述了固态电解质的最新研究成果",
            year="2024",
        ))
        results = lit_store.search_fts("固态电解质")
        assert len(results) >= 1


class TestApiFacade:
    def test_init_and_ingest(self, tmp_path, bib_file):
        from paperlit.api import init_lit, ingest_bib, paper_count, citation_count

        roots = Roots(data_dir=tmp_path / "data", lit_dir=tmp_path / "lit")
        stats = init_lit(roots)
        assert stats["total_papers"] == 0

        result = ingest_bib(bib_file)
        assert result["new_papers"] == 3
        assert paper_count() > 3
        assert citation_count() > 0
