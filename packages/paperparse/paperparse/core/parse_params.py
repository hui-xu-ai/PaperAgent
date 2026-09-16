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
# 判据只取 C0/C1 控制字符（不含 \t\n\r）、PUA 私有区、U+FFFD；软连字符（U+00AD）是正常断词，
# **不计分**（snb 有 38 个，属正常排版）。
# ★2026-09-17 全库普查（42 PDF / 15 篇）：约 1/3 文献文本层含控制字符；且实测到**页窗截断漏判**
# （scirobotics：前 8 页 0 个、全篇 4 个）⇒ 探测改为**全页扫描**（上限 CMAP_PROBE_MAX_PAGES）。
CMAP_CTRL_MIN_HITS = 1
CMAP_PROBE_MAX_PAGES = 200          # 安全上限（超长 PDF 不必全扫完）
# ★2026-09-17 L5 决策实验（6 篇交叉比对）：auto 默认改走 OCR（文本层模式有系统性劣化）。
# 但 OCR 逐页读图，**大文档**（书/学位论文）会更慢 ⇒ 超过此页数且无 cmap 异常时仍用文本层模式。
OCR_MAX_PAGES = 60
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


def text_layer_signals(text: str) -> dict:
    """[全局] 一段文本层内容的"可信度信号"计数（cmap 判据的**唯一来源**，便于单测）。

    · `ctrl`：C0/C1 控制字符（不含 \\t\\n\\r）——实测 `×`→U+0003、上标负号→U+0002；
    · `pua`：私有区 U+E000–U+F8FF（字体自造编码）；
    · `fffd`：U+FFFD 替换符（缺字形）。
    """
    t = text or ""
    return {
        "ctrl": _count_ctrl_chars(t),
        "pua": sum(1 for ch in t if 0xe000 <= ord(ch) <= 0xf8ff),
        "fffd": t.count("\ufffd"),
    }


def probe_pdf(pdf_path) -> dict:
    """探测 PDF 特征（供 auto 判定）：
    `{pages, pages_probed, chars, cjk_ratio, ctrl_chars, pua_chars, fffd_chars, ok, error}`。

    首页文本层判语种/扫描件；**全篇**（上限 CMAP_PROBE_MAX_PAGES）统计
    · `ctrl_chars`：C0/C1 控制字符（字体 cmap 把数学字形映射成控制符，实测 NC `×`→U+0003）；
    · `pua_chars`：私有区 U+E000–U+F8FF（字体自造编码，另一类 cmap 异常）；
    · `fffd_chars`：U+FFFD 替换符（缺字形/编码失败）。
    ★2026-09-17：由"只扫前 8 页"改为**全篇**——实测 scirobotics 前 8 页 0 个、全篇 4 个（漏判）。
    只读文本层，失败不抛错（`ok=False` + `error`），调用方按"无法判定"处理。
    """
    out = {"pages": 0, "pages_probed": 0, "chars": 0, "cjk_ratio": 0.0,
           "ctrl_chars": 0, "pua_chars": 0, "fffd_chars": 0, "ok": False, "error": ""}
    try:
        import pymupdf
        with pymupdf.open(str(pdf_path)) as doc:
            out["pages"] = doc.page_count
            text = doc[0].get_text() if doc.page_count else ""
            n = min(doc.page_count, CMAP_PROBE_MAX_PAGES)
            out["pages_probed"] = n
            for i in range(n):
                sig = text_layer_signals(doc[i].get_text())
                out["ctrl_chars"] += sig["ctrl"]
                out["pua_chars"] += sig["pua"]
                out["fffd_chars"] += sig["fffd"]
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


def cmap_suspect_score(probe: dict | None) -> int:
    """[全局] 文本层"cmap 可疑度"评分（★2026-09-17）。

    `3*ctrl_chars + 2*pua_chars + 1*fffd_chars`（软连字符不计：属正常断词）。
    权重依据：控制字符是**确定**的 cmap 错映射（实测 NC `×`→U+0003、上标负号→U+0002）；
    PUA 是字体自造编码（较强）；U+FFFD 缺字形（较弱，也可能来自别的原因）。
    """
    if not probe or not probe.get("ok"):
        return 0
    return (3 * int(probe.get("ctrl_chars") or 0)
            + 2 * int(probe.get("pua_chars") or 0)
            + 1 * int(probe.get("fffd_chars") or 0))


def resolve_is_ocr(cfg_is_ocr: str | None, probe: dict | None) -> tuple:
    """`MINERU_IS_OCR` → `(is_ocr, reason)`。

    on/off：显式开关优先（原样遵从）；
    auto（★2026-09-17 决策实验 L5 后调整）：
      ① **cmap 错映射**（`cmap_suspect_score >= 1`）→ **OCR**；
      ② 扫描件（首页文本层 < SCAN_CHAR_THRESHOLD）→ **OCR**；
      ③ **大文档**（页数 > OCR_MAX_PAGES，默认书/学位论文）→ **文本层模式**
         （OCR 逐页读图，数百页会明显更慢；若确需可显式 `MINERU_IS_OCR=on`）；
      ④ 其余 → **OCR**（★实测收益，见下）。
    ★实测依据（6 篇不同出版社论文，同一 PDF 两模式交叉比对，`analyze_mineru_ab.py`）：
      · LaTeX **空格碎片化** `\\mathrm { C o`：文本层模式 5–285 处 → OCR **全部 0**；
      · **连字丢字母**（`efect/diferent/takeof` → `effect/different/takeoff`）：OCR 全部恢复；
      · **数字被空格拆散**（`1 5 0`）：OCR 不再出现；
      · NC：控制符 2→0、drop-cap `<sup>` 碎片 8→0、`o2 nm` 3→0（`<2 nm` 5 处全对）、`\\Nu` 1→0。
      ⇒ 文本层模式在这些维度上**系统性劣化**，故 auto 默认改走 OCR（用户已确认配额免费）。
    探测失败：仍返回 False（不猜），但 reason 写明"探测失败"，由调用方落盘/提示（不静默）。
    """
    state = _as_tristate(cfg_is_ocr)
    if state == "on":
        return True, "显式 MINERU_IS_OCR=on"
    if state == "off":
        return False, "显式 MINERU_IS_OCR=off"
    if not probe or not probe.get("ok"):
        return False, "文本层探测失败（未能判定可信度，保持默认文本层模式）: %s" % (
            (probe or {}).get("error") or "未知")
    score = cmap_suspect_score(probe)
    if score >= 1:
        return True, ("文本层 cmap 可疑（评分 %d = ctrl%d*3 + pua%d*2 + fffd%d）→ 需 OCR 模式"
                      % (score, probe.get("ctrl_chars") or 0, probe.get("pua_chars") or 0,
                         probe.get("fffd_chars") or 0))
    if int(probe.get("chars") or 0) < SCAN_CHAR_THRESHOLD:
        return True, "首页文本层稀薄（疑似扫描件）"
    pages = int(probe.get("pages") or 0)
    if pages > OCR_MAX_PAGES:
        return False, ("大文档（%d 页 > %d）且文本层无 cmap 异常 → 文本层模式（OCR 逐页读图太慢；"
                       "如需可显式 MINERU_IS_OCR=on）" % (pages, OCR_MAX_PAGES))
    return True, ("born-digital 且无 cmap 异常，但 auto 默认走 OCR：实测文本层模式有系统性劣化"
                  "（LaTeX 空格碎片 5–285→0、连字丢字母 ef ect→effect、数字被空格拆散）")


def resolve_mineru_params(cfg, pdf_path) -> dict:
    """组装 MinerU v4 解析参数（**所有关键项显式下发**，不依赖服务端默认）。

    返回 `{model_version, language, is_ocr, is_ocr_reason, cmap_score, enable_table,
    enable_formula, probe}`，其中 `probe` 为原始探测结果（排障/UI 展示用）。
    """
    probe = probe_pdf(pdf_path)
    is_ocr, reason = resolve_is_ocr(getattr(cfg, "mineru_is_ocr", DEFAULT_IS_OCR), probe)
    return {
        "model_version": (getattr(cfg, "mineru_model_version", "") or "vlm"),
        "language": resolve_language(getattr(cfg, "mineru_language", DEFAULT_LANGUAGE), probe),
        "is_ocr": is_ocr,
        "is_ocr_reason": reason,
        "cmap_score": cmap_suspect_score(probe),
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
