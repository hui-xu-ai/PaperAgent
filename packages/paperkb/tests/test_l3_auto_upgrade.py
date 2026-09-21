# -*- coding: utf-8 -*-
"""L3 自动升级的 done 判据：**必须带产物存在性**（2026-09-21 adma 实测修复）。

复现的真实场景（用户重测，日志实证）：
    清产物重解析 → `compile_jobs` 里 L3 行仍是 43 分钟前的 `done`
    → `_maybe_auto_l3` 只判 status ⇒ 静默跳过 ⇒ `_relations.md` 永不重建
    → 界面继续显示"已完成 L1, L2, L3"。

`queue()` 早在 2026-09-11 就修过同类问题（加 `_artifact_exists` 第二判据，注释点名
同一个 DOI），但 `_maybe_auto_l3` 漏了 ⇒ 判据分叉。本文件把两边口径钉在一起。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from paperkb import api
from paperkb.config import Roots
from paperkb.doi import doi_to_dirname
from paperkb.models import PaperMeta

DOI = "10.1002/adma.202407106"


@pytest.fixture()
def env(tmp_path: Path):
    roots = Roots(data_dir=tmp_path / "data", library_dir=tmp_path / "library",
                  kb_dir=tmp_path / "kb").ensure()
    api.init_kb(roots)
    store = api._need_store()          # noqa: SLF001
    store.upsert_meta(PaperMeta(doi=DOI, title="T", journal="Advanced Materials"))
    folder = roots.kb_dir / doi_to_dirname(DOI)
    folder.mkdir(parents=True, exist_ok=True)
    # queue() 的文档检查：没有 document.json 会返回 skipped_no_doc，入队不成立
    (folder / "document.json").write_text("{}", encoding="utf-8")
    yield api, roots, store, folder
    api._store = None                  # noqa: SLF001
    api._settings = None               # noqa: SLF001
    api._compiler = None               # noqa: SLF001


def _fake_score(monkeypatch, *, score: float = 4.5, ai: float | None = 4.0):
    """隔离变量：只保留"总分 + AI 分可用性"，不牵扯 journals.db。"""
    monkeypatch.setattr(api, "value_score_for", lambda doi: {
        "score": score, "level": "L2",
        "parts": {"ai_value": {"value": ai, "available": ai is not None}},
    })


def _status(store) -> str:
    return (store.get_job(DOI, "L3") or {}).get("status", "<无行>")


def test_stale_done_row_without_artifact_requeues(env, monkeypatch):
    """回归钉：陈旧 done 行 + 产物缺失 → 必须重新入队（旧代码在此静默跳过）。"""
    api_mod, _roots, store, folder = env
    _fake_score(monkeypatch)
    store.upsert_job(DOI, "L3", status="done")          # 陈旧 done 行
    assert not (folder / "_relations.md").exists()      # 产物已被清

    api_mod._compiler._maybe_auto_l3(DOI)               # noqa: SLF001

    assert _status(store) == "queued", "陈旧 done 行挡住了重建（判据缺产物存在性）"


def test_done_with_artifact_stays_done(env, monkeypatch):
    """产物在盘 → 不重复入队（幂等仍然成立，别把修复变成"每次都重编"）。"""
    api_mod, _roots, store, folder = env
    _fake_score(monkeypatch)
    store.upsert_job(DOI, "L3", status="done")
    (folder / "_relations.md").write_text("# 关系\n", encoding="utf-8")

    api_mod._compiler._maybe_auto_l3(DOI)               # noqa: SLF001

    assert _status(store) == "done"


def test_below_threshold_not_queued(env, monkeypatch):
    """分数不到阈值 → 不入队（且不得建出 L3 行）。"""
    api_mod, _roots, store, _folder = env
    _fake_score(monkeypatch, score=3.18)

    api_mod._compiler._maybe_auto_l3(DOI)               # noqa: SLF001

    assert _status(store) == "<无行>"


def test_ai_unavailable_not_queued(env, monkeypatch):
    """AI 分不可用 → 不入队（L3 的判据要求 AI 附加分存在）。"""
    api_mod, _roots, store, _folder = env
    _fake_score(monkeypatch, ai=None)

    api_mod._compiler._maybe_auto_l3(DOI)               # noqa: SLF001

    assert _status(store) == "<无行>"


def test_first_time_queues(env, monkeypatch):
    """无 L3 行（首次）→ 正常入队。"""
    api_mod, _roots, store, _folder = env
    _fake_score(monkeypatch)

    api_mod._compiler._maybe_auto_l3(DOI)               # noqa: SLF001

    assert _status(store) == "queued"
