# -*- coding: utf-8 -*-
"""期刊名变体归一化（2026-09-21 用户报障：`&` 与 `and` 写法不同导致查不到指标）。

两种现象分开钉：
  1. **同一本刊不同写法**：`Sensors and Actuators B: Chemical`（bib 侧，Crossref 风格）
     与 `SENSORS AND ACTUATORS B-CHEMICAL`（JCR 表风格）必须归一到一个键 ——
     大小写/冒号/连字符本来就能对上，问题只出在 `&`：旧实现把 `&` 当标点**删掉**、
     却保留 `and`，于是同一本刊裂成两个键。
  2. **jcr↔cas 配对**：两表按规范化名配对，别再要求原始串逐字相同。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from paperkb.config import Roots
from paperkb.journals import JournalsDB, norm_journal_name, norm_journal_name_loose


def test_ampersand_equals_and():
    assert norm_journal_name("Sensors & Actuators B") == \
           norm_journal_name("Sensors and Actuators B")


def test_sensors_and_actuators_b_all_variants_collapse():
    """用户点名的期刊：7 种常见写法必须全部落到同一个键。"""
    forms = [
        "Sensors and Actuators B: Chemical",
        "SENSORS AND ACTUATORS B-CHEMICAL",
        "Sensors & Actuators B: Chemical",
        "Sensors & Actuators B-Chemical",
        "SENSORS AND ACTUATORS B: CHEMICAL",
        "Sensors and Actuators B Chemical",
        "Sensors and Actuators B-Chemical",
    ]
    assert len({norm_journal_name(x) for x in forms}) == 1


def test_ampersand_row_still_matches_other_style():
    """含 `&` 的真实刊名（表里最多的形态）与全拼写法等价。"""
    assert norm_journal_name("ACS Applied Materials & Interfaces") == \
           norm_journal_name("ACS Applied Materials and Interfaces")


def test_whitespace_and_punct_still_ignored():
    """原有的“去空白/标点”能力不能回退。"""
    assert norm_journal_name("Sensors-and Actuators,B") == \
           norm_journal_name("Sensors and Actuators B")


@pytest.fixture()
def jdb(tmp_path: Path) -> JournalsDB:
    roots = Roots(data_dir=tmp_path / "data", library_dir=tmp_path / "library",
                  kb_dir=tmp_path / "kb").ensure()
    db = JournalsDB(roots)
    db.init_schema()
    return db


def test_lookup_matches_across_naming_styles(jdb: JournalsDB):
    """表里是 JCR 大写连字符风格，bib 给是 Crossref 的 &/冒号风格 → 必须命中。"""
    jdb.upsert_jcr([{"journal_name": "SENSORS AND ACTUATORS B-CHEMICAL",
                     "jif": 8.0, "quartile": "Q1", "year": 2024,
                     "issn": "0925-4005"}])
    hit = jdb.lookup("Sensors & Actuators B: Chemical")
    assert hit is not None, "同一本刊换种写法就查不到（用户报障）"
    assert hit["jcr"]["jif"] == 8.0


def test_issn_still_preferred(jdb: JournalsDB):
    """ISSN 命中路径不受本次改动影响（仍是绕开缩写问题的主路）。"""
    jdb.upsert_jcr([{"journal_name": "SENSORS AND ACTUATORS B-CHEMICAL",
                     "jif": 8.0, "year": 2024, "issn": "0925-4005"}])
    hit = jdb.lookup_issn("0925-4005")
    assert hit is not None and hit["jcr"]["jif"] == 8.0


def test_cas_pairs_across_naming_styles(jdb: JournalsDB):
    """回归钉：jcr 与 cas 两表同一本刊写法不同时，中科院分区不得静默丢失。

    旧实现 `_with_cas` 用**原始名**去 cas 索引里找，要求两表逐字一致 ——
    实测 771 行 jcr 名在 cas 里没有完全相同的原始串。
    """
    jdb.upsert_jcr([{"journal_name": "SENSORS AND ACTUATORS B-CHEMICAL",
                     "jif": 8.0, "year": 2024}])
    jdb.upsert_cas([{"journal_name": "Sensors & Actuators B: Chemical",
                     "zone": 1, "is_top": "是", "year": 2024}])
    hit = jdb.lookup("SENSORS AND ACTUATORS B-CHEMICAL")
    assert hit is not None
    assert hit["cas"] is not None, "jcr/cas 写法不同 → 中科院分区被静默丢掉"
    assert hit["cas"]["zone"] == 1


# ------------------------------------------------ 城市消歧后缀（兜底，真实数据驱动）
def test_loose_norm_unchanged_without_suffix():
    """没有后缀时，兜底口径必须与常规口径同值（否则会影响全表）。"""
    assert norm_journal_name_loose("Advanced Materials") == \
           norm_journal_name("Advanced Materials")


def test_city_suffix_fallback_recovers_short_title(jdb: JournalsDB):
    """实测 lit.db 的真实 miss：`Children` 查不到 `Children-Basel`（2/676 篇受影响）。"""
    jdb.upsert_jcr([{"journal_name": "Children-Basel", "jif": 2.0,
                     "quartile": "Q1", "year": 2024}])
    hit = jdb.lookup("Children")
    assert hit is not None, "短名查不到带城市消歧后缀的表项"
    assert hit["jcr"]["journal_name"] == "Children-Basel"


def test_exact_match_wins_over_suffix_fallback(jdb: JournalsDB):
    """`Decision` 与 `Decision-Washington` 是两本独立刊 → 主键必须先赢，绝不跳去后缀项。"""
    jdb.upsert_jcr([{"journal_name": "Decision", "jif": 1.0, "year": 2024},
                    {"journal_name": "Decision-Washington", "jif": 9.9, "year": 2024}])
    hit = jdb.lookup("Decision")
    assert hit is not None and hit["jcr"]["jif"] == 1.0

    hit2 = jdb.lookup("Decision-Washington")
    assert hit2 is not None and hit2["jcr"]["jif"] == 9.9


def test_ambiguous_suffix_key_is_dropped(jdb: JournalsDB):
    """两个不同刊剥出同一个兜底键 → 该键弃用（宁可不匹配，不可错配）。"""
    jdb.upsert_jcr([{"journal_name": "Foo-Basel", "jif": 1.0, "year": 2024},
                    {"journal_name": "Foo-London", "jif": 2.0, "year": 2024}])
    assert jdb.lookup("Foo") is None
