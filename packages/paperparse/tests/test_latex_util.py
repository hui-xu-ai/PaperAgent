#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: tests/test_latex_util.py
功能: P11 LaTeX 工具单测（规范化等价 / 合法性校验 / 互转）
对外接口: 无（测试）
版本: v1.0.0 (2026-08-23)
版本历史:
  v1.0.0 初始版本
"""
from paperparse.core.latex_util import (canonical_form, contains_latex,
                                        latex_to_text, latex_valid,
                                        text_to_latex)


def test_canonical_equivalence_formula_repr():
    """同一公式两种表示（mineru LaTeX vs paddleocr Unicode）→ canonical 相等"""
    cases = [
        (r"$3 . 2 7 \mathrm { S } \mathrm { c m } ^ { - 1 } \left( \mathrm { P } - \mathrm { LIG } \right)$",
         "3.27 S cm⁻¹ (P-LIG)"),
        (r"$\mathrm{CoO}_x$", "CoOₓ"),
        (r"$\gamma$-ray", "γ-ray"),
        (r"$\mathrm { N } _ { 2 }$", "N₂"),
        (r"to $3 . 2 7 \mathrm { S } \mathrm { c m } ^ { - 1 }$ results",
         "to 3.27 S cm⁻¹ results"),
    ]
    for m, p in cases:
        assert canonical_form(m) == canonical_form(p), (m, p)


def test_canonical_difference_content():
    assert canonical_form(r"$\alpha$") != canonical_form("beta")
    assert canonical_form(r"$x^2$") != canonical_form("x³")
    assert canonical_form("cost-efective") != canonical_form("cost-effective")


def test_latex_valid_strict():
    ok, _ = latex_valid(r"\frac{a}{b}")
    assert ok is True
    ok, detail = latex_valid(r"\mathrm{CoO}_x")
    assert ok is True
    # 纯文本不判
    ok, _ = latex_valid("plain text")
    assert ok is True


def test_contains_latex():
    assert contains_latex(r"$x^2$") is True
    assert contains_latex(r"\mathrm{CoO}") is True
    assert contains_latex("plain text here") is False


def test_latex_to_text():
    out = latex_to_text(r"$\mathrm{CoO}_x$")
    assert "CoO" in out
    out2 = latex_to_text(r"\frac{a}{b}")
    assert out2  # 不崩溃


def test_text_to_latex():
    assert "^{" in text_to_latex("cm⁻¹")
    assert text_to_latex("γ") == "\\gamma "
