# -*- coding: utf-8 -*-
"""编译状态的“产物真实性”：done 行 ≠ 产物还在盘上（2026-09-21 用户报障修复）。

用户现象（实测 adma）：界面显示“已完成 L1, L2, L3”，但 `_relations.md` 早被清理过。
根因：`compile_jobs` 的 done 行在产物被删后**仍是 done**，而显示侧只读 status
（`kb_list` 的 bib 分支直接 `st == "done"` 就计入 compiled）。

本文件钉三件事，防止判据再分叉：
  1. `api.compile_status` 每行带 `artifact_exists`（与入队幂等同源）
  2. `api.kb_list` 把“done 但产物不在”的等级从 `compiled` 挪到 `stale`
  3. `_mark_done` / 编译失败路径不再把 `value_score` 冲成 0
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
    # queue()/kb_status() 都要看在盘文件；en.md 让 kb_list 的磁盘分支认这一篇
    (folder / "document.json").write_text("{}", encoding="utf-8")
    (folder / "en.md").write_text("x", encoding="utf-8")
    yield api, roots, store, folder
    api._store = None                  # noqa: SLF001
    api._settings = None               # noqa: SLF001
    api._journals = None               # noqa: SLF001
    api._compiler = None               # noqa: SLF001


def _row(level: str) -> dict:
    return next(r for r in api.compile_status() if r["level"] == level)


def _item() -> dict:
    return api.kb_list(q=DOI, on_disk_only=False)["items"][0]


# ---------------------------------------------------------------- 1) compile_status
def test_compile_status_artifact_exists_true_then_false(env):
    """产物在盘 → True；删掉 → False。这是显示侧唯一可信的来源。"""
    _api, _roots, store, folder = env
    (folder / "_relations.md").write_text("x", encoding="utf-8")
    store.upsert_job(DOI, "L3", status="done")
    assert _row("L3")["artifact_exists"] is True

    (folder / "_relations.md").unlink()
    assert _row("L3")["artifact_exists"] is False


def test_compile_status_unknown_level_keeps_old_semantics(env):
    """未知等级（_artifact_path 返回 None）→ 仍按“在”处理，不因新判据改变旧行为。"""
    _api, _roots, store, _folder = env
    store.upsert_job(DOI, "L9", status="done")
    assert _row("L9")["artifact_exists"] is True


# ---------------------------------------------------------------- 2) kb_list
def test_kb_list_counts_stale_done_as_stale_not_compiled(env):
    """回归钉：done 行 + 产物缺失 → 不得计入 compiled（旧代码在此显示“已完成 L3”）。"""
    _api, _roots, store, _folder = env
    store.upsert_job(DOI, "L3", status="done")      # 陈旧 done，产物不在盘
    it = _item()
    assert "L3" not in it["compiled"], "产物缺失的 done 行被当成已完成（用户报障复现）"
    assert it["stale"] == ["L3"]


def test_kb_list_keeps_real_done_compiled_and_no_stale(env):
    """产物真在盘 → 仍计入 compiled、stale 为空（别把修复做成“永不显示已完成”）。"""
    _api, _roots, store, folder = env
    (folder / "_relations.md").write_text("x", encoding="utf-8")
    store.upsert_job(DOI, "L3", status="done")
    it = _item()
    assert it["compiled"] == ["L3"]
    assert it["stale"] == []


def test_kb_list_stale_paper_not_l3_eligible(env):
    """stale 的 L2 → 不算“L2 已完成”，故不报可升级 L3（否则会推着用户去编 L3）。"""
    _api, _roots, store, _folder = env
    store.upsert_job(DOI, "L2", status="done")
    it = _item()
    assert it["stale"] == ["L2"]
    assert it["l3_eligible"] is False


# ---------------------------------------------------------------- 3) value_score 不被冲掉
def test_mark_done_preserves_value_score(env):
    """`upsert_job` 是 INSERT OR REPLACE，缺省 0.0 → _mark_done 会把入队分冲成 0。"""
    _api, _roots, store, _folder = env
    c = api._compiler                 # noqa: SLF001
    assert c.queue(DOI, "L3", value_score=3.9)["status"] == "queued"
    c._mark_done(DOI, "L3")           # noqa: SLF001
    row = store.get_job(DOI, "L3")
    assert row["status"] == "done"
    assert row["value_score"] == pytest.approx(3.9), "完成时价值分被冲成 0"
    assert row["done_at"], "done_at 仍要写（2026-09-12 的修复不能回退）"


def test_process_next_failure_preserves_value_score(env):
    """编译抛 CompileError → 置 failed 时同样不得丢掉入队分。"""
    from paperkb.compile import CompileError

    _api, _roots, store, _folder = env
    c = api._compiler                 # noqa: SLF001
    c.queue(DOI, "L1", value_score=2.7)

    def _boom(*_a, **_kw):
        raise CompileError("模拟编译失败")

    c.compile = _boom                 # noqa: SLF001
    r = c.process_next()
    assert r["error"] == "模拟编译失败"
    row = store.get_job(DOI, "L1")
    assert row["status"] == "failed"
    assert row["value_score"] == pytest.approx(2.7), "失败时价值分被冲成 0"
