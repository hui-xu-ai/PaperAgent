#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: skill/src/paperparse/core/latex_check.py
功能: LaTeX 语法校验（用户问题：校验阶段只能通过 LaTeX 语法定位错误源）：
      - 命令合法性：反斜杠 word 是否在常用 LaTeX 命令白名单（非法命令 → 异常定位）
      - 美元符 $ 配对：未闭合的 $..$ / $$..$$（转义美元符除外）
      - 花括号配对：{} 不匹配
      - begin/env 与 end/env 配对
      语法层（自动、确定性）→ 输出异常清单；语义层（如 Nu_2 化学式误用——
      语法合法但语义错）→ 由 AI 子代理判断（anomaly_detect 配合）。
对外接口: check_latex_syntax / check_document_latex
版本: v1.0.0 (2026-08-18)
版本历史:
  v1.0.0 初始版本（用户问题 4 深化：语法定位错误源）
"""
from __future__ import annotations

import re
from pathlib import Path

from paperparse.middleware.schema import ArticleDocument

__all__ = ["check_latex_syntax", "check_document_latex"]

# 常用 LaTeX 命令白名单（覆盖本文档数学/文本模式命令）
KNOWN_COMMANDS = {
    # 字体/样式
    "mathrm", "mathsf", "mathbf", "mathit", "mathbb", "mathcal", "mathscr",
    "mathfrak", "boldsymbol", "text", "mbox", "hbox", "textrm", "textbf",
    "textit", "textnormal", "emph", "underline", "overline", "overbrace",
    "underbrace", "substack", "binom", "tfrac", "dfrac",
    # 希腊字母（大写/小写）
    "alpha", "beta", "gamma", "delta", "epsilon", "varepsilon", "zeta",
    "eta", "theta", "vartheta", "iota", "kappa", "lambda", "mu", "nu",
    "xi", "pi", "varpi", "rho", "varrho", "sigma", "varsigma", "tau",
    "upsilon", "phi", "varphi", "chi", "psi", "omega",
    "Gamma", "Delta", "Theta", "Lambda", "Xi", "Pi", "Sigma", "Upsilon",
    "Phi", "Psi", "Omega", "Nu", "Mu", "Xi",
    # 运算/关系
    "circ", "pm", "mp", "times", "div", "cdot", "ast", "star", "dagger",
    "ddagger", "leq", "geq", "neq", "approx", "equiv", "propto", "sim",
    "cong", "le", "ge", "ll", "gg", "subset", "supset", "subseteq",
    "supseteq", "in", "notin", "cup", "cap", "setminus", "oplus",
    "otimes", "to", "rightarrow", "leftarrow", "leftrightarrow",
    "Rightarrow", "Leftarrow", "longrightarrow", "uparrow", "downarrow",
    "partial", "nabla", "infty", "sum", "prod", "int", "oint", "iint",
    "iiint", "sqrt", "frac", "lim", "log", "ln", "exp", "sin", "cos",
    "tan", "cot", "sec", "csc", "arcsin", "arccos", "arctan", "deg",
    "min", "max", "sup", "inf", "det", "dim", "ker", "hom", "arg", "gcd",
    # 结构/布局
    "left", "right", "big", "Big", "bigg", "Bigg", "bigl", "bigr",
    "Bigl", "Bigr", "quad", "qquad", "space", "hspace", "vspace", "text",
    "begin", "end", "hline", "label", "ref", "cite", "item", "multicolumn",
    "textbf", "textit", "texttt", "times", "thinspace", "enspace",
    # P-ENHANCE R07：MinerU 常见合法命令补全（ncomms/cej/sciadv/s40820 实测）
    "therefore", "vert", "check", "bullet", "bf", "tt", "operatorname",
    "varDelta", "varGamma", "varTheta", "varLambda", "varXi", "varPi",
    "varSigma", "varUpsilon", "varPhi", "varPsi", "varOmega",
    "ldots", "dots", "cdots", "mid", "parallel", "perp", "angle",
    "triangle", "square", "hbar", "ell", "prime", "bmod", "pmod",
    "Re", "Im", "nabla", "partial", "triangleq", "simeq", "cong",
    "stackrel", "overset", "underset", "phantom", "hphantom", "vphantom",
    "textsuperscript", "textsubscript", "raisebox", "rotatebox", "scalebox",
    "noindent", "indent", "newline", "linebreak", "par", "hfill", "vfill",
    "substack", "subarray", "array", "matrix", "pmatrix", "bmatrix",
    "cases", "aligned", "gathered", "split", "small", "normalsize",
    "footnotesize", "scriptsize", "tiny", "large", "Large", "LARGE",
    "huge", "Huge", "displaystyle", "textstyle", "scriptstyle",
    "scriptscriptstyle", "operatorname", "limits", "nolimits", "top",
    "bot", "dagger", "ddagger", "S", "P", "copyright", "pounds", "yen",
    "euro", "dollar", "cent", "neg", "land", "lor", "lnot", "wedge",
    "vee", "oplus", "ominus", "otimes", "oslash", "odot", "circ",
    "bigcirc", "amalg", "uplus", "sqcap", "sqcup", "sqsubseteq", "sqsupseteq",
    # 其他常见
    "vec", "hat", "bar", "tilde", "dot", "ddot", "widetilde", "widehat",
    "overrightarrow", "overleftrightarrow", "small", "large", "Large",
    "displaystyle", "textstyle", "scriptstyle", "scriptscriptstyle",
    "max", "min", "exists", "forall", "neg", "land", "lor", "mathbb",
    "mathrm", "thickspace", "medspace", "celsius", "degree", "angstrom",
    "stackrel", "overset", "underset", "phantom", "hphantom", "vphantom",
    "textsuperscript", "textsubscript", "raisebox", "rotatebox", "scalebox",
    "noindent", "indent", "newline", "linebreak", "par", "hfill", "vfill",
}
_CMD_RE = re.compile(r"\\([a-zA-Z]+)")
_DOLLAR_RE = re.compile(r"(?<!\\)\$\$|(?<!\\)\$")


def check_latex_syntax(text: str) -> list[dict]:
    """[全局] 单文本 LaTeX 语法校验（命令合法性 + $ 配对 + 花括号配对 + env 配对）

    参数:
        text: 含 LaTeX 的文本（段落）
    返回:
        异常列表 [{"kind", "detail", "near"}]：
        kind: unknown_command / unclosed_math / unmatched_brace / unmatched_env
    """
    errors: list[dict] = []
    if "$" not in text and "\\" not in text:
        return errors

    # 1) 命令合法性（忽略转义控制 \ 与行尾 \）
    for m in _CMD_RE.finditer(text):
        cmd = m.group(1)
        if cmd not in KNOWN_COMMANDS:
            s = max(0, m.start() - 25)
            errors.append({"kind": "unknown_command", "detail": "\\%s" % cmd,
                           "near": text[s: m.end() + 25].strip()})

    # 2) $ 配对（跳过 \${1,2} 计数；$$..$$ 与 $..$ 交替计数）
    dollars = [(m.start(), m.group(0)) for m in _DOLLAR_RE.finditer(text)]
    if len(dollars) % 2 != 0:
        near = text[max(0, dollars[-1][0] - 25): dollars[-1][0] + 25].strip()
        errors.append({"kind": "unclosed_math", "detail": "美元符 $ 未配对（奇数个）",
                       "near": near})

    # 3) 花括号配对
    depth = 0
    for i, ch in enumerate(text):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                errors.append({"kind": "unmatched_brace",
                               "detail": "多余右花括号 }",
                               "near": text[max(0, i - 25): i + 25].strip()})
                depth = 0
    if depth > 0:
        errors.append({"kind": "unmatched_brace", "detail": "左花括号 { 未闭合（%d 个）"
                       % depth, "near": text[-60:].strip()})

    # 4) \begin{env} / \end{env} 配对
    envs = [(m.group(1), m.group(2))
            for m in re.finditer(r"\\(begin|end)\{([a-zA-Z*]+)\}", text)]
    stack: list[str] = []
    for kind, env in envs:
        if kind == "begin":
            stack.append(env)
        elif stack and stack[-1] == env:
            stack.pop()
        else:
            errors.append({"kind": "unmatched_env",
                           "detail": "\\end{%s} 与 \\begin{%s} 不配对"
                           % (env, stack[-1] if stack else "?"),
                           "near": text[-80:].strip()})
            if stack:
                stack.pop()
    if stack:
        errors.append({"kind": "unmatched_env",
                       "detail": "\\begin{%s} 未闭合" % stack[-1],
                       "near": text[-80:].strip()})
    return errors


def check_document_latex(doc: ArticleDocument) -> list[dict]:
    """[全局] 全文档 LaTeX 语法校验（段落级），返回异常清单（供 AI 修复）

    参数:
        doc: ArticleDocument
    返回:
        异常列表 [{"para_id", "section", "kind", "detail", "near"}]
    """
    out: list[dict] = []
    for p in doc.paragraphs:
        if p.is_heading or not p.text_en:
            continue
        for err in check_latex_syntax(p.text_en):
            err = dict(err)
            err["para_id"] = p.para_id
            err["section"] = p.section
            out.append(err)
    return out
