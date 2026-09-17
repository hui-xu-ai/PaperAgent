# -*- coding: utf-8 -*-
"""重试入口修复单测（★2026-09-17）。

用户实测缺陷：`papers.pdf_path` 登记的是**上传暂存件** `work/upload/<run_id>/<名>.pdf`，
成功解析后被 `_clean_upload_staging` 清掉（`work/` 属"随时可清"区）；权威副本在
`library/<资源>/source.pdf`。于是"解析曾成功、之后失败"的篇点「重试」必然拿到不存在
的路径 → 报 `PAPER-0001 输入文件不存在或不可读`（文案误导为"PDF 坏了"）。
取证：`.dsh-memory/project/FINDING-RETRY-STAGING-20260917.md`。

修复：`TaskManager.repair_pdf_path`（入队前自愈 + 写回 `papers.pdf_path`）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import Settings
from app.services.store import Store
from app.services.task_service import MISSING_SOURCE_MSG, TaskManager


@pytest.fixture
def env(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "t.db"),
        engine_work_root=str(tmp_path / "library"),
        dual_work_root=str(tmp_path / "dual"),
        engine_input_root=str(tmp_path / "upload"),
    )
    return {"tmp": tmp_path, "settings": settings, "store": Store(settings.db_path)}


def _make_library_paper(env, rid: str = "10.1038_ncomms8258", with_source: bool = True):
    """造一篇"暂存件已被清理、权威副本在 library"的论文记录，返回 paper_id。"""
    lib = env["tmp"] / "library" / rid
    lib.mkdir(parents=True, exist_ok=True)
    doc = lib / "document.json"
    doc.write_text(json.dumps({"metadata": {"doi": rid}}, ensure_ascii=False),
                   encoding="utf-8")
    if with_source:
        (lib / "source.pdf").write_bytes(b"%PDF-1.4 fake")
    gone = env["tmp"] / "upload" / "run1" / "paper.pdf"      # 不创建 = 已被清理
    pid = env["store"].create_paper(str(gone), title="t", pdf_md5="d" * 32,
                                    pdf_name="paper.pdf")
    env["store"].update_paper(pid, doc_json=str(doc), status="failed", error="boom")
    return pid


def test_repairs_path_from_library_source(env):
    """登记路径失效 → 回填 library/<RID>/source.pdf，并写回 papers.pdf_path"""
    pid = _make_library_paper(env)
    paper = env["store"].get_paper(pid)
    ready = TaskManager.repair_pdf_path(env["store"], env["settings"], None, paper)
    assert ready, "应能从 library 权威副本回填"
    assert Path(ready).name == "source.pdf"
    assert env["store"].get_paper(pid)["pdf_path"] == ready, "必须写回登记（重试不再兜底）"


def test_keeps_valid_registered_path(env):
    """登记路径仍可用 → 原样返回（不碰库、不做多余拷贝）"""
    pid = _make_library_paper(env)
    real = env["tmp"] / "upload" / "run2" / "ok.pdf"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_bytes(b"%PDF-1.4 real")
    env["store"].update_paper(pid, pdf_path=str(real))
    ready = TaskManager.repair_pdf_path(
        env["store"], env["settings"], None, env["store"].get_paper(pid))
    assert ready == str(real)
    assert env["store"].get_paper(pid)["pdf_path"] == str(real)


def test_returns_empty_when_both_missing(env):
    """暂存件与 library 都没有 → 返回 ""（调用方给 409 + 可操作文案，不伪装成解析失败）"""
    pid = _make_library_paper(env, with_source=False)
    ready = TaskManager.repair_pdf_path(
        env["store"], env["settings"], None, env["store"].get_paper(pid))
    assert ready == ""


def test_missing_message_is_actionable():
    """用户可见文案必须说清"怎么办"，且带上失效路径便于排查"""
    old = "/x/work/upload/run1/paper.pdf"
    msg = MISSING_SOURCE_MSG % old
    assert "重新导入" in msg, "必须给出可操作建议"
    assert old in msg, "必须带上失效的登记路径"
    assert "解析失败" not in msg, "不得再伪装成解析失败（那是本次修复针对的误导文案）"


def test_legacy_record_without_doc_json(env):
    """老记录可能没有 doc_json → 不炸，返回 ""（行为与修复前一致但文案可操作）"""
    pid = env["store"].create_paper(str(env["tmp"] / "nope.pdf"), title="t")
    env["store"].update_paper(pid, status="failed")
    ready = TaskManager.repair_pdf_path(
        env["store"], env["settings"], None, env["store"].get_paper(pid))
    assert ready == ""
