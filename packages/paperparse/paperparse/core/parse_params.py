# -*- coding: utf-8 -*-
"""解析质量参数：智能默认 + 官方 payload 组装（2026-09-12 批2）。

背景（用户 2026-09-12 拍板"解析质量优先"）：
- MinerU v4 此前**硬编码** `language="en"`、`enable_formula=True`，且 `enable_table`/`is_ocr`
  根本不下发 ⇒ 中文文献走英文 OCR；扫描件（无文本层）官方默认 `is_ocr=false` ⇒ 近乎无输出。
- PaddleOCR-VL 此前下发空 optionalPayload ⇒ 官方默认 `restructurePages=false`
  ⇒ 论文常见的**跨页表格被拆断**、标题层级不重整。

本模块是这些参数的**单一判据**（`language`/`is_ocr` 的 auto 判定阈值也只在这里一份），
供 `mineru_client`（payload）与后端设置中心（展示"当前实际生效值"）共用。
"""
from __future__ import annotations

DEFAULT_LANGUAGE = "auto"
DEFAULT_IS_OCR = "auto"

# ---- 智能默认阈值（auto 判定）----
# 首页可抽取的非空白字符数低于此值 → 判为扫描件（无文本层）→ is_ocr 开。
# 100 的依据：正常论文首页正文 >1500 字符；纯扫描件首页通常 0-30（页眉/页码偶有文字层）。
SCAN_CHAR_THRESHOLD = 100
# 首页 CJK 字符占比高于此值 → 判为中文文献（MinerU language="ch"）。
CJK_RATIO_THRESHOLD = 0.15

# PaddleOCR-VL 官方服务化参数**论文解析推荐值**（官方默认 restructurePages=false）。
DEFAULT_PADDLEOCR_OPTIONS: dict = {
    "restructurePages": True,   # 跨页表格合并 + 标题层级重整（论文常见跨页表）
    "mergeTables": True,        # 同一表格被拆成多块时合并（restructurePages 的子开关）
    "relevelTitles": True,      # 按版面重排标题级别
}

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def _as_tristate(value: str | None, default: str = DEFAULT_IS_OCR) -> str:
    """归一化三态开关：auto / on / off（未知值回落 auto，不抛错）。"""
    v = (value or "").strip().lower()
    if v in _TRUE:
        return "on"
    if v in _FALSE:
        return "off"
    return default


def probe_pdf(pdf_path) -> dict:
    """探测 PDF 首页特征（供 auto 判定）：`{pages, chars, cjk_ratio, ok, error}`。

    只读第一页文本层，失败不抛错（返回 ok=False），调用方按"无法判定"处理。
    """
    out = {"pages": 0, "chars": 0, "cjk_ratio": 0.0, "ok": False, "error": ""}
    try:
        import pymupdf
        with pymupdf.open(str(pdf_path)) as doc:
            out["pages"] = doc.page_count
            text = doc[0].get_text() if doc.page_count else ""
    except Exception as e:  # noqa: BLE001 - 探测失败不影响解析（按默认参数走）
        out["error"] = str(e)[:200]
        return out
    stripped = "".join(text.split())
    out["chars"] = len(stripped)
    if stripped:
        cjk = sum(1 for ch in stripped if "\u4e00" <= ch <= "\u9fff")
        out["cjk_ratio"] = round(cjk / len(stripped), 4)
    out["ok"] = True
    return out


def resolve_language(cfg_language: str | None, probe: dict | None) -> str:
    """`MINERU_LANGUAGE` → 实际下发给 MinerU 的语种。

    auto（或未知值）：按首页 CJK 占比判定 ch/en；探测失败时回退 en
    （英文文献占绝大多数，且 en 对纯符号/公式页最稳）。
    """
    v = (cfg_language or DEFAULT_LANGUAGE).strip().lower()
    if v in ("en", "ch"):
        return v
    if not probe or not probe.get("ok"):
        return "en"
    return "ch" if float(probe.get("cjk_ratio") or 0) >= CJK_RATIO_THRESHOLD else "en"


def resolve_is_ocr(cfg_is_ocr: str | None, probe: dict | None) -> bool:
    """`MINERU_IS_OCR` → 实际下发的 `is_ocr`。

    auto：首页可抽字符数 < SCAN_CHAR_THRESHOLD 判为扫描件 → True；探测失败 → False
    （保持官方默认，避免把正常 PDF 当扫描件多花钱）。
    """
    state = _as_tristate(cfg_is_ocr)
    if state == "on":
        return True
    if state == "off":
        return False
    if not probe or not probe.get("ok"):
        return False
    return int(probe.get("chars") or 0) < SCAN_CHAR_THRESHOLD


def resolve_mineru_params(cfg, pdf_path) -> dict:
    """组装 MinerU v4 解析参数（**所有关键项显式下发**，不依赖服务端默认）。

    返回 `{model_version, language, is_ocr, enable_table, enable_formula, probe}`，
    其中 `probe` 为原始探测结果（排障/UI 展示用）。
    """
    probe = probe_pdf(pdf_path)
    return {
        "model_version": (getattr(cfg, "mineru_model_version", "") or "vlm"),
        "language": resolve_language(getattr(cfg, "mineru_language", DEFAULT_LANGUAGE), probe),
        "is_ocr": resolve_is_ocr(getattr(cfg, "mineru_is_ocr", DEFAULT_IS_OCR), probe),
        "enable_table": bool(getattr(cfg, "mineru_enable_table", True)),
        "enable_formula": bool(getattr(cfg, "mineru_enable_formula", True)),
        "probe": probe,
    }


def mineru_payload_params(params: dict) -> dict:
    """从 `resolve_mineru_params()` 结果取**可直接放进 MinerU 请求体**的字段。"""
    return {
        "model_version": params.get("model_version", "vlm"),
        "language": params.get("language", "en"),
        "is_ocr": bool(params.get("is_ocr", False)),
        "enable_table": bool(params.get("enable_table", True)),
        "enable_formula": bool(params.get("enable_formula", True)),
    }


def parse_paddleocr_options(raw: str | None) -> dict:
    """`PADDLEOCR_OPTIONS`（JSON 字符串）→ optionalPayload 字典。

    空/非法 JSON → `DEFAULT_PADDLEOCR_OPTIONS`；只保留已知键，值统一 bool。
    """
    import json as _json
    if not raw:
        return dict(DEFAULT_PADDLEOCR_OPTIONS)
    try:
        data = _json.loads(raw)
    except (ValueError, TypeError):
        return dict(DEFAULT_PADDLEOCR_OPTIONS)
    if not isinstance(data, dict):
        return dict(DEFAULT_PADDLEOCR_OPTIONS)
    out = dict(DEFAULT_PADDLEOCR_OPTIONS)
    for k in DEFAULT_PADDLEOCR_OPTIONS:
        if k in data:
            out[k] = bool(data[k])
    return out


def paddleocr_options_from_cfg(cfg) -> dict:
    return parse_paddleocr_options(getattr(cfg, "paddleocr_options", ""))
