# -*- coding: utf-8 -*-
"""papers_meta 统一资源键（rid）单测（P0-B step2，2026-09-11）。

锁定四条性质：
1. **无 DOI 资料可共存**——旧实现以 doi 为主键，第二篇无 DOI 文献必然撞主键；
2. DOI 仍能查回（identifiers 别名表）——既有调用方无需改动；
3. 主键迁移 doi→rid 幂等、不掉行；
4. 键解析 `resolve_rid` 的四个分支（rid / DOI / 未登记 DOI / md5・历史目录名）。
"""
from __future__ import annotations

import sqlite3

from paperkb.config import Roots
from paperkb.db import KBStore
from paperkb.doi import make_rid
from paperkb.models import PaperMeta


def _roots(tmp_path) -> Roots:
    return Roots(data_dir=tmp_path / "data", library_dir=tmp_path / "library",
                 kb_dir=tmp_path / "kb")


def _store(tmp_path) -> KBStore:
    s = KBStore(_roots(tmp_path))
    s.init_schema()
    return s


# ---------------------------------------------------------------- 核心：无 DOI 可共存

def test_two_non_doi_resources_coexist(tmp_path):
    """中文文献/书/学位论文没有 DOI —— 旧主键会直接撞车。"""
    s = _store(tmp_path)
    r1 = s.upsert_meta(PaperMeta(title="中文文献甲", year="2019", journal="某学报"))
    r2 = s.upsert_meta(PaperMeta(title="中文文献乙", year="2020", journal="某学报"))
    assert r1 and r2 and r1 != r2, "两篇无 DOI 资料必须得到不同 rid"
    assert len(s.list_meta()) == 2, "两篇都要落库（旧实现第二篇覆盖/报错）"
    assert s.get_meta(r1).title == "中文文献甲"
    assert s.get_meta(r2).title == "中文文献乙"


def test_non_doi_rows_are_addressable_by_rid(tmp_path):
    s = _store(tmp_path)
    rid = s.upsert_meta(PaperMeta(title="某某博士学位论文", year="2021"))
    assert rid.startswith("nd-")
    assert s.has_meta(rid)
    assert s.get_meta(rid).year == "2021"


# ---------------------------------------------------------------- 别名：DOI 仍可查

def test_lookup_by_doi_alias_still_works(tmp_path):
    """既有调用方一律用 DOI 查询 —— 迁移后必须照旧命中。"""
    s = _store(tmp_path)
    doi = "10.1002/adma.202407106"
    rid = s.upsert_meta(PaperMeta(doi=doi, title="X"))
    assert rid == make_rid("paper", doi=doi)
    assert s.get_meta(doi).title == "X"          # 按 DOI 查
    assert s.get_meta(rid).title == "X"          # 按 rid 查
    assert s.has_meta(doi) and s.has_meta(rid)
    assert s.find_rid_by_identifier("doi", doi) == rid


def test_upsert_idempotent_by_rid(tmp_path):
    s = _store(tmp_path)
    s.upsert_meta(PaperMeta(doi="10.1000/x.1", title="v1"))
    s.upsert_meta(PaperMeta(doi="10.1000/x.1", title="v2"))
    assert len(s.list_meta()) == 1, "同一 DOI 重导入应更新而非新增"
    assert s.get_meta("10.1000/x.1").title == "v2"


def test_identifiers_registry(tmp_path):
    s = _store(tmp_path)
    rid = s.upsert_meta(PaperMeta(doi="10.1000/x.1", wos_id="WOS:0001"))
    assert s.find_rid_by_identifier("wos", "WOS:0001") == rid
    assert {i["kind"] for i in s.identifiers_for(rid)} == {"doi", "wos"}
    assert s.find_rid_by_identifier("cnki", "不存在") == ""
    # 手工登记（未来 ISBN/CNKI 导入路径用）
    s.register_identifier("isbn", "978-3-527-34567-8", rid)
    assert s.find_rid_by_identifier("isbn", "978-3-527-34567-8") == rid


def test_meta_fts_uses_rid(tmp_path):
    """FTS 与主表按 rid 连接（列名已由 doi 改为 rid）。"""
    s = _store(tmp_path)
    s.upsert_meta(PaperMeta(doi="10.1000/x.1", title="Graphitic carbon nitride"))
    hits = s.search_meta("Graphitic")
    assert hits and hits[0].doi == "10.1000/x.1"


# ---------------------------------------------------------------- 迁移

def test_migration_from_legacy_doi_pk_table(tmp_path):
    """老库（doi 主键）→ 新库（rid 主键）：不掉行、幂等。"""
    roots = _roots(tmp_path)
    roots.ensure()
    c = sqlite3.connect(roots.main_db)
    c.execute("CREATE TABLE papers_meta (doi TEXT PRIMARY KEY, title TEXT DEFAULT '', "
              "year TEXT DEFAULT '', wos_id TEXT DEFAULT '')")
    c.execute("INSERT INTO papers_meta(doi,title,year) VALUES(?,?,?)",
              ("10.1002/adma.202407106", "老行", "2024"))
    c.execute("INSERT INTO papers_meta(doi,title) VALUES(?,?)", ("", "无 DOI 老行"))
    c.commit()
    c.close()

    # 2026-09-12：主键迁移已收编进迁移层（migrations/0003_meta_pk_rid.py），
    # 老库由迁移 runner 升级（不再由 init_schema 内联 ALTER 处理）
    from paperkb.migrations import run_migrations
    from paperkb.version import DATA_FORMAT

    run_migrations(roots, target_format=DATA_FORMAT)
    s = KBStore(roots)
    s.init_schema()

    m = s.get_meta("10.1002/adma.202407106")
    assert m and m.title == "老行"
    assert m.rid == make_rid("paper", doi="10.1002/adma.202407106")
    assert len(s.list_meta()) == 2, "老库两行都要搬过来"
    # 幂等：重复 init 不报错、不丢行、不重复
    s.init_schema()
    assert len(s.list_meta()) == 2


# ---------------------------------------------------------------- 键解析

def test_resolve_rid_branches(tmp_path):
    s = _store(tmp_path)
    assert s.resolve_rid("") == ""
    assert s.resolve_rid("doi-10.1000_x.1") == "doi-10.1000_x.1"   # 已是 rid
    assert s.resolve_rid("nd-3f9a1c22b7d4") == "nd-3f9a1c22b7d4"
    assert s.resolve_rid("10.1000/x.1") == make_rid("paper", doi="10.1000/x.1")  # 未登记 DOI
    assert s.resolve_rid("a" * 32) == "a" * 32                  # md5 目录名：原样
    assert s.resolve_rid("JMADE-D-26-04277_R1_reviewer") == "JMADE-D-26-04277_R1_reviewer"
