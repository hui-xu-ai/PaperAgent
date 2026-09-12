#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: paperparse/core/md_qa_check.py
功能: Markdown 清洗后自检（QA 检查，P16）：对最终 en.md 做基本质量问题扫描，
      供用户审阅（**只报告、不改文本**）。
      - 正文段文字过少（孤立段/过短）
      - 正文段句子完整性（句末结束符，排除参考文献引用 [n] / 括号引用）
      - 正文段首是否大写（公式段/数字/符号开头例外）
      - 图的数量 vs 图注数量对应（image 标记 vs Fig./Table/Scheme 图注）
      可选传入 document.json 以核对 figures 数量。
对外接口: check_markdown_qa / classify_block
版本: v1.0.0 (2026-08)
"""
from __future__ import annotations

import json
import re
from pathlib import Path

__all__ = ["check_markdown_qa", "classify_block"]

# 正文段词数低于此值判"过短/孤立"（可配置）
DEFAULT_SHORT_WORDS = 8

_HEADING_RE = re.compile(r"^#{1,6}\s+")
_IMG_RE = re.compile(r"^!\[[^\]]*\]\([^)]*\)\s*$")
_CAPTION_RE = re.compile(r"^(fig(?:ure)?|table|scheme)\.?\s*\d+[\.\s]", re.I)
_EQ_RE = re.compile(r"^\$\$[\s\S]*\$\$\s*$")
_REF_CITE_RE = re.compile(r"\[[\d\s,;,\-–—]+\]\s*$")        # 尾随 [n],[n,m]
_PAREN_CITE_RE = re.compile(r"\([^()\d]{0,12}\d[^()]*\)\s*$")  # 尾随 (…n…)
_LEAD_ALPHA_RE = re.compile(r"([A-Za-z])")


def classify_block(block: str) -> str:
    """[局部] markdown 块分类：heading/caption/image/equation/ref_entry/body"""
    t = block.strip()
    if not t:
        return "empty"
    if _HEADING_RE.match(t):
        return "heading"
    if t.lower().startswith("## references"):
        return "refs_title"
    if _IMG_RE.match(t):
        return "image"
    if _CAPTION_RE.match(t):
        return "caption"
    if _EQ_RE.match(t) or t.startswith("$$"):
        return "equation"
    if re.match(r"^\[\d+\]", t):
        return "ref_entry"
    return "body"


def _split_blocks(md_text: str) -> list[str]:
    """[局部] 按空行切块（剥 YAML frontmatter 与 callout）"""
    lines = md_text.splitlines()
    # 剥 YAML frontmatter（--- … ---）
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                lines = lines[i + 1:]
                break
    blocks, cur = [], []
    for ln in lines:
        if ln.strip():
            cur.append(ln)
        else:
            if cur:
                blocks.append("\n".join(cur))
                cur = []
    if cur:
        blocks.append("\n".join(cur))
    return blocks


def _word_count(text: str) -> int:
    """[局部] 正文词数：剥 LaTeX/标签/引用后计英文词 + 数字 token"""
    t = re.sub(r"\$[^$]*\$|\$\$[\s\S]+?\$\$", " ", text)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"\[[\d\s,;,\-–—]+\]", " ", t)
    words = re.findall(r"[A-Za-z]+(?:[A-Za-z'-]*[A-Za-z])?", t)
    nums = re.findall(r"\d+(?:[.,]\d+)*", t)
    return len(words) + len(nums)


def _strip_trailing_refs(text: str) -> str:
    """[局部] 剥尾随参考文献引用 [n]/(…n…) 后取句尾字符"""
    t = text.rstrip()
    prev = None
    while t != prev:
        prev = t
        t = _REF_CITE_RE.sub("", t).rstrip()
        t = _PAREN_CITE_RE.sub("", t).rstrip()
    return t


def _ends_terminal(text: str) -> tuple[bool, str, str]:
    """[局部] 句尾检查：剥尾随引用后，末字符是否句末结束符(.!?/公式)
    返回 (完整?, 末字符, 状态)  状态: ok/colon/formula/no_terminal"""
    stripped = _strip_trailing_refs(text)
    if stripped.endswith(("$", "$$")):
        return True, stripped[-5:], "formula"
    if not stripped:
        return False, "", "empty"
    last = stripped[-1]
    if last in ".!?":
        return True, last, "ok"
    if last == ":":
        return False, last, "colon"      # 引导句（方程/列表前），非错误但提示
    if last in ",;":
        return False, last, "comma"
    return False, last, "no_terminal"


def _starts_upper(text: str) -> tuple[bool, str]:
    """[局部] 段首大写检查：找首个字母，小写即有问题。
    公式/数字/符号开头 → 跳过（返回 True 表示不适用/不报）。"""
    t = text.lstrip()
    if not t:
        return True, ""
    if t.startswith(("$", "\\", "(", "[", "“", "‘")):
        return True, ""                     # 公式/引号开头不判
    m = _LEAD_ALPHA_RE.search(t)
    if not m:
        return True, ""                     # 无字母（纯公式/数字）不判
    return m.group(1).isupper(), m.group(1)


def check_markdown_qa(md_text: str, *,
                      doc_json: str | Path | None = None,
                      short_words: int = DEFAULT_SHORT_WORDS) -> dict:
    """[全局] 对最终 markdown 做清洗后 QA 检查（只报告，不改文本）。

    返回报告 dict：
      stats: 各 kind 块数 / 问题计数
      figures: {image_markers, captions, doc_figures, match}
      paragraphs: 每个 body 段的 {idx, text, words, short, end_state, end_char,
                  starts_upper, upper_char, flags}
      issues: 汇总问题清单（供审阅）
    """
    blocks = _split_blocks(md_text)
    body_paras: list[dict] = []
    img_count = cap_count = 0
    in_refs = False
    for i, blk in enumerate(blocks):
        kind = classify_block(blk)
        if kind == "refs_title":
            in_refs = True
            continue
        if kind == "image":
            img_count += 1
            continue
        if kind == "caption":
            cap_count += 1
            continue
        if kind in ("equation", "ref_entry", "heading"):
            continue
        if kind != "body" or in_refs:
            continue
        t = blk.strip()
        words = _word_count(t)
        ok_end, end_char, end_state = _ends_terminal(t)
        up_ok, up_char = _starts_upper(t)
        flags: list[str] = []
        if words < short_words:
            flags.append("short")
        if not ok_end:
            flags.append("end_" + end_state)   # end_colon/end_comma/end_no_terminal
        if not up_ok:
            flags.append("lower_start")
        body_paras.append({
            "idx": i, "text": t, "words": words, "short": words < short_words,
            "end_state": end_state, "end_char": end_char,
            "starts_upper": up_ok, "upper_char": up_char, "flags": flags})
    # 图 vs 图注
    doc_figs = None
    if doc_json and Path(doc_json).exists():
        try:
            doc = json.loads(Path(doc_json).read_text(encoding="utf-8"))
            figs = doc.get("figures") or []
            doc_figs = len(figs) if isinstance(figs, list) else None
        except Exception:  # noqa: BLE001
            doc_figs = None
    fig_match = (img_count == cap_count)
    issues: list[str] = []
    for p in body_paras:
        for f in p["flags"]:
            tag = {"short": "过短/孤立", "end_colon": "句尾冒号(可能引导公式/列表)",
                   "end_comma": "句尾逗号", "end_no_terminal": "句尾无结束符",
                   "lower_start": "段首小写"}[f]
            issues.append("[%s] 段%d(%d词): %s — %s" % (
                tag, p["idx"], p["words"], p["text"][:70], f))
    if not fig_match:
        issues.append("[图图注不对应] image标记=%d 图注=%d" % (img_count, cap_count))
    return {
        "stats": {"blocks": len(blocks), "body": len(body_paras),
                  "images": img_count, "captions": cap_count},
        "figures": {"image_markers": img_count, "captions": cap_count,
                    "doc_figures": doc_figs,
                    "match": fig_match and (doc_figs is None
                                            or doc_figs == img_count)},
        "paragraphs": body_paras,
        "issues": issues,
        "config": {"short_words": short_words},
    }
