# -*- coding: utf-8 -*-
"""译文文本形态归一（**纯函数、无依赖**）——上下标标记的"补全"。

2026-09-23 用户报障：中文译文里同一件事（文献引用上标）有三种写法，读者看到的效果完全不同。
实测（`kb/10.1016_j.cej.2025.167798`）：
  ① `$^{[29]}$`            —— 规范（37 处）；
  ② `^[[38]]`              —— 模型自己发明的形态（6 处）。Markdown/Obsidian 把 `^[文字]`
                              当**内联脚注**，于是文献编号被渲染成脚注、正文里留着裸露的 `^[[38]]`；
  ③ `<sup>[21,22]</sup>`   —— 模型保留的 HTML 标签。`_strip_html_tags` 连标签一起删 ⇒
                              **上标语义直接丢失**（只剩正文方括号 `[21,22]`）。

本模块把 ②③ 归一到**与英文原文同一约定**：`^{[38]}`（裸上标）。包 `$...$` 由展示层负责
（`engine_service._clean_html` 与模板渲染本来就会把裸 `^{...}` 包成 `$^{...}$`），
于是**数据层两种语言同形、展示层统一带 `$`**。

为什么要在数据层（document.json）就归一：检索/问答/笔记都读同一份文本，只在渲染层包 `$`
的话，那些入口拿到的仍是坏文本。渲染层再加一道兜底（同一函数），历史数据不必重译即可修复。
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# 上标/下标 HTML 标签 → TeX 记法。**必须先转义再删标签**，否则上标语义丢失（见模块头 ③）。
_SUP_TAG_RE = re.compile(r"<sup>\s*(.*?)\s*</sup>", re.IGNORECASE | re.DOTALL)
_SUB_TAG_RE = re.compile(r"<sub>\s*(.*?)\s*</sub>", re.IGNORECASE | re.DOTALL)

# 引用上标的正文：数字 + 常见分隔（逗号 / 连字符 / 短破折号– / 长破折号— / 空格）
_CITE_BODY = r"\d[\d\s,\u2013\u2014\-]*"
# 非规范形态：`^[[38]]` / `^[[38]` / `^[38]` / `^[38]]`（方括号一层或两层、允许不对称）
_CITE_SUP_RE = re.compile(r"\^\[\[?(" + _CITE_BODY + r")\]?\]")
# 归一后仍以 `^[` 开头的片段（监测：可能是真脚注，也可能是模型又发明的新写法）
_SUSPECT_RE = re.compile(r"\^\[[^\]\n]{0,40}\]?")


def html_script_to_tex(text: str) -> str:
    """`<sup>x</sup>` → `^{x}`、`<sub>x</sub>` → `_{x}`（保留语义，供随后清标签）。"""
    if not text or "<" not in text:
        return text or ""
    out = _SUB_TAG_RE.sub(lambda m: "_{" + m.group(1) + "}", text)
    return _SUP_TAG_RE.sub(lambda m: "^{" + m.group(1) + "}", out)


def normalize_citation_superscripts(text: str) -> tuple[str, int]:
    """`^[[38]]` / `^[38]` → `^{[38]}`；返回 (新文本, 修了几处)。**幂等**。

    只动"内容是数字/引用分隔符"的方括号上标 —— 真正的内联脚注（如 `^[见附录]`）不碰。
    """
    if not text or "^[" not in text:
        return text or "", 0
    fixed = 0

    def _repl(m: re.Match) -> str:
        nonlocal fixed
        fixed += 1
        return "^{[" + re.sub(r"\s+", " ", m.group(1)).strip() + "]}"

    return _CITE_SUP_RE.sub(_repl, text), fixed


def suspicious_superscripts(text: str) -> list[str]:
    """归一后仍以 `^[` 开头的片段（**监测**信号：真脚注，或模型的新形态）。"""
    return [m.group(0) for m in _SUSPECT_RE.finditer(text or "")]


# 数学片段（块级 `$$...$$` / 行内 `$...$`）——包 `$` 前先抽出保护，避免把
# `$\mathrm{Co(O_{x})}$` 里的 `_{x}` 撑成非法嵌套 `$\mathrm{Co(O$_{x}$)}$`。
_MATH_SPAN_RE = re.compile(r"\$\$[^$]*\$\$|\$[^$]*\$")
_BARE_SCRIPT_RE = re.compile(r"(?<![$\{])([\^_])\{([^}]+)\}(?![$\}])")


def wrap_bare_scripts(text: str) -> tuple[str, int]:
    """公式外的裸 `^{...}` / `_{...}` → `$^{...}$` / `$_{...}$`；返回 (新文本, 修了几处)。

    与变体渲染（`engine_service._clean_html`）**同一套规则**：先抽 `$...$` / `$$...$$`
    占位保护，只对数学环境外的裸上下标包 `$`，再回填。幂等（已包的不再动）。
    """
    if not text or ("^{" not in text and "_{" not in text):
        return text or "", 0
    stash: list[str] = []

    def _stash(m: re.Match) -> str:
        stash.append(m.group(0))
        return "\x00M%d\x00" % (len(stash) - 1)

    md = _MATH_SPAN_RE.sub(_stash, text)
    fixed = 0

    def _wrap(m: re.Match) -> str:
        nonlocal fixed
        fixed += 1
        return "$" + m.group(1) + "{" + m.group(2) + "}$"

    md = _BARE_SCRIPT_RE.sub(_wrap, md)
    md = re.sub(r"\x00M(\d+)\x00", lambda m: stash[int(m.group(1))], md)
    return md, fixed
