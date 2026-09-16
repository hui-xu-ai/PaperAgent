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

# ★2026-09-17（NC 实测）：**cmap 错映射**信号——PDF 自定义字体把数学字形（×/±/≤/上标负号…）
# 映射到 C0 控制字符。实测 NC：`×`→U+0003、上标负号→U+0002、另有 U+0004；
# 后果是"**两条通道 + PDF 文本层三边同错**"（MinerU 文本层模式读错、PaddleOCR 也读错、
# 文本层无从裁决），例如 `more micropores (o2 nm)`（真值 `<2 nm`）、`\Nu_{2}`（真值 N₂）。
# 这类篇**改走 MinerU OCR 模式**（从图像读字形，绕过坏 cmap）即可全解：
# 实测 NC OCR 模式 ⇒ `o2 nm` 0 处 / `<2 nm` 5 处、控制符 0、`\Nu` 0、drop cap 也自愈。
# 判据只取 C0/C1 控制字符（不含 \t\n\r），且要求出现次数 ≥ 阈值，避免零星噪声误触发。
CMAP_CTRL_MIN_HITS = 1
# 文本层探测页数上限（首页 + 若干页足以暴露 cmap 问题，且探测要快）
CMAP_PROBE_PAGES = 8
_CTRL_RE = None

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


def _count_ctrl_chars(text: str) -> int:
    """[局部] 文本层里的 C0/C1 控制字符数（不含 \\t\\n\\r）——cmap 错映射的判据。"""
    return sum(1 for ch in text
               if (ord(ch) < 0x20 and ch not in "\t\n\r") or 0x7f <= ord(ch) <= 0x9f)


def probe_pdf(pdf_path) -> dict:
    """探测 PDF 特征（供 auto 判定）：`{pages, chars, cjk_ratio, ctrl_chars, ok, error}`。

    首页文本层判语种/扫描件；**前 CMAP_PROBE_PAGES 页**统计控制字符数（cmap 错映射信号，
    见 CMAP_CTRL_MIN_HITS 注释）。只读文本层，失败不抛错（返回 ok=False），调用方按"无法判定"处理。
    """
    out = {"pages": 0, "chars": 0, "cjk_ratio": 0.0, "ctrl_chars": 0, "ok": False, "error": ""}
    try:
        import pymupdf
        with pymupdf.open(str(pdf_path)) as doc:
            out["pages"] = doc.page_count
            text = doc[0].get_text() if doc.page_count else ""
            ctrl = 0
            for i in range(min(doc.page_count, CMAP_PROBE_PAGES)):
                ctrl += _count_ctrl_chars(doc[i].get_text())
            out["ctrl_chars"] = ctrl
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


def resolve_is_ocr(cfg_is_ocr: str | None, probe: dict | None) -> tuple:
    """`MINERU_IS_OCR` → `(is_ocr, reason)`。

    on/off：显式开关优先（原样遵从）；
    auto：① **cmap 错映射**（文本层含 C0/C1 控制字符 ≥ CMAP_CTRL_MIN_HITS）→ **True**
             （★2026-09-17 NC 实测：这类篇两通道+文本层三边同错，只有 OCR 模式能读对
              `<2 nm` / `N₂` / `×`，并顺带消除 drop cap 与乱码）；
          ② 首页可抽字符数 < SCAN_CHAR_THRESHOLD（扫描件）→ True；
          ③ 其余 → False（保持官方默认，正常 PDF 不多花配额/时间）。
    探测失败 → False（不猜）。
    """
    state = _as_tristate(cfg_is_ocr)
    if state == "on":
        return True, "显式 MINERU_IS_OCR=on"
    if state == "off":
        return False, "显式 MINERU_IS_OCR=off"
    if not probe or not probe.get("ok"):
        return False, "探测失败，保持默认"
    ctrl = int(probe.get("ctrl_chars") or 0)
    if ctrl >= CMAP_CTRL_MIN_HITS:
        return True, f"文本层含 {ctrl} 个控制字符（字体 cmap 错映射）→ 需 OCR 模式"
    if int(probe.get("chars") or 0) < SCAN_CHAR_THRESHOLD:
        return True, "首页文本层稀薄（疑似扫描件）"
    return False, "born-digital 且文本层无 cmap 异常"


def resolve_mineru_params(cfg, pdf_path) -> dict:
    """组装 MinerU v4 解析参数（**所有关键项显式下发**，不依赖服务端默认）。

    返回 `{model_version, language, is_ocr, is_ocr_reason, enable_table, enable_formula, probe}`，
    其中 `probe` 为原始探测结果（排障/UI 展示用）。
    """
    probe = probe_pdf(pdf_path)
    is_ocr, reason = resolve_is_ocr(getattr(cfg, "mineru_is_ocr", DEFAULT_IS_OCR), probe)
    return {
        "model_version": (getattr(cfg, "mineru_model_version", "") or "vlm"),
        "language": resolve_language(getattr(cfg, "mineru_language", DEFAULT_LANGUAGE), probe),
        "is_ocr": is_ocr,
        "is_ocr_reason": reason,
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
