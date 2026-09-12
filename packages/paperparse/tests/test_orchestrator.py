#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: tests/test_orchestrator.py
功能: T14 编排器测试：正常管线 / 故障注入 / MinerU 降级链 / 断点续跑
对外接口: 无（测试）
版本: v1.0.0 (2025-xx-xx)
版本历史:
  v1.0.0 初始版本
"""
import pymupdf
import pytest
from pathlib import Path

from paperparse.config import AppConfig
from paperparse.middleware.audit import Auditor
from paperparse.middleware.errors import PaperError
from paperparse.middleware.orchestrator import Orchestrator
from paperparse.middleware.stages import Stage


def _make_pdf(path):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "My Test Paper Title", fontsize=16)
    page.insert_text((72, 100), "Alice Doe, Bob Smith", fontsize=11)
    page.insert_text((72, 130), "This is the abstract paragraph of the test paper.", fontsize=10)
    page.insert_text((72, 160), "1. Introduction", fontsize=12)
    page.insert_text((72, 190), "Introduction body text of the test paper.", fontsize=10)
    doc.save(str(path))
    doc.close()


def _run(pdf, tmp_work, **kw):
    cfg = AppConfig()
    return Orchestrator(cfg).run(pdf, out_dir=tmp_work / "out", **kw)


def test_pipeline_success(tmp_work):
    pdf = tmp_work / "test.pdf"
    _make_pdf(pdf)
    result = _run(pdf, tmp_work)
    assert result.status == "success"
    assert result.stats.pages == 1
    assert result.error is None
    assert Path(result.outputs.md).exists()
    assert "My Test Paper Title" in Path(result.outputs.md).read_text(encoding="utf-8")
    assert Path(result.outputs.audit_dir).exists()
    assert (Path(result.outputs.audit_dir) / "report.html").exists()
    # 审计事件
    events = Auditor(result.run_id, result.outputs.audit_dir).load_from_disk()
    assert len(events) >= 8
    assert any(e.kind == "error" for e in events) is False


def test_fault_injection(tmp_work):
    """阶段抛 PaperError → failed + 错误信封 + 审计 error 事件"""
    pdf = tmp_work / "test.pdf"
    _make_pdf(pdf)

    class BoomStage(Stage):
        name = "SX_boom"

        def run(self, ctx):
            raise PaperError("PAPER-0020", stage=self.name, detail={"why": "injected"})

    orch = Orchestrator(AppConfig())
    orch._stages = [BoomStage()]
    result = orch.run(pdf, out_dir=tmp_work / "out2")
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "PAPER-0020"
    events = Auditor(result.run_id, result.outputs.audit_dir).load_from_disk()
    assert any(e.kind == "error" for e in events)


def test_mineru_v1_degrade(tmp_work, monkeypatch):
    """v2.1 监督：parser=mineru 云端解析失败 → **中止**（不静默降级），审计记录 error 事件"""
    pdf = tmp_work / "test.pdf"
    _make_pdf(pdf)

    class FakeClient:
        def extract_v1(self, *a, **k):
            raise PaperError("PAPER-0010", stage="S1", detail={"why": "network"})

    import paperparse.core.mineru_client as mc_mod
    monkeypatch.setattr(mc_mod, "MineruClient", lambda cfg: FakeClient())

    result = _run(pdf, tmp_work, parser="mineru")
    # v2.1：云端解析失败不再降级成功——中止并反馈（由上层 process_pdf 返回错误让用户选择）
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "PAPER-0010"
    events = Auditor(result.run_id, result.outputs.audit_dir).load_from_disk()
    assert any(e.kind == "error" for e in events)       # 监督：失败事件被记录


def test_resume_skips_stages(tmp_work):
    pdf = tmp_work / "test.pdf"
    _make_pdf(pdf)
    r1 = _run(pdf, tmp_work, run_id="run-resume-test")
    assert r1.status == "success"
    # resume 运行：所有阶段检查点跳过
    r2 = _run(pdf, tmp_work, run_id="run-resume-test-2", resume=True)
    # 检查点存在 → 新 run 也能复用（intermediate 在 DOI 目录下，run_id 不同则目录不同）
    # 这里验证 resume 不报错且成功
    assert r2.status == "success"
