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


def _probe(chars: int, cjk_ratio: float = 0.0, ok: bool = True,
           ctrl_chars: int = 0, pua_chars: int = 0, fffd_chars: int = 0) -> dict:
    return {"pages": 1, "chars": chars, "cjk_ratio": cjk_ratio, "ok": ok,
            "ctrl_chars": ctrl_chars, "pua_chars": pua_chars, "fffd_chars": fffd_chars,
            "error": ""}


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


# ---------------------------------------------------------------- 扫描件 / 默认模式判定
def test_is_ocr_auto_defaults_to_ocr():
    """★2026-09-17 L5 决策实验后：auto 对普通论文**默认走 OCR**（文本层模式实测系统性劣化：
    LaTeX 空格碎片 5–285→0、连字丢字母 `efect`→`effect`、数字被空格拆散），
    只有**大文档**（> OCR_MAX_PAGES 页）且无 cmap 异常时才回落文本层模式。"""
    from paperparse.core.parse_params import OCR_MAX_PAGES
    assert resolve_is_ocr("auto", _probe(12))[0] is True          # 扫描件
    assert resolve_is_ocr("auto", _probe(4200))[0] is True        # 普通 born-digital → 也走 OCR
    assert "OCR" in resolve_is_ocr("auto", _probe(4200))[1]
    big = dict(_probe(4200), pages=OCR_MAX_PAGES + 1)
    assert resolve_is_ocr("auto", big)[0] is False                # 大文档（书/学位论文）→ 文本层
    assert "大文档" in resolve_is_ocr("auto", big)[1]


def test_is_ocr_explicit_overrides_auto():
    assert resolve_is_ocr("on", _probe(4200))[0] is True         # 强制开（图片型公式页等）
    assert resolve_is_ocr("off", _probe(3))[0] is False          # 强制关


# ------------------------------------------------- ★cmap 错映射（2026-09-17 NC 实测）
def test_is_ocr_auto_detects_cmap_breakage():
    """文本层含 C0 控制字符 = 字体 cmap 错映射 ⇒ **必须**走 OCR 模式（且原因要写明评分构成）。

    NC 实测（10.1038_ncomms8258）：ctrl_chars=99 ⇒ OCR 模式把 `o2 nm`(错) 读成 `<2 nm`(对)、
    `\\Nu_{2}` 读成 `N_{2}`、并顺带消除 drop cap 与控制字符。
    """
    ok, why = resolve_is_ocr("auto", _probe(1711, ctrl_chars=99))
    assert ok is True and "cmap" in why and "评分 297" in why
    # 显式开关仍然优先（用户可强制关掉自动行为）
    assert resolve_is_ocr("off", _probe(1711, ctrl_chars=99))[0] is False
    assert resolve_is_ocr("on", _probe(4197, ctrl_chars=0))[0] is True
    # 探测失败不猜
    assert resolve_is_ocr("auto", {"ok": False})[0] is False


def test_probe_pdf_counts_ctrl_chars(tmp_path):
    """`probe_pdf` 必须给出 `ctrl_chars`（cmap 判据的唯一来源）。"""
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "normal text")
    page.insert_text((72, 90), "size \x03 2.5 nm")     # 模拟坏 cmap 的控制字符
    p = tmp_path / "t.pdf"
    doc.save(str(p))
    doc.close()
    pr = probe_pdf(str(p))
    assert pr["ok"] is True and pr["ctrl_chars"] >= 1
    assert resolve_mineru_params(_Cfg(), str(p))["is_ocr"] is True
    assert resolve_mineru_params(_Cfg(), str(p))["is_ocr_reason"]      # 判定原因必须可解释
    assert resolve_is_ocr("true", _probe(3))[0] is True          # 兼容 1/true/yes/on


def test_is_ocr_probe_failure_keeps_official_default():
    """探测失败 → False（官方默认），但**原因必须写明**（不许静默）。"""
    ok, why = resolve_is_ocr("auto", {"ok": False, "error": "encrypted"})
    assert ok is False and "探测失败" in why and "encrypted" in why
    assert resolve_is_ocr("auto", None)[0] is False


def test_cmap_score_weights_and_pua(tmp_path):
    """★2026-09-17 L1b：可疑度评分 `3*ctrl + 2*pua + fffd`；PUA 也能触发。"""
    from paperparse.core.parse_params import cmap_suspect_score
    assert cmap_suspect_score(_probe(4000, ctrl_chars=1)) == 3
    assert cmap_suspect_score(_probe(4000, pua_chars=1)) == 2
    assert cmap_suspect_score(_probe(4000, fffd_chars=1)) == 1
    assert cmap_suspect_score(_probe(4000)) == 0
    assert cmap_suspect_score({"ok": False}) == 0
    # PUA 单独出现也能触发（用户担心的"漏判另一类 cmap 异常"）
    assert resolve_is_ocr("auto", _probe(4000, pua_chars=3))[0] is True
    assert resolve_is_ocr("auto", _probe(4000, fffd_chars=1))[0] is True


def test_probe_pdf_scans_all_pages(tmp_path):
    """★2026-09-17 L1a：**页窗截断漏判**——scirobotics 前 8 页 0 个、全篇 4 个。

    造一个 10 页 PDF，坏字形只出现在第 9/10 页：旧口径（只扫前 8 页）会判 0 → 漏判；
    新口径必须统计到。
    """
    import pymupdf
    doc = pymupdf.open()
    for i in range(10):
        pg = doc.new_page()
        pg.insert_text((72, 72), "page %d normal text " % (i + 1) * 6)
        if i >= 8:                                  # 第 9/10 页才有坏 cmap 控制符
            pg.insert_text((72, 100), "size \x03 2.5 nm")
    p = tmp_path / "late_breakage.pdf"
    doc.save(str(p))
    doc.close()
    pr = probe_pdf(str(p))
    assert pr["ok"] is True and pr["pages"] == 10 and pr["pages_probed"] == 10
    assert pr["ctrl_chars"] >= 2, "第 9/10 页的控制符必须被统计到（页窗截断回归）"
    assert resolve_mineru_params(_Cfg(), str(p))["is_ocr"] is True


def test_text_layer_signals_counts_pua_and_fffd():
    """`text_layer_signals` 是 cmap 判据的唯一来源（pymupdf 无法把 PUA/FFFD 写回 PDF，故直接测本函数）。"""
    from paperparse.core.parse_params import text_layer_signals
    sig = text_layer_signals("size \x03 2.5 nm, pua \ue123, broken \ufffd, tab\tok")
    assert sig == {"ctrl": 1, "pua": 1, "fffd": 1}        # \t 不算
    assert text_layer_signals("plain text") == {"ctrl": 0, "pua": 0, "fffd": 0}


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
    # ★2026-09-17：auto 默认走 OCR（文本层模式实测系统性劣化）；显式 off 才回落文本层模式
    assert body["is_ocr"] is True
    assert client.last_params["is_ocr_reason"]
    assert body["enable_table"] is True             # 显式下发（旧实现缺失）
    assert client.last_params["probe"]["cjk_ratio"] == 0.6

    # 显式 MINERU_IS_OCR=off ⇒ 文本层模式（逃生门必须有效）
    captured.clear()
    cfg_off = AppConfig(mineru_api_key="sk-test", mineru_language="auto",
                        mineru_is_ocr="off")
    with pytest.raises(PaperError):
        mc.MineruClient(cfg_off).extract_v4_batch(str(pdf))
    assert captured["payload"]["is_ocr"] is False
