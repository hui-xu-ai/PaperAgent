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

    def test_doi_dup_fills_stub_empty_fields(self, lit_store):
        """库内空存根 ← WoS bib 完整记录：只补空字段并升级来源/置 is_enriched。"""
        lit_store.upsert_paper(Paper(
            doi="10.1002/stub", title="", abstract="", is_reference=True,
            source_main="wos_ref"))
        papers = [Paper(doi="10.1002/STUB", title="Full Title", abstract="Full abstract",
                        journal="ADV MATER", year="2021", authors=["A, B"],
                        source_main="wos", source_file="wos.bib")]
        unique, dups = deduplicate(papers, lit_store)
        assert len(unique) == 0 and dups == 1
        got = lit_store.get_paper("10.1002/stub")
        assert got.title == "Full Title" and got.abstract == "Full abstract"
        assert got.journal == "ADV MATER" and got.authors == ["A, B"]
        assert got.source_main == "wos" and got.source_file == "wos.bib"
        assert got.is_enriched is True and got.enriched_at
        assert got.is_reference is True  # 入库身份不变

    def test_doi_dup_never_overwrites_existing_values(self, lit_store):
        lit_store.upsert_paper(Paper(
            doi="10.1002/full", title="Old Title", abstract="Old abstract",
            journal="OLD J", year="2020", times_cited=7, source_main="wos"))
        papers = [Paper(doi="10.1002/full", title="New Title", abstract="New abstract",
                        journal="NEW J", year="2021", times_cited=99,
                        source_main="wos", source_file="wos2.bib")]
        unique, dups = deduplicate(papers, lit_store)
        assert len(unique) == 0 and dups == 1
        got = lit_store.get_paper("10.1002/full")
        assert got.title == "Old Title" and got.abstract == "Old abstract"
        assert got.journal == "OLD J" and got.year == "2020"
        assert got.times_cited == 7 and got.source_file == ""

    def test_doi_dup_upgrades_single_author_stub(self, lit_store):
        """库内单作者缩写存根 ← WoS 主记录完整作者列表：应升级而非保留存根。"""
        lit_store.upsert_paper(Paper(
            doi="10.1088/stub", title="T", abstract="A",
            authors=["Yu CH"], is_reference=True, source_main="wos_ref"))
        papers = [Paper(doi="10.1088/stub",
                        authors=["Yu, Chi-Hua", "Qin, Zhao", "Buehler, Markus J."],
                        affiliations=["MIT"], source_main="wos", source_file="wos.bib")]
        unique, dups = deduplicate(papers, lit_store)
        assert len(unique) == 0 and dups == 1
        got = lit_store.get_paper("10.1088/stub")
        assert got.authors == ["Yu, Chi-Hua", "Qin, Zhao", "Buehler, Markus J."]
        assert got.affiliations == ["MIT"]

    def test_dedup_reports_progress_per_paper(self, lit_store):
        """判重阶段必须逐条报进度（补全文件耗时在此，不能停 0）。"""
        lit_store.upsert_paper(Paper(doi="10.1088/exist", title="T", abstract="A"))
        papers = [Paper(doi="10.1088/exist", title="T", abstract="A"),
                  Paper(doi="10.1088/new", title="N", abstract="B")]
        calls = []
        deduplicate(papers, lit_store,
                    progress_cb=lambda c, t, d, ph: calls.append((c, t, ph)))
        assert [c[0] for c in calls] == [1, 2]
        assert all(c[1] == 2 for c in calls)
        assert all(c[2] == "判重与补全" for c in calls)

    def test_doi_dup_keeps_real_authors_not_stub(self, lit_store):
        """库内已有带 source_authors 的真实作者列表：不被 incoming 覆盖。"""
        lit_store.upsert_paper(Paper(
            doi="10.1088/real", title="T", abstract="A",
            authors=["Yu, Chi-Hua"], source_authors="openalex", source_main="wos"))
        papers = [Paper(doi="10.1088/real",
                        authors=["A, B", "C, D", "E, F"], source_main="wos")]
        unique, dups = deduplicate(papers, lit_store)
        assert len(unique) == 0 and dups == 1
        assert lit_store.get_paper("10.1088/real").authors == ["Yu, Chi-Hua"]

    def test_normalize_doi(self):
        assert _normalize_doi("https://doi.org/10.1002/aenm") == "10.1002/aenm"
        assert _normalize_doi("DOI: 10.1002/aenm.") == "10.1002/aenm"
        assert _normalize_doi("10.1002/AENM") == "10.1002/aenm"


class TestCorrespondingParsing:
    AFFS = [
        "Buehler, MJ (Corresponding Author), MIT, Lab Atomist \\& Mol Mech, "
        "Dept Civil \\& Environm Engn, 77 Massachusetts Ave, Cambridge, MA USA.",
        "Yu, Chi-Hua; Qin, Zhao; Buehler, Markus J., MIT, Lab Atomist \\& Mol Mech, "
        "Dept Civil \\& Environm Engn, 77 Massachusetts Ave, Cambridge, MA USA.",
    ]
    AUTHORS = ["Yu, Chi-Hua", "Qin, Zhao", "Buehler, Markus J."]

    def test_matches_full_name_from_abbrev(self):
        from paperlit.ingest.bib_ingest import parse_corresponding
        assert parse_corresponding(self.AFFS, self.AUTHORS) == ["Buehler, Markus J."]

    def test_no_marker_returns_empty(self):
        from paperlit.ingest.bib_ingest import parse_corresponding
        assert parse_corresponding(["MIT, Cambridge"], self.AUTHORS) == []
        assert parse_corresponding([], self.AUTHORS) == []

    def test_falls_back_to_abbrev_when_no_full_match(self):
        from paperlit.ingest.bib_ingest import parse_corresponding
        assert parse_corresponding(self.AFFS, ["Yu, Chi-Hua"]) == ["Buehler, MJ"]

    def test_multiple_corresponding_matched(self):
        from paperlit.ingest.bib_ingest import parse_corresponding
        affs = ["Zhang, Y; Wang, JH (Corresponding Author), Peking Univ, Beijing."]
        authors = ["Liu, Ying", "Zhang, Yan", "Wang, Jia-huai"]
        assert parse_corresponding(affs, authors) == ["Zhang, Yan", "Wang, Jia-huai"]

    def test_prefix_initials_match(self):
        """WoS 缩写 ZY 对应全称 Zhenyang（首字母口径不一致）应前缀兼容匹配。"""
        from paperlit.ingest.bib_ingest import parse_corresponding
        affs = ["Xi, M; Wang, ZY (Corresponding Author), Chinese Acad Sci, Hefei."]
        authors = ["Kang, Zihao", "Xi, Min", "Wang, Zhenyang"]
        assert parse_corresponding(affs, authors) == ["Xi, Min", "Wang, Zhenyang"]

    def test_author_whitespace_normalized(self, tmp_path):
        bib = tmp_path / "ws.bib"
        bib.write_text(
            '@article{a,\n  doi = {10.1016/ws},\n  title = {T},\n'
            '  author = {Zhang,\n   Yan and Wang, Jia-huai},\n}\n', encoding="utf-8")
        from paperlit.ingest.bib_ingest import parse_bib_file
        papers = parse_bib_file(bib)
        assert papers[0].authors == ["Zhang, Yan", "Wang, Jia-huai"]

    def test_bib_parse_sets_corresponding(self, tmp_path):
        bib = tmp_path / "c.bib"
        bib.write_text(
            '@article{a,\n'
            '  doi = {10.1088/corr},\n'
            '  title = {T},\n'
            '  author = {Yu, Chi-Hua and Qin, Zhao and Buehler, Markus J.},\n'
            '  affiliation = {' + self.AFFS[0] + '\n' + self.AFFS[1] + '},\n'
            '}\n', encoding="utf-8")
        from paperlit.ingest.bib_ingest import parse_bib_file
        papers = parse_bib_file(bib)
        assert papers[0].corresponding == ["Buehler, Markus J."]


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

    def test_ingest_stamps_is_enriched_when_complete(self, lit_store):
        complete = Paper(doi="10.1002/c", title="T", abstract="A", source_main="wos")
        stub = Paper(doi="10.1002/d", title="", abstract="", source_main="wos_ref")
        ingest_papers([complete, stub], lit_store, source_file="t.bib")
        assert lit_store.get_paper("10.1002/c").is_enriched is True
        assert lit_store.get_paper("10.1002/d").is_enriched is False

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
