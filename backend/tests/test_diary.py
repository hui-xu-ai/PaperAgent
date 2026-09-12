# -*- coding: utf-8 -*-
"""backend 文献阅读日记 diary 单测（paperkb 层，临时 db 夹具）。

直接测 paperkb.diary（backend KbMetaService 为纯透传；避免触碰真实 APP_DATA_DIR）。
覆盖：days 聚合 + 月过滤、day 详情（含笔记读）、note 写读覆盖、export。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
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


def _add_meta(store, doi: str, title: str, imported_at: str) -> str:
    """写 papers_meta，并把 imported_at 固定为指定值（upsert_meta 会用 now 覆盖，故补 UPDATE）。"""
    doi = normalize_doi(doi)
    store.upsert_meta(PaperMeta(doi=doi, title=title))
    conn = store._conn()  # noqa: SLF001
    conn.execute("UPDATE papers_meta SET imported_at=? WHERE doi=?", (imported_at, doi))
    conn.commit()
    conn.close()
    return doi


def _make_kb(roots: Roots, doi: str, files: tuple[str, ...] = ()) -> Path:
    """构建 kb/<doi_to_dirname>/ 目录（en.md/en_zh.md/document.json/_note.md 等）。"""
    d = roots.kb_dir / doi_to_dirname(doi)
    for name in files:
        p = d / name
        if name == "images":
            p.mkdir(parents=True, exist_ok=True)
        elif name == "document.json":
            p.parent.mkdir(parents=True, exist_ok=True)
            data = {"paragraphs": [
                {"para_id": "p1", "section": "Abstract",
                 "text_en": "A", "text_zh": "甲"},
                {"para_id": "p2", "section": "Intro",
                 "text_en": "B", "text_zh": ""},
            ]}
            p.write_text(json.dumps(data), encoding="utf-8")
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x", encoding="utf-8")
    return d


def _touch_mtime(p: Path, date: str) -> None:
    """把目录/文件 mtime 设为指定日期（纳入日可控）。"""
    ts = datetime.strptime(date, "%Y-%m-%d").timestamp()
    os.utime(p, (ts, ts))


# ---------------------------------------------------------------- days 聚合

def test_diary_days_empty(kbsetup):
    from paperkb import diary as d
    store = kbsetup._store  # noqa: SLF001
    r = d.diary_days(store, store.roots)
    assert r["days"] == [] and r["months"] == []


def test_diary_days_aggregation(kbsetup, roots):
    store = kbsetup._store  # noqa: SLF001
    d1 = _add_meta(store, "10.1000/a.1", "Alpha", "2026-08-26T09:00:00")
    _add_meta(store, "10.1000/b.2", "Beta", "2026-08-26T10:00:00")
    _add_meta(store, "10.1000/c.3", "Gamma", "2026-08-27T08:00:00")

    # d1 已纳入 kb → 纳入日 = kb 目录 mtime（设为 2026-08-28）
    kbd = _make_kb(roots, d1, ("en.md", "_note.md"))
    _touch_mtime(kbd, "2026-08-28")

    # 用户笔记：2026-08-29
    from paperkb import diary as pd
    pd.diary_note_write(roots, "2026-08-29", "读完了 Alpha")

    r = pd.diary_days(store, roots)
    by_date = {x["date"]: x for x in r["days"]}
    assert set(by_date) == {"2026-08-26", "2026-08-27", "2026-08-28", "2026-08-29"}
    assert by_date["2026-08-26"]["imports"] == 2
    assert by_date["2026-08-27"]["imports"] == 1
    assert by_date["2026-08-28"]["kb"] == 1
    assert by_date["2026-08-29"]["notes"] == 1

    # 月聚合
    months = {m["month"]: m["count"] for m in r["months"]}
    assert months == {"2026-08": 5}

    # month 过滤
    r7 = pd.diary_days(store, roots, month="2026-07")
    assert r7["days"] == []
    assert r7["months"] == []
    r8 = pd.diary_days(store, roots, month="2026-08")
    assert {x["date"] for x in r8["days"]} == by_date.keys()


# ---------------------------------------------------------------- day 详情

def test_diary_day_detail(kbsetup, roots):
    from paperkb import diary as pd
    store = kbsetup._store  # noqa: SLF001
    d1 = _add_meta(store, "10.1000/a.1", "Alpha", "2026-08-26T09:00:00")
    d2 = _add_meta(store, "10.1000/b.2", "Beta", "2026-08-26T10:00:00")
    _add_meta(store, "10.1000/c.3", "Gamma", "2026-08-27T08:00:00")

    # d1：完整链路（已解析+已翻译+已纳入）；d2：仅元数据（pending）
    _make_kb(roots, d1, ("en.md", "en_zh.md", "document.json", "_note.md"))

    r = pd.diary_day(store, roots, "2026-08-26")
    assert r["date"] == "2026-08-26"
    by_doi = {it["doi"]: it for it in r["imports"]}
    assert set(by_doi) == {d1, d2}

    a = by_doi[d1]
    assert a["title"] == "Alpha" and a["status"] == "translated"
    assert a["parsed"] is True and a["translated"] is True and a["in_kb"] is True
    assert a["compiled"] is True and a["translated_paragraphs"] == 1

    b = by_doi[d2]
    assert b["title"] == "Beta" and b["status"] == "pending"
    assert b["parsed"] is False and b["translated"] is False and b["in_kb"] is False


def test_diary_day_other_day_empty(kbsetup, roots):
    from paperkb import diary as pd
    store = kbsetup._store  # noqa: SLF001
    _add_meta(store, "10.1000/a.1", "Alpha", "2026-08-26T09:00:00")
    r = pd.diary_day(store, roots, "2026-08-27")
    assert r["date"] == "2026-08-27" and r["imports"] == [] and r["note"] == ""


# ---------------------------------------------------------------- 笔记写读

def test_diary_note_write_read_overwrite(kbsetup, roots):
    from paperkb import diary as pd
    store = kbsetup._store  # noqa: SLF001
    _add_meta(store, "10.1000/a.1", "Alpha", "2026-08-26T09:00:00")

    r = pd.diary_note_write(roots, "2026-08-26", "今天读 Alpha，收获很多。")
    assert r["ok"] is True and r["date"] == "2026-08-26"
    assert Path(r["file"]).exists()

    day = pd.diary_day(store, roots, "2026-08-26")
    assert day["note"] == "今天读 Alpha，收获很多。"

    # 覆盖写
    pd.diary_note_write(roots, "2026-08-26", "更正：其实是 Beta。")
    assert pd.diary_day(store, roots, "2026-08-26")["note"] == "更正：其实是 Beta。"

    # 无该天笔记 → 空字符串
    assert pd.diary_day(store, roots, "2026-08-27")["note"] == ""


def test_diary_note_invalid_date(kbsetup, roots):
    from paperkb import diary as pd
    with pytest.raises(ValueError):
        pd.diary_note_write(roots, "2026/08/26", "x")
    with pytest.raises(ValueError):
        pd.diary_day(kbsetup._store, roots, "bad-date")  # noqa: SLF001


# ---------------------------------------------------------------- 导出

def test_diary_export_desc(kbsetup, roots):
    from paperkb import diary as pd
    store = kbsetup._store  # noqa: SLF001
    _add_meta(store, "10.1000/a.1", "Alpha", "2026-08-26T09:00:00")
    pd.diary_note_write(roots, "2026-08-27", "第二天的笔记")
    pd.diary_note_write(roots, "2026-08-26", "第一天的笔记")

    r = pd.diary_export(roots)
    assert r["count"] == 2
    assert r["text"].index("## 2026-08-27") < r["text"].index("## 2026-08-26")
    assert "第一天的笔记" in r["text"] and "第二天的笔记" in r["text"]


def test_diary_export_empty(kbsetup, roots):
    from paperkb import diary as pd
    r = pd.diary_export(roots)
    assert r["count"] == 0
