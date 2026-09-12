# -*- coding: utf-8 -*-
"""批2：解析硬门禁（无 MinerU Key 拒绝解析）回归钉。

用户决策（2026-09-12）："解析 PDF 至少需要配置 MinerU 的密钥才能确保质量可靠" ⇒
生产链只保留 mineru-v4，无 Key 时**直接拒绝**（不降级免费 v1 / 本地 pymupdf）。
本文件锁三处判据：引擎唯一解析入口、通道收敛、上传前置预检。
"""
from __future__ import annotations

import pytest


def _clear_live_key(monkeypatch):
    """把实时 Key 置空（os.environ 有键但为空 ⇒ mineru_ready 不回落启动快照）。"""
    monkeypatch.setenv("MINERU_API_KEY", "")


# ---------------------------------------------------------------- 引擎层（唯一解析入口）
def test_parse_pdf_blocked_without_key(tmp_path, settings, monkeypatch):
    from app.services.engine_service import EngineError, EngineService
    _clear_live_key(monkeypatch)
    eng = EngineService(settings)
    pdf = tmp_path / "t.pdf"
    pdf.write_bytes(b"%PDF-1.7")
    with pytest.raises(EngineError) as ei:
        eng.parse_pdf(str(pdf))
    assert ei.value.code == "PAPER-MINERU-REQUIRED"
    assert "MinerU" in str(ei.value) and "设置中心" in str(ei.value)


def test_parse_pdf_passes_gate_with_live_key(tmp_path, settings, monkeypatch):
    """有 Key 时门禁放行（用假 _api 避免真实网络）。"""
    from app.services.engine_service import EngineService
    monkeypatch.setenv("MINERU_API_KEY", "sk-live")
    eng = EngineService(settings)
    calls: list[str] = []

    class _Api:
        def process_pdf_v2(self, pdf_path, **kw):
            calls.append("v2")
            return {"status": "success", "document_json": str(pdf_path),
                    "parse_source": "p14"}

    eng._api = _Api()
    fake_md = tmp_path / "full.md"
    fake_md.write_text("# x\n\nbody\n", encoding="utf-8")
    monkeypatch.setattr(eng, "_ensure_mineru_md", lambda p, w: fake_md)
    monkeypatch.setattr(eng, "_post_parse_clean", lambda p: None)
    pdf = tmp_path / "t.pdf"
    pdf.write_bytes(b"%PDF-1.7")
    r = eng.parse_pdf(str(pdf))
    assert calls == ["v2"] and r["parse_source"] == "p14"


def test_parse_chain_only_mineru_v4(settings):
    """通道收敛：生产链不再有 v1 免费 / 本地 pymupdf 兜底。"""
    from app.services.engine_service import EngineService
    eng = EngineService(settings)
    assert eng._parse_chain() == ["mineru-v4"]
    assert eng._parse_chain("pymupdf") == ["pymupdf"]     # 显式指定（内部/离线调用）仍尊重


# ---------------------------------------------------------------- 上传预检（API 层）
def test_upload_precheck_rejects_without_key(settings, monkeypatch):
    from fastapi import HTTPException

    from app.api import papers
    from app.services import container
    monkeypatch.setattr(container, "get_settings", lambda: settings)
    monkeypatch.setattr("app.config.mineru_ready", lambda *_a, **_k: False)
    with pytest.raises(HTTPException) as ei:
        papers._require_mineru()
    assert ei.value.status_code == 400
    assert ei.value.detail["mineru_required"] is True
    assert "MinerU" in ei.value.detail["message"]


def test_upload_precheck_allows_with_key(settings, monkeypatch):
    from app.api import papers
    from app.services import container
    monkeypatch.setattr(container, "get_settings", lambda: settings)
    papers._require_mineru()          # 不抛异常即通过（settings fixture 带测试 Key）
