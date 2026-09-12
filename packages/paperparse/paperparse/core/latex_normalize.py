#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: skill/src/paperparse/core/latex_normalize.py
功能: LaTeX 规范化/卫生（skill 固定环节，方案已确认）：
      - strip_control_chars：清除破坏 LaTeX 的不可见控制字符（\\x08退格/\\x07 BEL/\\x0b VT 等），
        保 \\n/\\t/\\r
      - normalize_document：对全部段落 text_en 与 text_zh 统一执行 控制字清扫 + KNOWN_FIXES，
        修源文语义错（如 \\bar{2}→^2）——写盘前固定调用，对所有译文统一生效（而非仅当前一篇）
对外接口: strip_control_chars / normalize_document
版本: v1.0.0 (2026-08-19)
版本历史:
  v1.0.0 初始版本（通用清洗；方案确认）
"""
from __future__ import annotations

import re

from paperparse.core.anomaly_detect import KNOWN_FIXES
from paperparse.middleware.schema import ArticleDocument

__all__ = ["strip_control_chars", "normalize_document",
           "normalize_formula_fragments"]

# 破坏 LaTeX 的控制字符（退格/BEL/VT/FF/US 等）；保留 \n \t \r
_CTRL_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")

# ---- P16：公式碎片归一化（方案A：合法 LaTeX 化）----
# mineru 官方输出常把公式字符被空格拆散（"0 . 3 7"、"^ { \circ }"、"\mathrm { C o O }"）。
# 数学模式下空白本无语义 → 折叠为合法紧凑 LaTeX；\text/\mbox 内空格有意义须保护。
_FORMULA_RE = re.compile(r"\$\$[\s\S]+?\$\$|\$[^$\n]+?\$")
_TEXT_CMD_RE = re.compile(r"\\text\s*\{[^{}]*\}|\\mbox\s*\{[^{}]*\}")
_CTRL_SPACE_RE = re.compile(r"\\ ")
_PLACEHOLDER_RE = re.compile(r"\u0003(\d+)\u0003")
# P16：孤儿 LaTeX 命令（mineru 有时不给 $ 定界，如 "\mathrm{CoO_x}@LIG"）
_ORPHAN_LATEX_RE = re.compile(
    r"(?<![\\$])\\(?:mathrm|mathsf|mathbf|mathit|textbf|textrm|operatorname"
    r"|rm|it|bf|frac|approx|cdot|circ|times|rightarrow|left|right|alpha|beta|"
    r"gamma|delta|theta|pi|mu|nu|Omega|infty)\b[^\s$]{0,60}")


def wrap_orphan_latex(text: str | None) -> str:
    r"""[全局] 给孤儿 LaTeX 命令补 $...$ 定界（mineru 有时漏 $，导致 KaTeX 不渲染）。
    已配对的 $...$ 区间不动（先占位保护）。如 "\mathrm{CoO_x}@LIG" → "$\mathrm{CoO_x}@LIG$"。"""
    if not text:
        return text or ""
    prot: list[str] = []
    def _save(m):
        prot.append(m.group(0))
        return "\u0002%d\u0002" % (len(prot) - 1)
    def _rest(m):
        return prot[int(m.group(1))]
    t = _FORMULA_RE.sub(_save, text)          # 保护已配对 $...$
    t = _ORPHAN_LATEX_RE.sub(lambda m: "$%s$" % m.group(0), t)
    return re.sub(r"\u0002(\d+)\u0002", _rest, t)


def _normalize_math_block(m: re.Match) -> str:
    """[局部] 单个 $...$/$$...$$ 数学块：折叠碎片空格 → 合法紧凑 LaTeX"""
    whole = m.group(0)
    is_block = whole.startswith("$$")
    inner = whole[2:-2] if is_block else whole[1:-1]
    prot: list[str] = []

    def _save(mm):
        prot.append(mm.group(0))
        return "\u0003%d\u0003" % (len(prot) - 1)

    def _rest(mm):
        return prot[int(mm.group(1))]

    inner = _TEXT_CMD_RE.sub(_save, inner)          # 保护 \text/\mbox 内容
    inner = _CTRL_SPACE_RE.sub(lambda _m: "\u0005", inner)  # 控制空格 \\ 占位（恢复 \\ ）
    # P16 回归修复（2026-08-26）：删空格前保护**命令分隔空格**——\cmd + 空格 + 字母
    # （\Delta V / \tt BF4 / {\bf h}）。删掉后 \DeltaV/\ttBF4/{\bfh} 会变成未知命令
    # （TeX 命令名贪婪读入字母）→ KaTeX 渲染失败。命令后跟 {/\\/数字/符号不需要空格
    # （\mathrm{CoO}、\approx0.34 合法）→ 不保护、随碎片空格一起删。
    # lookahead 检查命令名**之后**的空格+字母，贪婪回溯到命令名中间时（\mathr|m{...}
    # m 后无空格）不匹配，无回溯误插。
    inner = re.sub(r"\\([a-zA-Z]+)(?=\s+[A-Za-z])",
                   lambda m: "\\" + m.group(1) + "\u0004", inner)
    inner = re.sub(r"\s+", "", inner)               # 折叠碎片空格
    inner = inner.replace("\u0004", " ")
    inner = inner.replace("\u0005", "\\ ")
    inner = _PLACEHOLDER_RE.sub(_rest, inner)
    return "$$%s$$" % inner if is_block else "$%s$" % inner


def normalize_formula_fragments(text: str | None) -> str:
    r"""[全局] 公式碎片归一化：把 mineru 空格拆散的 $...$ 折叠为合法 LaTeX。
    如 "$4 3 . 1 ^ { \circ } \ ( 2 \theta )$" → "$43.1^{\circ}\ (2\theta)$"；
    "${ \approx } 0 . 3 4$" → "${\approx}0.34$"。\text/\mbox 内空格保留。"""
    if not text:
        return text or ""
    return _FORMULA_RE.sub(_normalize_math_block, text)


def strip_control_chars(text: str | None) -> str:
    """[全局] 清除文本中的破坏性控制字符（\\x00-\\x08、\\x0b、\\x0c、\\x0e-\\x1f、\\x7f）

    参数:
        text: 任意文本（可为 None）
    返回:
        清洗后文本（换行/制表/回车保留）
    """
    if not text:
        return text or ""
    return _CTRL_RE.sub("", text)


def normalize_document(doc: ArticleDocument) -> int:
    """[全局] 文档级 LaTeX 规范化：对全部非标题段落 text_en 与 text_zh
    统一做 控制字符清扫 + KNOWN_FIXES（含修正 \\bar{2}→^2 等源文语义错）。

    参数:
        doc: ArticleDocument（原地修改 text_en / text_zh；ai_summary 亦清扫）
    返回:
        被修改的段落数
    """
    changed = 0
    for p in doc.paragraphs:
        if p.is_heading:
            continue
        para_changed = False
        for field in ("text_en", "text_zh"):
            val = getattr(p, field)
            if val is None:
                continue                          # 保留 None（未译段）
            t = strip_control_chars(val)
            for pat, repl, _note in KNOWN_FIXES:
                new = re.sub(pat, lambda _m: repl, t)   # lambda: repl 反斜杠不被 re 解析
                if new != t:
                    t = new
            if t != val:
                setattr(p, field, t)
                para_changed = True
        if para_changed:
            changed += 1
    # ai_summary 清扫控制字符（防污染）
    if doc.ai_summary:
        for k, v in doc.ai_summary.items():
            if isinstance(v, str) and _CTRL_RE.search(v):
                doc.ai_summary[k] = strip_control_chars(v)
    return changed
