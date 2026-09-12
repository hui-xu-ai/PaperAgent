#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""latex_util 冒烟测试（真实双通道公式差异样本）"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from paperparse.core.latex_util import (canonical_form, latex_valid, latex_to_text,
                                        text_to_latex, contains_latex)

CASES = [
    # (mineru LaTeX 表示, paddleocr 纯文本表示, 期望: 规范化相等?)
    (r"$3 . 2 7 \mathrm { S } \mathrm { c m } ^ { - 1 } \left( \mathrm { P } - \mathrm { LIG } \right)$",
     "3.27 S cm⁻¹ (P-LIG)", True),
    (r"$\mathrm{CoO}_x$", "CoOₓ", True),
    (r"$\gamma$-ray", "γ-ray", True),
    (r"$\mathrm { N } _ { 2 }$", "N₂", True),
    (r"to $3 . 2 7 \mathrm { S } \mathrm { c m } ^ { - 1 }$ results from",
     "to 3.27 S cm⁻¹ results from", True),
    # 内容不同 → 不等
    (r"$\alpha$", "beta", False),
    (r"$x^2$", "x³", False),
    # 纯文本 vs 纯文本
    ("cost-efective", "cost-effective", False),
]

print("== canonical 等价性 ==")
ok = True
for m, p, expect in CASES:
    cm, cp = canonical_form(m), canonical_form(p)
    eq = cm == cp
    mark = "OK " if eq == expect else "FAIL"
    if eq != expect:
        ok = False
    print("%s M=%-40r P=%-30r eq=%s (expect %s)" % (mark, cm[:38], cp[:28], eq, expect))

print("\n== 合法性校验 ==")
for s in (r"\frac{a}{b}", r"\frac{a}{", r"3.27 S cm^-1", r"\mathrm{CoO}_x"):
    v, d = latex_valid(s)
    print("  %-16r valid=%s %s" % (s[:16], v, d))

print("\n== 互转 ==")
print("  l2t:", repr(latex_to_text(r"$\mathrm{CoO}_x$")))
print("  t2l:", repr(text_to_latex("3.27 S cm⁻¹ (P-LIG)")))
print("  t2l greek:", repr(text_to_latex("γ-ray α/β")))
print("\nALL OK" if ok else "\nSOME FAILED")
