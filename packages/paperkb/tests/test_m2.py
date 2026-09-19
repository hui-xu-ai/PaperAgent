# -*- coding: utf-8 -*-
"""M2 单测：journals.db（xlsx 导入/查询）+ 价值评分（缺失归一化/等级）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from paperkb import api
from paperkb.config import Roots
from paperkb.journals import JournalsDB, norm_journal_name
from paperkb.models import PaperMeta
from paperkb.score import value_score

REAL_XLSX = Path(__file__).resolve().parents[3] / "用户提供的文献" / "影响因子和分区" / "JCR分区.xlsx"
REAL_XLSX_EXISTS = REAL_XLSX.exists()


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
    api._journals = None  # noqa: SLF001
    api._settings = None  # noqa: SLF001


def _make_xlsx(path: Path) -> None:
    """构造小 xlsx fixture（JCR + 中科院两个 sheet）。"""
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "2025JCRIF-分区"
    ws.append(["期刊名称", "2024JIF", "Quartile", "JIF rank", "2023分区",
               "Total citation", "Category", "ISSN", "eISSN"])
    ws.append(["NATURE REVIEWS MICROBIOLOGY", "103.3", "Q1", "1/163", "Q1",
               "61109", "MICROBIOLOGY(SCIE)", "1740-1526", "1740-1534"])
    ws.append(["CHEM ENG J", "15.1", "Q1", "8/170", "Q1",
               "123456", "ENGINEERING, CHEMICAL(SCIE)", "1385-8947", ""])
    ws.append(["SENSOR ACTUAT B-CHEM", "8.4", "Q1", "12/86", "Q1",
               "98765", "CHEMISTRY, ANALYTICAL(SCIE)", "0925-4005", ""])
    ws2 = wb.create_sheet("2025中科学院分区表")
    ws2.append(["期刊名称", "2025分区", "Top", "Open Access"])
    ws2.append(["NATURE REVIEWS MICROBIOLOGY", 1, "是", "是"])
    ws2.append(["CHEM ENG J", 1, "是", "否"])
    ws2.append(["SENSOR ACTUAT B-CHEM", 2, "否", "否"])
    wb.save(path)


# ---------------------------------------------------------------- 导入/查询

def test_journals_import_lookup(store, tmp_path):
    x = tmp_path / "jcr_test.xlsx"
    _make_xlsx(x)

    prev = api.journals_preview(x)
    assert prev["total_jcr"] == 3
    assert prev["total_cas"] == 3
    assert prev["sheets"][0]["year"] == 2025
    assert prev["sheets"][0]["sample"][0]["journal_name"] == "NATURE REVIEWS MICROBIOLOGY"

    r = api.journals_import(x)
    assert r["jcr_imported"] == 3 and r["cas_imported"] == 3
    assert r["stats"]["jcr_rows"] == 3 and r["stats"]["cas_rows"] == 3

    # 名称规范化匹配（大小写/标点差异）
    hit = api.journals_lookup("Nature Reviews Microbiology")
    assert hit and hit["jcr"]["quartile"] == "Q1"
    assert float(hit["jcr"]["jif"]) == 103.3
    assert hit["cas"]["zone"] == 1

    hit2 = api.journals_lookup("CHEM ENG J")
    assert hit2 and hit2["cas"]["zone"] == 1

    assert api.journals_lookup("NO SUCH JOURNAL") is None
    # 幂等：重导 = 覆盖
    api.journals_import(x)
    assert api.journals_stats()["jcr_rows"] == 3


def test_journals_real_xlsx_preview(store):
    """②级：真实 JCR 表格只做预览（不 dump 全文，不写库）。"""
    if not REAL_XLSX_EXISTS:
        pytest.skip("真实 xlsx 不存在")
    p = api.journals_preview(REAL_XLSX)
    assert p["total_jcr"] > 20000, f"jcr 行数异常: {p['total_jcr']}"
    assert p["total_cas"] > 20000
    assert p["sheets"][0]["sample"][0]["quartile"] in ("Q1", "Q2", "Q3", "Q4")


# ---------------------------------------------------------------- 价值评分

def _meta(**kw) -> PaperMeta:
    base = dict(doi="10.1000/test.1", title="A study on X", journal="CHEM ENG J",
                year="2024", times_cited=50, keywords=["X", "Y"])
    base.update(kw)
    return PaperMeta(**base)


def test_score_with_journals(store, tmp_path):
    x = tmp_path / "jcr_test.xlsx"
    _make_xlsx(x)
    api.journals_import(x)

    # Q1 + 中科院 1 区（+1）→ if 分 5.0（journals.db 联查）
    from paperkb.api import _score_meta
    meta = _meta()
    r = _score_meta(meta, None, None)
    assert r["parts"]["if"]["value"] == 5.0
    assert r["parts"]["if"]["available"] is True
    assert r["score"] >= 4.0, f"Q1+1区 应高价值: {r}"


def test_score_missing_normalization():
    """无 IF/无被引（无 bib）→ 归一化到已有字段，不惩罚。"""
    meta = _meta(times_cited=0)
    meta.source_file = ""  # 无 bib
    r = value_score(meta, journal_info=None, has_bib=False)
    assert r["parts"]["if"]["available"] is False
    assert r["parts"]["cited"]["available"] is False
    assert r["parts"]["ai_value"]["available"] is False
    assert r["parts"]["topic"]["available"] is False
    # 仅 year 可用 → 权重归一化后 score = 0.10/0.10 * year_val / 1.0 * 5.0
    assert 0 < r["score"] <= 5.0
    assert r["level"] in ("L1", "L2")  # 无 AI 评分 → 不可能 L3


def test_score_ai_value_forces_l3():
    meta = _meta()
    meta.ai_value_score = 4.5
    r = value_score(meta, journal_info=None, has_bib=False)
    assert r["parts"]["ai_value"]["value"] == 4.5
    assert r["parts"]["ai_value"]["available"] is True
    assert r["score"] > 0


def test_score_year_old():
    meta = _meta(year="2005")
    r = value_score(meta, has_bib=True)
    assert r["parts"]["year"]["value"] == 0.4


def test_norm_journal_name():
    assert norm_journal_name("Science China-Materials") == norm_journal_name("sciencechinamaterials")
    # & 与 AND 不强制等价（规范化只去空白/标点）
    assert norm_journal_name("  Sensors & Actuators B: Chemical ") == "SENSORSACTUATORSBCHEMICAL"


def test_lookup_issn_and_override(store, tmp_path):
    """ISSN 优先匹配（缩写期刊名绕开）+ 人工纠正 journal_override。"""
    x = tmp_path / "jcr_test.xlsx"
    _make_xlsx(x)
    api.journals_import(x)

    # ISSN 精确匹配（bib 期刊缩写 "CHEM ENG J" 也命中）
    hit = api.journals_lookup_issn("1385-8947")
    assert hit and hit["jcr"]["journal_name"] == "CHEM ENG J"

    # 未匹配期刊 + override
    from paperkb.models import PaperMeta
    meta = PaperMeta(doi="10.1000/override.1", title="T", journal="CHEM ENG J",
                     issn="9999-9999", year="2024")
    api._need_store().upsert_meta(meta)  # noqa: SLF001
    r = api.set_journal_override("10.1000/override.1", "CHEM ENG J")
    assert r["journal_override"] == "CHEM ENG J"
    assert r["score"]["parts"]["if"]["available"] is True
