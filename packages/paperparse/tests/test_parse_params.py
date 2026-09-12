# -*- coding: utf-8 -*-
"""批2：解析参数（language/is_ocr/enable_table/PaddleOCR options）与 MinerU payload 下发。

依据：`docs/` 与 `work/scratch/docs/`（官方参数表）；判据集中在
`paperparse.core.parse_params`（本文件即其回归钉）。
"""
from __future__ import annotations

import pytest

from paperparse.core.parse_params import (
    DEFAULT_PADDLEOCR_OPTIONS, mineru_payload_params, parse_paddleocr_options,
    probe_pdf, resolve_is_ocr, resolve_language, resolve_mineru_params,
)
from paperparse.middleware.errors import PaperError


class _Cfg:
    """最小 cfg 替身（只带本模块读取的属性）。"""

    def __init__(self, **kw):
        self.mineru_model_version = kw.get("model_version", "vlm")
        self.mineru_language = kw.get("language", "auto")
        self.mineru_is_ocr = kw.get("is_ocr", "auto")
        self.mineru_enable_table = kw.get("enable_table", True)
        self.mineru_enable_formula = kw.get("enable_formula", True)
        self.paddleocr_options = kw.get("paddleocr_options", "")


def _probe(chars: int, cjk_ratio: float = 0.0, ok: bool = True) -> dict:
    return {"pages": 1, "chars": chars, "cjk_ratio": cjk_ratio, "ok": ok, "error": ""}


# ---------------------------------------------------------------- 语种判定
def test_language_explicit_wins():
    assert resolve_language("ch", _probe(5000, 0.0)) == "ch"     # 显式中文不被探测覆盖
    assert resolve_language("en", _probe(5000, 0.9)) == "en"


def test_language_auto_detects_cjk():
    assert resolve_language("auto", _probe(5000, 0.42)) == "ch"  # 中文文献
    assert resolve_language("auto", _probe(5000, 0.02)) == "en"  # 英文文献


def test_language_auto_probe_failure_falls_back_en():
    assert resolve_language("auto", None) == "en"
    assert resolve_language("auto", {"ok": False}) == "en"
    assert resolve_language("weird-value", _probe(5000, 0.0)) == "en"   # 未知值按 auto


# ---------------------------------------------------------------- 扫描件判定
def test_is_ocr_auto_detects_scan():
    assert resolve_is_ocr("auto", _probe(12)) is True            # 首页几乎无文本层 → 扫描件
    assert resolve_is_ocr("auto", _probe(4200)) is False         # 正常文本层


def test_is_ocr_explicit_overrides_auto():
    assert resolve_is_ocr("on", _probe(4200)) is True            # 强制开（图片型公式页等）
    assert resolve_is_ocr("off", _probe(3)) is False             # 强制关
    assert resolve_is_ocr("true", _probe(3)) is True             # 兼容 1/true/yes/on


def test_is_ocr_probe_failure_keeps_official_default():
    """探测失败 → False（官方默认），避免把正常 PDF 误判为扫描件多花钱。"""
    assert resolve_is_ocr("auto", {"ok": False}) is False
    assert resolve_is_ocr("auto", None) is False


# ---------------------------------------------------------------- payload 组装
def test_mineru_payload_has_all_quality_switches(tmp_path):
    """回归钉：payload **必须显式下发** language/is_ocr/enable_table（此前 language 硬编码 en、
    后两项缺失 ⇒ 中文走英文 OCR、扫描件无输出）。"""
    pdf = tmp_path / "scan.pdf"
    _make_pdf(pdf, text="")                       # 无文本层 = 扫描件
    params = resolve_mineru_params(_Cfg(language="auto", is_ocr="auto"), str(pdf))
    payload = mineru_payload_params(params)
    assert set(payload) == {"model_version", "language", "is_ocr",
                            "enable_table", "enable_formula"}
    assert payload["is_ocr"] is True              # 自动识别为扫描件
    assert payload["model_version"] == "vlm"
    assert payload["enable_table"] is True


def test_probe_pdf_reads_text_layer(tmp_path):
    pdf = tmp_path / "text.pdf"
    _make_pdf(pdf, text="Ionic polymer sensors exhibit high sensitivity.")
    p = probe_pdf(str(pdf))
    assert p["ok"] is True and p["chars"] > 20 and p["pages"] == 1


def test_probe_pdf_missing_file_is_not_fatal():
    p = probe_pdf("no-such-file.pdf")
    assert p["ok"] is False and p["error"]


def _make_pdf(path, text: str = "") -> None:
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    if text:
        page.insert_text((72, 100), text, fontsize=11)
    doc.save(str(path))
    doc.close()


# ---------------------------------------------------------------- PaddleOCR options
def test_paddleocr_options_default_all_on():
    """官方默认 restructurePages=false；论文解析推荐全开（跨页表格 + 标题分级）。"""
    assert parse_paddleocr_options("") == DEFAULT_PADDLEOCR_OPTIONS
    assert DEFAULT_PADDLEOCR_OPTIONS["restructurePages"] is True


def test_paddleocr_options_json_roundtrip_and_guard():
    import json
    raw = json.dumps({"restructurePages": False, "mergeTables": True,
                      "relevelTitles": True, "unknownKey": True})
    got = parse_paddleocr_options(raw)
    assert got["restructurePages"] is False        # 用户显式关 → 尊重
    assert "unknownKey" not in got                 # 未知键丢弃（不传给服务端）
    assert parse_paddleocr_options("{bad json") == DEFAULT_PADDLEOCR_OPTIONS


def test_mineru_client_sends_resolved_params(tmp_path, monkeypatch):
    """端到端（HTTP 打桩）：`extract_v4_batch` 的申请链接请求体带上解析参数。

    探测值用打桩注入（真实文本层探测已由 `test_probe_pdf_reads_text_layer` 覆盖；
    pymupdf 内置字体无法可靠写入中文层，故此处不造中文 PDF）。
    """
    import paperparse.core.mineru_client as mc
    from paperparse.config import AppConfig

    monkeypatch.setattr("paperparse.core.parse_params.probe_pdf",
                        lambda p: {"pages": 1, "chars": 800, "cjk_ratio": 0.6,
                                   "ok": True, "error": ""})
    pdf = tmp_path / "cn.pdf"
    _make_pdf(pdf, text="placeholder")
    captured: dict = {}

    class _Resp:
        status_code = 400
        text = "bad request"
        def json(self):
            return {"code": 1, "msg": "stub"}

    def _fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        captured["url"] = url
        captured["payload"] = json
        return _Resp()

    monkeypatch.setattr(mc.requests, "post", _fake_post)
    cfg = AppConfig(mineru_api_key="sk-test", mineru_language="auto", mineru_is_ocr="auto")
    client = mc.MineruClient(cfg)
    with pytest.raises(PaperError):
        client.extract_v4_batch(str(pdf))
    body = captured["payload"]
    assert body["language"] == "ch"                 # 中文文献 → ch（旧实现硬编码 en）
    assert body["is_ocr"] is False                  # 有文本层 → 不按扫描件处理
    assert body["enable_table"] is True             # 显式下发（旧实现缺失）
    assert client.last_params["probe"]["cjk_ratio"] == 0.6
