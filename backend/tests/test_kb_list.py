# -*- coding: utf-8 -*-
"""A4 知识库聚合列表 kb_list 单测（paperkb 层，临时 db 夹具）。

直接测 paperkb.api.kb_list（backend KbMetaService 为纯透传；避免触碰真实
APP_DATA_DIR）。评分在部分用例中 monkeypatch 为固定值，保证过滤/排序确定性。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from paperkb import api
from paperkb.config import Roots
from paperkb.doi import doi_to_dirname, normalize_doi
from paperkb.models import PaperMeta


@pytest.fixture()
def roots(tmp_path: Path) -> Roots:
    return Roots(data_dir=tmp_path / "data",
                 library_dir=tmp_path / "library",
                 kb_dir=tmp_path / "kb").ensure()


@pytest.fixture()
def kbsetup(roots: Roots):
    """初始化 paperkb（临时库）；测后复位全局单例。"""
    api.init_kb(roots)
    yield api
    api._store = None            # noqa: SLF001
    api._settings = None         # noqa: SLF001
    api._journals = None         # noqa: SLF001
    api._compiler = None         # noqa: SLF001


@pytest.fixture()
def fake_scores(monkeypatch):
    """固定评分 map：{doi: (score, level)}，替换真实价值分计算。"""
    def _set(mapping: dict[str, tuple[float, str]]) -> None:
        rows = [{"doi": doi, "score": sc, "level": lv,
                 "title": "", "journal": "", "year": ""}
                for doi, (sc, lv) in mapping.items()]
        monkeypatch.setattr(api, "list_scores", lambda: rows)
    return _set


def _add_meta(store, doi: str, *, title: str = "T", journal: str = "Nature",
              year: str = "2024", times_cited: int = 0) -> str:
    doi = normalize_doi(doi)
    store.upsert_meta(PaperMeta(doi=doi, title=title, journal=journal,
                                year=year, times_cited=times_cited))
    return doi


_LEVEL_ARTIFACT = {"L1": "_note.md", "L2": "_wiki.md", "L3": "_relations.md"}


def _add_job(store, doi: str, level: str, status: str, error: str = "",
             roots: Roots | None = None) -> None:
    """写 compile_jobs 一行。

    2026-09-21：status=done 且给了 roots 时**同时把产物文件铺出来**。kb_list 现在只把
    “产物确实在盘上”的 done 行算作已编译（done 行 + 产物缺失 ⇒ 归入 stale、显示“待重建”），
    所以 fixture 里凭空写一个 done 行已经不代表“已编译完成”了。
    """
    store.upsert_job(normalize_doi(doi), level, status=status, error=error)
    if status == "done" and roots is not None:
        _make_kb(roots, doi, (_LEVEL_ARTIFACT[level],))


def _make_kb(roots: Roots, doi: str, files: tuple[str, ...] = ()) -> None:
    d = roots.kb_dir / doi_to_dirname(doi)
    for name in files:
        p = d / name
        if name == "images":
            p.mkdir(parents=True, exist_ok=True)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x", encoding="utf-8")


def _kb(**kw) -> dict:
    """kb_list 便捷入口（本文件用例）。

    这些用例只验证聚合/过滤/排序/分页逻辑，不铺 kb 目录磁盘态，而 kb_list 自
    2026-09-19 起默认 on_disk_only=True（过滤磁盘已删的幽灵记录）——不关掉就
    会被整体过滤成空列表。幽灵过滤的默认行为由 test_kb_list_on_disk_only_default 覆盖。
    """
    kw.setdefault("on_disk_only", False)
    return api.kb_list(**kw)


# ---------------------------------------------------------------- 基本聚合

def test_kb_list_empty(kbsetup):
    r = _kb()
    assert r == {"items": [], "total": 0, "page": 1, "page_size": 50}


def test_kb_list_basic_aggregation(kbsetup, fake_scores, roots):
    store = api._store  # noqa: SLF001
    d1 = _add_meta(store, "10.1000/a.1", title="Alpha paper", journal="Nature", year="2024")
    d2 = _add_meta(store, "10.1000/b.2", title="Beta paper", journal="Science", year="2022")
    d3 = _add_meta(store, "10.1000/c.3", title="Gamma paper", journal="Cell", year="2020")
    fake_scores({d1: (4.5, "L3"), d2: (3.0, "L2"), d3: (1.2, "L1")})

    # 编译状态：d1 完成 L1/L2 + L3 排队；d2 失败带错误；d3 无任务
    _add_job(store, d1, "L1", "done", roots=roots)
    _add_job(store, d1, "L2", "done", roots=roots)
    _add_job(store, d1, "L3", "queued")
    _add_job(store, d2, "L1", "failed", error="llm timeout")
    _add_job(store, d2, "L2", "pending")          # pending 语义=排队中

    # kb 原文层四件：d1 全有；d2 部分；d3 无目录
    _make_kb(roots, d1, ("source.pdf", "en.md", "document.json", "images"))
    _make_kb(roots, d2, ("en.md",))

    r = _kb()
    assert r["total"] == 3 and r["page"] == 1 and r["page_size"] == 50
    by_doi = {it["doi"]: it for it in r["items"]}
    assert set(by_doi) == {d1, d2, d3}

    a = by_doi[d1]
    assert a["title"] == "Alpha paper" and a["journal"] == "Nature" and a["year"] == "2024"
    assert a["value_score"] == 4.5 and a["level"] == "L3"
    assert a["in_kb"] is True
    assert a["source_files"] == {"source_pdf": True, "en_md": True,
                                 "document_json": True, "images": True}
    assert a["compiled"] == ["L1", "L2"] and a["queued"] == ["L3"]
    assert a["last_error"] == ""

    b = by_doi[d2]
    assert b["in_kb"] is True and b["source_files"]["en_md"] is True
    assert b["source_files"]["source_pdf"] is False
    assert b["compiled"] == [] and b["queued"] == ["L2"]  # L1 已 failed；pending 计入排队
    assert b["last_error"] == "llm timeout"

    c = by_doi[d3]
    assert c["in_kb"] is False
    assert c["source_files"] == {"source_pdf": False, "en_md": False,
                                 "document_json": False, "images": False}
    assert c["compiled"] == [] and c["queued"] == [] and c["last_error"] == ""

    # 默认 value 降序
    assert [it["doi"] for it in r["items"]] == [d1, d2, d3]


# ---------------------------------------------------------------- 过滤

def test_kb_list_filters(kbsetup, fake_scores, roots):
    store = api._store  # noqa: SLF001
    d1 = _add_meta(store, "10.1000/a.1", journal="Nature", year="2024")
    d2 = _add_meta(store, "10.1000/b.2", journal="Science Advances", year="2022")
    d3 = _add_meta(store, "10.1000/c.3", journal="Cell", year="2020")
    fake_scores({d1: (4.5, "L3"), d2: (2.0, "L1"), d3: (3.3, "L2")})
    _add_job(store, d1, "L1", "done", roots=roots)
    _add_job(store, d2, "L1", "queued")           # 排队 ≠ 已编译

    assert [it["doi"] for it in _kb(journal="science")["items"]] == [d2]
    assert [it["doi"] for it in _kb(journal="SCIENCE")["items"]] == [d2]
    assert [it["doi"] for it in _kb(journal="nat")["items"]] == [d1]

    assert {it["doi"] for it in _kb(score_min=3.0)["items"]} == {d1, d3}
    assert {it["doi"] for it in _kb(score_min=5.0)["items"]} == set()

    assert [it["doi"] for it in _kb(compile_status="done")["items"]] == [d1]
    assert {it["doi"] for it in _kb(compile_status="none")["items"]} == {d2, d3}
    assert [it["doi"] for it in _kb(compile_status="L1")["items"]] == [d1]
    assert [it["doi"] for it in _kb(compile_status="l3")["items"]] == []

    # 组合过滤
    assert [it["doi"] for it in _kb(score_min=3.0, compile_status="done")["items"]] == [d1]


def test_kb_list_journal_filter_not_leaked_by_disk_branch(kbsetup, fake_scores, roots):
    """回归钉：磁盘上有产物的篇目，被 journal 过滤器挡掉后不得被磁盘分支原样加回。

    根因：`matched` 的登记点排在 journal/score_min 过滤器之后 ⇒ 被过滤器挡掉的篇目
    在 3b「磁盘分支」里"复活"，而那个分支拿不到 journal 字段、也就没有 journal 过滤，
    表现为"按期刊筛选筛不干净"。2026-09-21 修 kb_list 产物判据时实测暴露。
    """
    store = api._store  # noqa: SLF001
    d1 = _add_meta(store, "10.1000/a.1", journal="Nature", year="2024")
    d2 = _add_meta(store, "10.1000/b.2", journal="Science Advances", year="2022")
    _make_kb(roots, d1, ("_note.md",))          # 两篇磁盘上都有产物
    _make_kb(roots, d2, ("_note.md",))

    assert [it["doi"] for it in _kb(journal="science")["items"]] == [d2]
    assert [it["doi"] for it in _kb(journal="nature")["items"]] == [d1]
    assert _kb(journal="cell")["items"] == []


def test_kb_list_search_q(kbsetup, fake_scores):
    store = api._store  # noqa: SLF001
    d1 = _add_meta(store, "10.1000/a.1", title="Quantum computing advances", journal="Nature")
    d2 = _add_meta(store, "10.1000/b.2", title="Deep learning for vision", journal="Science")
    d3 = _add_meta(store, "10.1000/c.3", title="Quantum dots in display", journal="Cell")
    fake_scores({d1: (4.0, "L2"), d2: (3.0, "L2"), d3: (2.0, "L1")})

    got = _kb(q="quantum")
    assert got["total"] == 2
    assert {it["doi"] for it in got["items"]} == {d1, d3}

    # q + 其他过滤叠加
    got = _kb(q="quantum", journal="cell")
    assert [it["doi"] for it in got["items"]] == [d3]


def test_kb_list_real_scoring(kbsetup):
    """真实价值分路径冒烟：journals 未配置（journal_info=None）也能出分数与等级。"""
    store = api._store  # noqa: SLF001
    _add_meta(store, "10.1000/a.1", title="Real scoring", journal="Nature",
              year="2024", times_cited=50)
    r = _kb()
    assert r["total"] == 1
    it = r["items"][0]
    assert isinstance(it["value_score"], float) and 0.0 <= it["value_score"] <= 5.0
    assert it["level"] in ("L1", "L2", "L3")


# ---------------------------------------------------------------- 排序 / 分页

def test_kb_list_sort_pagination(kbsetup, fake_scores):
    store = api._store  # noqa: SLF001
    metas = [
        ("10.1000/a.1", "Alpha", "2024", 2.0),
        ("10.1000/b.2", "Beta", "2023", 5.0),
        ("10.1000/c.3", "Gamma", "2022", 1.0),
        ("10.1000/d.4", "Delta", "2021", 3.0),
        ("10.1000/e.5", "Epsilon", "2020", 4.0),
    ]
    scores = {}
    for doi, title, year, sc in metas:
        d = _add_meta(store, doi, title=title, year=year)
        scores[d] = (sc, "L2")
    fake_scores(scores)
    order = [normalize_doi(m[0]) for m in metas]

    assert [it["doi"] for it in _kb(sort="value")["items"]] == \
        [order[1], order[4], order[3], order[0], order[2]]        # 5.0,4.0,3.0,2.0,1.0
    assert [it["doi"] for it in _kb(sort="year")["items"]] == order  # 2024→2020
    assert [it["doi"] for it in _kb(sort="title")["items"]] == \
        [order[0], order[1], order[3], order[4], order[2]]        # Alpha,Beta,Delta,Epsilon,Gamma
    # 未知 sort 回落 value
    assert [it["doi"] for it in _kb(sort="bogus")["items"]] == \
        [order[1], order[4], order[3], order[0], order[2]]

    # 分页（value 序）：page2/2 → 3.0,2.0
    r = _kb(sort="value", page=2, page_size=2)
    assert [it["doi"] for it in r["items"]] == [order[3], order[0]]
    assert r["total"] == 5 and r["page"] == 2 and r["page_size"] == 2

    # 越界页 → 空 items，total 不变
    r = _kb(sort="value", page=99, page_size=2)
    assert r["items"] == [] and r["total"] == 5

    # page_size 上限 200 / 下限 1；page 下限 1
    assert _kb(page_size=500)["page_size"] == 200
    assert _kb(page_size=0)["page_size"] == 1
    assert _kb(page=0)["page"] == 1


# ---------------------------------------------------------------- 磁盘幽灵过滤

def test_kb_list_on_disk_only_default(kbsetup, fake_scores, roots):
    """2026-09-19 默认行为：kb 目录已删的幽灵记录默认不出现。

    这里刻意用裸 api.kb_list()（不套 _kb）验证**默认值**本身；
    on_disk_only=False 时才把数据库里仍在、磁盘上已无目录的记录放出来。
    """
    store = api._store  # noqa: SLF001
    d1 = _add_meta(store, "10.1000/a.1", title="On disk paper")
    d2 = _add_meta(store, "10.1000/b.2", title="Ghost paper")
    fake_scores({d1: (4.0, "L2"), d2: (3.0, "L2")})
    _make_kb(roots, d1, ("en.md",))          # 只有 d1 落磁盘

    r = api.kb_list()
    assert r["total"] == 1 and [it["doi"] for it in r["items"]] == [d1]
    assert r["items"][0]["on_disk"] is True

    got = api.kb_list(on_disk_only=False)
    assert got["total"] == 2
    by = {it["doi"]: it for it in got["items"]}
    assert by[d2]["on_disk"] is False and by[d2]["in_kb"] is False
