# -*- coding: utf-8 -*-
"""LaTeX 占位符标签切分与回填（自 paperparse.llm.latextap 迁入，纯函数无依赖）。

把公式/$..$ 与 <sup> 替换为稳定标签 [[MATHn]]，AI 翻译时以标签引用公式，
程序 reassemble 逐字节回填 → 公式与源文 100% 一致。
"""
from __future__ import annotations

import re

__all__ = ["MATH_RE", "split_text", "reassemble"]

MATH_RE = [
    re.compile(r"\$\$.*?\$\$", re.S),
    re.compile(r"\$[^$]*?\$"),
    re.compile(r"\\\(.*?\\\)", re.S),
    re.compile(r"\\\[.*?\\\]", re.S),
    re.compile(r"\\begin\{[^}]*\}.*?\\end\{[^}]*\}", re.S),
    re.compile(r"<sup>.*?</sup>", re.S),
]


def _collect_spans(text: str) -> list[tuple[int, int, str]]:
    found: list[tuple[int, int, str]] = []
    for pat in MATH_RE:
        for m in pat.finditer(text):
            found.append((m.start(), m.end(), m.group(0)))
    found.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    out: list[tuple[int, int, str]] = []
    last_end = -1
    for start, end, tok in found:
        if start < last_end:
            continue
        out.append((start, end, tok))
        last_end = end
    return out


def split_text(text: str) -> tuple[str, list[str]]:
    if not text:
        return "", []
    math_list: list[str] = []
    spans = _collect_spans(text)
    if not spans:
        return text, []
    chunks: list[str] = []
    pos = 0
    for start, end, tok in spans:
        if start > pos:
            chunks.append(text[pos:start])
        math_list.append(tok)
        chunks.append("[[MATH%d]]" % (len(math_list) - 1))
        pos = end
    if pos < len(text):
        chunks.append(text[pos:])
    return "".join(chunks), math_list


def reassemble(labeled_text: str, math_list: list[str]) -> str:
    if not math_list:
        return labeled_text

    def _rep(m: re.Match) -> str:
        n = int(m.group(1))
        return math_list[n] if 0 <= n < len(math_list) else m.group(0)

    return re.sub(r"\[\[MATH(\d+)\]\]", _rep, labeled_text)
