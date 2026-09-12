#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: skill/src/paperparse/core/anomaly_detect.py
功能: 异常片段检测与 AI 辅助修复清单（用户问题 4）：
      - 检测 �（U+FFFD 替换字符）等 OCR/公式识别异常字符
      - 记录所在句子 + 上下文 + 段落位置 + 本地 PyMuPDF 对应片段（备用识别源）
      - 输出 JSON 修复清单：供 AI（轻量子代理/DeepSeek flash）判断是否存在
        LaTeX 公式识别问题，并用在线/本地结果纠正
      - 封装为可复用功能：后续各种意外情况均可"轻量调用 AI 辅助识别判断"，
        以少量 token 解决问题
对外接口: detect_anomalies / render_fix_list
版本: v1.0.0 (2026-08-18)
版本历史:
  v1.0.0 初始版本（用户问题 4：� 字符检测 + 备用识别 + AI 辅助纠正）
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from paperparse.middleware.schema import ArticleDocument

__all__ = ["detect_anomalies", "render_fix_list", "apply_known_fixes"]

ANOMALY_RE = [
    re.compile(r"\uFFFD"),                          # � 替换字符
    re.compile(r"[\\]?Nu\s*_\s*\{?\s*2\s*\}?"),     # 可疑 \Nu_2（应为 N2 氮气）等
]
CONTEXT = 60        # 上下文宽度（字符）
MAX_ITEMS = 50      # 清单上限（防爆量）

# 已确认修复库（AI 判断后的确定性替换；新异常经 AI 确认后追加）
KNOWN_FIXES: list[tuple[str, str, str]] = [
    # (异常模式, 替换为, 说明)
    (r"\$\\Nu\s*_\s*\{\s*2\s*\}\$", "$\\mathrm { N } _ { 2 }$",
     "\\Nu_2 为希腊字母 Nu 误识别，应为氮气 N2"),
    (r"\(\s*Φ\s*and\s*\uFFFD\s*", "(Φ and φ ",
     "� 应为 φ（与前文 ΔV = Φ − φ 对应）"),
    (r"poly\(\s*\uFFFD\s*-?\s*caprolactone", "poly($\\varepsilon$-caprolactone",
     "� 应为 ε（PCL = 聚 ε-己内酯）"),
    (r"\\mathsf\s*\{\s*A\s*\}\s*\\mathsf\s*\{\s*m\s*\}\s*\^\s*\{\s*\\bar\s*\{\s*2\s*\}\s*\}",
     "\\mathsf { A } \\mathsf { m } ^ { 2 }",
     "\\mathsf{A m}^{\\bar{2}} 应为 \\mathsf{A m}^{2}（单位 A·m²·kg⁻¹，OCR 误把 2 写成 \\bar{2}）"),
    # 兼容反斜杠位置被控制字符/回车污染（如 ^ { \x08ar { 2 } }）：\\bar 的反斜杠可能是 \x08 等不可见字符
    (r"\\mathsf\s*\{\s*A\s*\}\s*\\mathsf\s*\{\s*m\s*\}\s*\^\s*\{\s*[\x00-\x1f]?ar\s*\{\s*2\s*\}\s*\}",
     "\\mathsf { A } \\mathsf { m } ^ { 2 }",
     "\\mathsf{A m}^{bar{2}} 中反斜杠缺失/被控制字符污染 → 应为 \\mathsf{A m}^{2}（A·m²·kg⁻¹）"),
]


def _local_hint(text: str, local_texts: list[str]) -> str:
    """[局部] 在本地（PyMuPDF 低精度）文本中找对应片段的提示（备用识别源）：
    词集合 Dice 匹配（容忍 �/𝜖 等字符差异）"""
    st = set(re.findall(r"[a-z]{3,}", text.lower()))
    if not st:
        return ""
    best, best_d = "", 0.0
    for lt in local_texts:
        lt_t = set(re.findall(r"[a-z]{3,}", lt.lower()))
        d = 2.0 * len(st & lt_t) / (len(st) + len(lt_t)) if lt_t else 0.0
        if d > best_d:
            best_d, best = d, lt
    return best[:120] if best_d > 0.3 else ""


def detect_anomalies(doc: ArticleDocument,
                     local_texts: list[str] | None = None) -> list[dict]:
    """[全局] 检测文档中的异常片段（� 等），输出修复清单条目

    参数:
        doc: ArticleDocument（含 LaTeX 文本的 document.json）
        local_texts: 本地 PyMuPDF 行级文本列表（备用识别源，可选）
    返回:
        清单 [{"para_id", "sentence", "context", "anomaly", "local_hint"}]
    """
    items: list[dict] = []
    for p in doc.paragraphs:
        if p.is_heading:
            continue
        t = p.text_en or ""
        for pat in ANOMALY_RE:
            for m in pat.finditer(t):
                if len(items) >= MAX_ITEMS:
                    return items
                s = max(0, m.start() - CONTEXT)
                sentence = t[s: m.end() + CONTEXT].strip()
                items.append({
                    "para_id": p.para_id,
                    "section": p.section,
                    "sentence": sentence,
                    "anomaly": pat.pattern,
                    "local_hint": _local_hint(sentence, local_texts or []),
                })
                break        # 每段每模式一条（避免同一段刷屏）
    return items


def apply_known_fixes(doc: ArticleDocument) -> int:
    """[全局] 应用已确认修复库（KNOWN_FIXES）到文档段落文本：
    已知的 LaTeX/OCR 异常（经 AI 确认）确定性替换，可复现；
    新异常由 detect_anomalies 输出清单 → AI 子代理判断 → 追加规则。
    同时应用到 text_en 与 text_zh（译文/题注），保证源文修正同步到译文。

    参数:
        doc: ArticleDocument（原地修改段落 text_en / text_zh）
    返回:
        修复的段落数
    """
    fixed = 0
    for p in doc.paragraphs:
        if p.is_heading:
            continue
        changed = False
        for field in ("text_en", "text_zh"):
            t = getattr(p, field) or ""
            for pat, repl, _note in KNOWN_FIXES:
                new = re.sub(pat, lambda _m: repl, t)   # lambda：repl 中反斜杠不被 re 解析
                if new != t:
                    t = new
                    changed = True
            setattr(p, field, t)
        if changed:
            fixed += 1
    return fixed


def repair_garbled(doc: ArticleDocument, pdf_path: str | Path) -> int:
    """[全局] 乱码修复（用户需求：识别错误 → 调用本地 PDF 解析提取片段替换）：
    扫描含 U+FFFD（� 替换字符）的段落，按其 coords.pages 用本地 PyMuPDF 提取
    **整页**文本作为匹配池（Paragraph.coords 仅记录段首/尾行 bbox，不足以覆盖
    � 所在行），将 � 替换为本地片段对应位置的字符。
    若本地对应位置同样为 �（字体映射缺失）或无法定位，则保留（交给 KNOWN_FIXES / AI 清单）。

    参数:
        doc: ArticleDocument（原地修改含乱码段落的 text_en）
        pdf_path: 源 PDF 路径（本地重提取用）
    返回:
        修复的段落数
    """
    import difflib

    try:
        import pymupdf
    except ImportError:
        return 0
    fixed = 0
    try:
        pdf = pymupdf.open(str(pdf_path))
    except Exception:
        return 0
    try:
        for p in doc.paragraphs:
            if p.is_heading or "\uFFFD" not in (p.text_en or ""):
                continue
            pages = (p.coords or {}).get("pages") or []
            if not pages:
                continue
            parts: list[str] = []
            for pg in pages:
                try:
                    txt = pdf[int(pg) - 1].get_text("text") or ""
                except Exception:
                    txt = ""
                parts.append(txt)
            local = " ".join(parts)
            if not local:
                continue                      # 本地无文本 → 无法修复
            t = p.text_en
            out: list[str] = []
            n = len(t)
            i = 0
            while i < n:
                if t[i] != "\uFFFD":
                    out.append(t[i])
                    i += 1
                    continue
                # � 上下文窗口（前后 40 字符）在 local 中定位最相似片段，取对应字符
                ctx_l, ctx_r = max(0, i - 40), min(n, i + 41)
                seg = t[ctx_l:ctx_r]
                best_ratio, best_pos = 0.0, -1
                span = len(seg)
                for j in range(0, max(1, len(local) - span + 1), 2):
                    sub = local[j:j + span]
                    if not sub:
                        continue
                    r = difflib.SequenceMatcher(None, seg, sub).ratio()
                    if r > best_ratio:
                        best_ratio, best_pos = r, j
                ch = "\uFFFD"
                if best_pos >= 0 and best_ratio >= 0.6:
                    cand = local[best_pos + (i - ctx_l)]
                    if cand not in ("", "\n", "\r", "\uFFFD"):
                        ch = cand
                out.append(ch)
                i += 1
            new_text = "".join(out)
            if new_text != t:
                p.text_en = new_text
                fixed += 1
    finally:
        pdf.close()
    return fixed


def render_fix_list(items: list[dict], out_path: str | Path | None = None) -> str:
    """[全局] 修复清单 → 可读文本（供 AI 子代理逐条判断/纠正）

    参数:
        items: detect_anomalies() 产物
        out_path: 可选落盘路径（JSON）
    返回:
        可读清单文本
    """
    lines = ["# 异常片段修复清单（AI 辅助识别）", ""]
    for i, it in enumerate(items, 1):
        lines.append("## %d) 段 %s（章节: %s）" % (i, it["para_id"], it["section"]))
        lines.append("  句子: %s" % it["sentence"])
        if it.get("local_hint"):
            lines.append("  本地提示: %s" % it["local_hint"])
        lines.append("")
    text = "\n".join(lines)
    if out_path:
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        p.with_suffix(".json").write_text(
            json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    return text
