# -*- coding: utf-8 -*-
"""变体头部（frontmatter）装配 + 键形态归一 的守卫测试（2026-09-16）。

背景（用户报障："zh.md/en_zh.md 开头的元数据很混乱"）：实测三个独立缺陷叠加——
  D1 **键形态**：引擎层用**目录名**（`10.1002_adma.202407106`）当键取 papers_meta，
     而 `resolve_rid` 当时只认 RID / 裸 DOI ⇒ 恒 None（`has_meta=False`），
     头部只能回退 document.json（那里 journal/year 为空、作者串带 `\\*` 解析噪声）；
  D2 **时序**：翻译渲染发生在 `_assemble_kb`（Crossref 富化）之前 ⇒ 那一刻 papers_meta 还没行；
  D3 **指标漏传**：变体头部拿不到 journals.db 的影响因子/分区（恒显示「（无指标）」）。
本文件守 D1 与 D3（D2 由 `backend/tests/test_task_service.py` 的顺序断言守）。
"""
from __future__ import annotations

import sqlite3

import pytest

from paperkb import api as kbapi
from paperkb.config import Roots
from paperkb.journals import JournalsDB
from paperkb.models import PaperMeta

DIRNAME = "10.1002_adma.202407106"
RAW_DOI = "10.1002/adma.202407106"
RID = "doi-10.1002_adma.202407106"


def _roots(tmp_path) -> Roots:
    roots = Roots(data_dir=tmp_path / "data", library_dir=tmp_path / "library",
                  kb_dir=tmp_path / "knowledge_base")
    roots.ensure()
    return roots


def _seed_meta(roots: Roots) -> None:
    """写一行 papers_meta（含 DOI 标识登记）+ journals.db 的 JCR/CAS 指标。"""
    store = kbapi._need_store()                       # noqa: SLF001 - 测试用内部 store
    store.upsert_meta(PaperMeta(
        doi=RAW_DOI, title="T",
        authors=["Zhenjin Xu", "Keqi Deng", "Jianyi Zheng", "Dezhi Wu"],
        corresponding=["Jianyi Zheng", "Dezhi Wu"],
        affiliations=["Xiamen University"],
        journal="Advanced Materials", year="2024", issn="0935-9648",
        keywords=["Graphene", "Actuator"], times_cited=11), rid=RID)
    db = JournalsDB(roots)
    db.init_schema()
    conn = sqlite3.connect(str(roots.reference_db))
    conn.execute("INSERT OR REPLACE INTO jcr(issn,eissn,journal_name,jif,quartile,"
                 "jif_rank,zone_2023,total_citation,year) VALUES(?,?,?,?,?,?,?,?,?)",
                 ("0935-9648", "", "Advanced Materials", 26.8, "Q1", "", "", 0, 2024))
    conn.execute("INSERT OR REPLACE INTO cas(journal_name,zone,is_top,is_oa,year,updated_at)"
                 " VALUES(?,?,?,?,?,?)", ("Advanced Materials", 1, 0, 0, 2024, ""))
    conn.commit()
    conn.close()


def test_resolve_rid_normalizes_directory_name(tmp_path):
    """D1 守卫：**DOI 目录名**必须解析到与 RID/裸 DOI 同一个键。"""
    roots = _roots(tmp_path)
    kbapi.init_kb(roots)
    _seed_meta(roots)
    store = kbapi._need_store()                       # noqa: SLF001
    assert store.resolve_rid(DIRNAME) == RID
    assert store.resolve_rid(DIRNAME) == store.resolve_rid(RAW_DOI) == store.resolve_rid(RID)
    assert store.has_meta(DIRNAME) is True
    assert store.get_meta(DIRNAME) is not None


def test_get_paper_meta_accepts_directory_name(tmp_path):
    roots = _roots(tmp_path)
    kbapi.init_kb(roots)
    _seed_meta(roots)
    m = kbapi.get_paper_meta(DIRNAME)
    assert m is not None and m["rid"] == RID, "目录名形态取不到 papers_meta（头部会退化成空值）"


def test_variant_frontmatter_full_fields_from_dirname_key(tmp_path):
    """端到端：**目录名**键 → papers_meta + journals.db → 用户给定顺序的完整 frontmatter。"""
    roots = _roots(tmp_path)
    kbapi.init_kb(roots)
    _seed_meta(roots)
    fm = kbapi.variant_frontmatter(
        DIRNAME, {"title": "Reinforced Magnetic-Responsive…", "authors": ["（脏）\\* and X\\*"],
                  "doi": RAW_DOI, "extraction_time": "2026-09-16T00:28:27+00:00"},
        ["文献"])
    lines = fm.splitlines()
    assert lines[0] == "---" and lines[-1] == "---"
    assert lines[1] == 'title: "Reinforced Magnetic-Responsive…"'
    assert lines[2] == '作者: "Zhenjin Xu, Keqi Deng, Jianyi Zheng*, Dezhi Wu*"'
    assert lines[3] == '通讯作者: "Jianyi Zheng；Dezhi Wu"'
    assert lines[4] == '研究单位: "Xiamen University"'
    assert lines[5] == "年份: 2024"
    assert lines[6] == '期刊: "Advanced Materials"'
    assert lines[7] == "影响因子: 26.8"
    assert lines[8] == 'JCR分区: "Q1"'
    assert lines[9] == '中科院分区: "1区"'
    assert lines[10] == f'DOI: "{RAW_DOI}"'
    assert lines[11] == "被引: 11"
    assert lines[12] == '关键词: "Graphene, Actuator"'
    assert lines[13] == 'tags: ["文献"]'
    assert lines[14] == "source: pdf"
    assert lines[15] == "created: 2026-09-16T00:28:27+00:00"
    assert "\\*" not in fm, "解析噪声作者串不得进头部（papers_meta 权威值优先）"


def test_variant_frontmatter_without_db_row_falls_back_to_document_json(tmp_path):
    """无 papers_meta 行时不报错：document.json 能给的字段照出，其余整行不出。"""
    roots = _roots(tmp_path)
    kbapi.init_kb(roots)
    fm = kbapi.variant_frontmatter(
        "10.1002_none.1", {"title": "T", "authors": ["A"], "doi": "10.1002/none.1",
                           "extraction_time": "x"}, ["文献"])
    assert '作者: "A"' in fm
    assert "期刊" not in fm and "被引" not in fm and "影响因子" not in fm


def test_variant_frontmatter_without_journals_db_omits_metrics(tmp_path):
    """D3：journals.db 无该刊时**不写**指标行（不写 `影响因子: 0` 之类假值）。"""
    roots = _roots(tmp_path)
    kbapi.init_kb(roots)
    store = kbapi._need_store()                       # noqa: SLF001
    store.upsert_meta(PaperMeta(doi=RAW_DOI, authors=["A"], journal="Unknown Journal",
                                year="2024", times_cited=3), rid=RID)
    fm = kbapi.variant_frontmatter(DIRNAME, {"title": "T"}, ["文献"])
    assert '期刊: "Unknown Journal"' in fm
    assert "影响因子" not in fm and "JCR分区" not in fm and "中科院分区" not in fm
    assert "被引: 3" in fm


@pytest.mark.parametrize("key", [RID, RAW_DOI, DIRNAME])
def test_all_three_key_forms_give_same_frontmatter(tmp_path, key):
    """三种键写法（RID / 裸 DOI / 目录名）必须产出**逐字相同**的头部。"""
    roots = _roots(tmp_path)
    kbapi.init_kb(roots)
    _seed_meta(roots)
    doc_meta = {"title": "T", "extraction_time": "2026-01-01T00:00:00+00:00"}
    assert kbapi.variant_frontmatter(key, doc_meta, ["文献"]) == \
        kbapi.variant_frontmatter(RID, doc_meta, ["文献"])
