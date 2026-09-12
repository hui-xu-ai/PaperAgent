#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: tests/test_latex_check.py
功能: LaTeX 语法校验器测试：
      - 非法命令检测（拼写错误命令）
      - 美元符配对（未闭合/转义美元符不计）
      - 花括号配对
      - env 配对
      - 合法文本无异常
对外接口: 无（测试）
版本: v1.0.0 (2026-08-18)
版本历史:
  v1.0.0 初始版本
"""
from paperparse.core.latex_check import check_latex_syntax, check_document_latex
from paperparse.middleware.schema import ArticleDocument, ArticleMetadata, Paragraph


def test_unknown_command():
    """非法 LaTeX 命令（拼写错误）定位"""
    errs = check_latex_syntax(r"formula $\epsilno$ value")
    kinds = [e["kind"] for e in errs]
    assert "unknown_command" in kinds
    assert any("epsilno" in e["detail"] for e in errs)


def test_valid_commands_ok():
    """常见/扩展命令不误报"""
    t = (r"$\stackrel { . } { e V }$ $\Delta V = \Phi - \varphi$ "
         r"$\mathrm { N } _ { 2 }$ \textsuperscript{x} $8 0 0 ^ { \circ } \mathrm { C }$")
    assert check_latex_syntax(t) == []


def test_unclosed_math():
    """美元符未配对"""
    errs = check_latex_syntax(r"unclosed $x = 1")
    assert any(e["kind"] == "unclosed_math" for e in errs)


def test_escaped_dollar_ignored():
    """转义美元符（\\$）不参与配对"""
    errs = check_latex_syntax(r"price \$5 and $x$ ok")
    assert errs == []


def test_unmatched_brace():
    """花括号不匹配"""
    errs = check_latex_syntax(r"$x = {1}$ trailing {")
    assert any(e["kind"] == "unmatched_brace" for e in errs)


def test_unmatched_env():
    """begin/end 不配对"""
    errs = check_latex_syntax(r"\begin{equation}x\end{align}")
    assert any(e["kind"] == "unmatched_env" for e in errs)


def test_document_check():
    """全文档校验：返回段落定位"""
    doc = ArticleDocument(
        metadata=ArticleMetadata(extraction_time="x"),
        paragraphs=[Paragraph(para_id="P001", order=1, section="S",
                              text_en=r"bad \epsilno here", confidence=0.9)])
    errs = check_document_latex(doc)
    assert len(errs) == 1
    assert errs[0]["para_id"] == "P001"
