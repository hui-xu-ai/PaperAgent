#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: paperparse/core/latex_util.py
功能: LaTeX 公式工具（P11 增强）：合法性校验 / LaTeX↔纯文本互转 / 规范化比较
     用于双通道差异分类（format_diff=表示差异 vs text_conflict=内容差异）
对外接口: latex_valid / latex_to_text / text_to_latex / canonical_form / contains_latex
版本: v1.0.0 (2026-08-23)
版本历史:
  v1.0.0 初始版本（工具链实测：latex2mathml 严格校验；pylatexenc.latex2text 转换；
         规范化比较用自研 canonicalizer——pylatexenc 对稀疏 LaTeX 转换质量差）
"""
from __future__ import annotations

import re

_LATEX_FRAGMENT = re.compile(r"\$\$.*?\$\$|\$.*?\$|\\[a-zA-Z]{2,}|\\\(|\\\[|\\begin\{")

# 常见 LaTeX 命令 → Unicode（规范化比较与文本转换共用）
_TEX_TO_UNI = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "varepsilon": "ε", "zeta": "ζ", "eta": "η", "theta": "θ", "lambda": "λ",
    "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π", "rho": "ρ", "sigma": "σ",
    "tau": "τ", "phi": "φ", "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Pi": "Π",
    "Sigma": "Σ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
    "times": "×", "div": "÷", "pm": "±", "mp": "∓", "cdot": "·", "leq": "≤",
    "geq": "≥", "neq": "≠", "approx": "≈", "infty": "∞", "to": "→",
    "rightarrow": "→", "leftarrow": "←", "Rightarrow": "⇒", "Leftrightarrow": "⇔",
    "circ": "°", "degree": "°", "AA": "Å", "angstrom": "Å", "sqrt": "√",
    "partial": "∂", "nabla": "∇", "sum": "∑", "prod": "∏", "int": "∫",
    "in": "∈", "notin": "∉", "subset": "⊂", "subseteq": "⊆", "cup": "∪", "cap": "∩",
    "approx": "≈", "simeq": "≃", "cong": "≅", "sim": "∼", "propto": "∝",
    "ldots": "…", "dots": "…", "cdots": "…", "prime": "′", "hbar": "ℏ",
    "mathbb": "", "mathbf": "", "boldsymbol": "", "mathcal": "", "operatorname": "",
    "text": "", "mathrm": "", "mathit": "", "textbf": "", "textit": "",
}
# 删除型命令（保留参数内容）
_STRIP_CMDS = {"mathrm", "text", "mathit", "mathbf", "mathbb", "mathcal",
               "boldsymbol", "operatorname", "textbf", "textit", "left", "right",
               "big", "Big", "bigg", "Bigg", "displaystyle", "textstyle",
               "scriptstyle", "scriptscriptstyle", "quad", "qquad", ","}
# 上/下标 Unicode → 文本
_SUP_MAP = {"⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4", "⁵": "5",
            "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9", "⁻": "-", "⁺": "+",
            "⁽": "(", "⁾": ")", "ⁿ": "n", "ˣ": "x", "ʸ": "y", "ᵃ": "a",
            "ᵇ": "b", "ᶜ": "c", "ᵈ": "d", "ᵉ": "e", "ᶠ": "f", "ᵍ": "g",
            "ʰ": "h", "ⁱ": "i", "ʲ": "j", "ᵏ": "k", "ˡ": "l", "ᵐ": "m"}
_SUB_MAP = {"₀": "0", "₁": "1", "₂": "2", "₃": "3", "₄": "4", "₅": "5",
            "₆": "6", "₇": "7", "₈": "8", "₉": "9", "₊": "+", "₋": "-",
            "ₐ": "a", "ₑ": "e", "ₕ": "h", "ᵢ": "i", "ⱼ": "j", "ₖ": "k",
            "ₗ": "l", "ₘ": "m", "ₙ": "n", "ₒ": "o", "ₚ": "p", "ᵣ": "r",
            "ₛ": "s", "ₜ": "t", "ᵤ": "u", "ᵥ": "v", "ₓ": "x"}
_UNI_TO_TEX = {v: "\\%s" % k for k, v in _TEX_TO_UNI.items() if v}
_MATH_SYM = {"×": r"\times", "÷": r"\div", "±": r"\pm", "·": r"\cdot",
             "≤": r"\leq", "≥": r"\geq", "≠": r"\neq", "≈": r"\approx",
             "∞": r"\infty", "√": r"\sqrt{}", "∑": r"\sum", "∏": r"\prod",
             "∫": r"\int", "∂": r"\partial", "∇": r"\nabla", "∈": r"\in",
             "→": r"\rightarrow", "←": r"\leftarrow", "°": r"^{\circ}",
             "Å": r"\mathrm{\AA}"}
_SUP_UNICODE = "⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺⁽⁾ⁿˣʸᵃᵇᶜᵈᵉᶠᵍʰⁱʲᵏˡᵐ"
_SUB_UNICODE = "₀₁₂₃₄₅₆₇₈₉₊₋"


def contains_latex(text: str) -> bool:
    """[全局] 文本是否含 LaTeX 公式片段"""
    return bool(_LATEX_FRAGMENT.search(text))


def _strip_math_delimiters(s: str) -> str:
    """[局部] 剥离 $..$ / $$..$$ / \\$ 转义 / \\(..\\) / \\[..\\] 边界，返回公式体"""
    t = s.strip().replace("\\$", "$")
    for a, b in (("$$", "$$"), ("$", "$"), ("\\[", "\\]"), ("\\(", "\\)")):
        if t.startswith(a) and t.endswith(b) and len(t) > len(a) + len(b):
            t = t[len(a):-len(b)].strip()
            break
    return t


def latex_valid(latex: str) -> tuple[bool, str]:
    """[全局] 校验 LaTeX 合法性：严格（latex2mathml 转换异常即非法）+
    宽松回退（pylatexenc walker，容忍 latex2mathml 不支持的语法）
    返回 (合法?, 说明)
    """
    body = _strip_math_delimiters(latex)
    if not body:
        return True, ""
    if "\\" not in body and "{" not in body:
        return True, ""   # 纯文本，不判
    try:
        import latex2mathml.converter
        latex2mathml.converter.convert(body)
        return True, ""
    except Exception as e:
        try:
            from pylatexenc.latex2text import LatexNodes2Text
            LatexNodes2Text().latex_to_text(body)
            return True, "宽松通过（%s）" % type(e).__name__
        except Exception:
            return False, "%s: %s" % (type(e).__name__, str(e)[:80])


def latex_to_text(latex: str) -> str:
    """[全局] LaTeX → 纯文本（Unicode 近似）：命令映射 + 剥除包装；失败回退 pylatexenc"""
    body = _strip_math_delimiters(latex)
    if not body:
        return ""
    t = body
    # 包装命令：\mathrm{X} → X
    t = re.sub(r"\\(?:%s)\s*\{([^{}]*)\}" % "|".join(_STRIP_CMDS), r"\1", t)
    # 已知命令 → Unicode
    for name, uni in sorted(_TEX_TO_UNI.items(), key=lambda x: -len(x[0])):
        if uni:
            t = t.replace("\\%s" % name, uni)
    # 先统一 ^/_ 组（保护 "^ { - 1 }" 不被下面剥括号破坏）
    t = _to_canon_unicode(t)
    # 残余命令删除（\left 等）
    t = re.sub(r"\\[a-zA-Z]+\b", "", t)
    t = t.replace("{", "").replace("}", "")
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _to_canon_unicode(t: str) -> str:
    """[局部] 上/下标 Unicode 与 ^/_ 标记 → 统一 token（^{..}/_{..}）"""
    # Unicode 上标 → ^{...}
    out = []
    i = 0
    while i < len(t):
        ch = t[i]
        if ch in _SUP_MAP:
            j = i
            buf = []
            while j < len(t) and t[j] in _SUP_MAP:
                buf.append(_SUP_MAP[t[j]])
                j += 1
            out.append("^{%s}" % "".join(buf))
            i = j
            continue
        if ch in _SUB_MAP:
            j = i
            buf = []
            while j < len(t) and t[j] in _SUB_MAP:
                buf.append(_SUB_MAP[t[j]])
                j += 1
            out.append("_{%s}" % "".join(buf))
            i = j
            continue
        out.append(ch)
        i += 1
    t = "".join(out)
    # LaTeX ^/_ 带花括号（内容可含空格，如 "^ { - 1 }"）→ 统一
    t = re.sub(r"\^\s*\{\s*([^{}]*?)\s*\}",
               lambda m: "^{%s}" % re.sub(r"\s+", "", m.group(1)), t)
    t = re.sub(r"_\s*\{\s*([^{}]*?)\s*\}",
               lambda m: "_{%s}" % re.sub(r"\s+", "", m.group(1)), t)
    # 裸 ^x / _x（无花括号；允许 "^ - 1" 中间空格）
    t = re.sub(r"\^\s*([A-Za-z0-9+\-()]+)", r"^{\1}", t)
    t = re.sub(r"_\s*([A-Za-z0-9+\-()]+)", r"_{\1}", t)
    return t


def canonical_form(text: str) -> str:
    """[全局] 规范化比较形式（双通道差异分类用）：
    含 LaTeX → 命令映射 Unicode + 剥除包装；纯文本 → Unicode 上下标 token 化；
    最终：去空白 + 小写。canonical 相等 = 表示差异（format_diff），不等 = 内容差异。
    """
    if contains_latex(text):
        t = latex_to_text(text)
    else:
        t = text
    t = _to_canon_unicode(t)
    t = t.replace("$", "")
    t = re.sub(r"\s+", "", t)
    return t.lower()


def text_to_latex(text: str) -> str:
    """[全局] 纯文本 → LaTeX（尽力转换：上/下标 Unicode、希腊字母、常用符号）"""
    t = _to_canon_unicode(text)
    # 反向：^{n} 保留；希腊字母 Unicode → \name
    out = []
    i = 0
    while i < len(t):
        ch = t[i]
        if ch in _UNI_TO_TEX and ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
            out.append(_UNI_TO_TEX[ch] + " ")
        elif ch in _MATH_SYM:
            out.append(_MATH_SYM[ch])
        else:
            out.append(ch)
        i += 1
    return "".join(out)
