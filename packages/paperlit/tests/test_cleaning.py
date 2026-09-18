# -*- coding: utf-8 -*-
"""paperlit 清洗回归测试：主文献保护 + 度量持久化（防数据丢失）。

锁定两个曾导致**用户数据丢失**的缺陷：
  1. cleaning 无主文献保护 → library_citations<N 会删掉 is_reference=0 的核心论文
     （主文献在库内 library_citations 通常为 0）。
  2. upsert_paper 的 INSERT OR REPLACE 漏写 impact_factor/quartile/library_citations
     → 元数据补全（enrich）调 upsert 时把清洗算好的度量重置为 0。
"""
from __future__ import annotations

import pytest

from paperlit.config import Roots
from paperlit.db import LitStore
from paperlit.models import Paper, Citation
from paperlit.ingest import cleaning


@pytest.fixture
def store(tmp_path):
    roots = Roots(data_dir=tmp_path / "data", lit_dir=tmp_path / "lit")
    roots.ensure()
    s = LitStore(roots)
    s.init_schema()
    return s


def _add(store, doi, *, is_reference=False, library_citations=0,
         impact_factor=0.0, quartile="", year="2020", title=None):
    store.upsert_paper(Paper(
        doi=doi, title=title or f"Paper {doi}", year=year,
        is_reference=is_reference))
    with store._conn() as conn:
        conn.execute(
            "UPDATE papers SET library_citations=?, impact_factor=?, quartile=? "
            "WHERE doi=?", (library_citations, impact_factor, quartile, doi))


def _cite(store, citing, cited):
    store.upsert_citation(Citation(citing_doi=citing, cited_doi=cited))


@pytest.fixture
def library(store):
    """2 主文献(库内被引0) + 3 参考(被引2/1/0)，主文献 M1 引用全部参考。"""
    _add(store, "M1", is_reference=False, library_citations=0, year="2023")
    _add(store, "M2", is_reference=False, library_citations=0, year="2024")
    _add(store, "R_hot", is_reference=True, library_citations=2, quartile="Q1",
         impact_factor=12.0)
    _add(store, "R_mid", is_reference=True, library_citations=1, quartile="Q2",
         impact_factor=4.0)
    _add(store, "R_cold", is_reference=True, library_citations=0, quartile="Q4",
         impact_factor=0.5)
    for r in ("R_hot", "R_mid", "R_cold"):
        _cite(store, "M1", r)
    _cite(store, "M2", "R_hot")
    return store


class TestMainPaperProtection:
    def test_preview_never_counts_mains(self, library):
        # library_citations<2 命中所有主文献(=0)与 R_mid/R_cold，但主文献必须被排除
        prev = cleaning.preview_cleaning(library, {"library_citations": {"min": 2}})
        assert prev["to_remove"] == 2          # 仅 R_mid + R_cold
        removed_dois = {s["doi"] for s in prev["samples"]}
        assert "M1" not in removed_dois and "M2" not in removed_dois

    def test_execute_delete_keeps_mains(self, library):
        res = cleaning.execute_cleaning(
            library, {"library_citations": {"min": 2}}, mode="delete")
        assert res["removed"] == 2
        with library._conn() as conn:
            mains = conn.execute(
                "SELECT COUNT(*) FROM papers WHERE is_reference=0").fetchone()[0]
            assert mains == 2                  # 主文献全在
            assert conn.execute(
                "SELECT 1 FROM papers WHERE doi='R_hot'").fetchone() is not None
            assert conn.execute(
                "SELECT 1 FROM papers WHERE doi='R_cold'").fetchone() is None

    def test_lc_min_1_removes_nothing_when_only_mains_are_zero(self, library):
        # 旧 bug：lc<1 会删掉库内被引=0 的主文献；修复后主文献受保护
        prev = cleaning.preview_cleaning(library, {"library_citations": {"min": 1}})
        # 仅 R_cold(参考, lc=0) 命中；两条主文献虽 lc=0 但被保护
        assert prev["to_remove"] == 1

    def test_delete_removes_orphan_citations(self, library):
        cleaning.execute_cleaning(
            library, {"library_citations": {"min": 2}}, mode="delete")
        with library._conn() as conn:
            # R_cold/R_mid 被删 → 指向它们的 citations 必须一并清除（无孤立边）
            orphan = conn.execute("""
                SELECT COUNT(*) FROM citations
                WHERE cited_doi NOT IN (SELECT doi FROM papers)
                   OR citing_doi NOT IN (SELECT doi FROM papers)
            """).fetchone()[0]
            assert orphan == 0

    def test_no_rules_is_noop(self, library):
        prev = cleaning.preview_cleaning(library, {})
        assert prev["to_remove"] == 0 and prev["to_keep"] == prev["total"]
        res = cleaning.execute_cleaning(library, {}, mode="delete")
        assert res["removed"] == 0


class TestMetricPersistence:
    def test_upsert_roundtrip_preserves_metrics(self, library):
        # 模拟 enrich：get_paper → 改字段 → upsert_paper，度量不得丢失
        p = library.get_paper("R_hot")
        assert p.library_citations == 2 and p.impact_factor == 12.0 and p.quartile == "Q1"
        p.title = "Enriched Title"
        p.is_enriched = True
        library.upsert_paper(p)
        q = library.get_paper("R_hot")
        assert q.title == "Enriched Title"
        assert q.library_citations == 2, "library_citations 被 upsert 重置（回归）"
        assert q.impact_factor == 12.0, "impact_factor 被 upsert 重置（回归）"
        assert q.quartile == "Q1", "quartile 被 upsert 重置（回归）"

    def test_compute_library_citations_survives_later_upsert(self, library):
        cleaning.compute_library_citations(library)
        with library._conn() as conn:
            before = conn.execute(
                "SELECT library_citations FROM papers WHERE doi='R_hot'").fetchone()[0]
        p = library.get_paper("R_hot")
        p.abstract = "x"
        library.upsert_paper(p)
        with library._conn() as conn:
            after = conn.execute(
                "SELECT library_citations FROM papers WHERE doi='R_hot'").fetchone()[0]
        assert before == after
