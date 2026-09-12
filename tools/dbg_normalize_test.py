#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A 修复验证：normalize_formula_fragments 命令空格保护（真实用例）"""
from paperparse.core.latex_normalize import normalize_formula_fragments as nf

cases = [
    # (输入, 期望输出, 说明)
    (r"$\Delta V = \Phi - \varphi$", r"$\Delta V=\Phi-\varphi$", "DeltaV 缺空格修复"),
    (r"$\tt B F 4 ^ { - }$", r"$\tt BF4^{-}$", "ttBF4 修复"),
    (r"${\bf h}$", r"${\bf h}$", "bf h 修复（组内命令）"),
    (r"${ \bf h }$", r"${\bf h}$", "bf h 修复（组+空格）"),
    (r"$4 3 . 1 ^ { \circ } \ ( 2 \theta )$", r"$43.1^{\circ}\ (2\theta)$", "常规碎片不破坏"),
    (r"${\approx} 0 . 3 4$", r"${\approx}0.34$", "approx 常规"),
    (r"$\mathrm { C o O }$", r"$\mathrm{CoO}$", "mathrm 后跟 { 不补空格"),
    (r"$0 . 3 7$", r"$0.37$", "纯数字碎片"),
    (r"$\circ\mathrm { C }$", r"$\circ\mathrm{C}$", "命令后跟反斜杠不补"),
    (r"$\alpha\beta$", r"$\alpha\beta$", "连续命令"),
    (r"$40{-}140\ ^{\circ}\mathrm{C}$", r"$40{-}140\ ^{\circ}\mathrm{C}$", "控制空格保留"),
    (r"$\mathrm{0.98°s}$", r"$\mathrm{0.98°s}$", "已正常公式不动"),
    (r"$\scriptstyle\mathrm{Co}/\mathrm{P}$", r"$\scriptstyle\mathrm{Co}/\mathrm{P}$", "scriptstyle 后跟 { 不补"),
    (r"$\mathsf{BF}_{4}^{\mathrm{~-~}}$", r"$\mathsf{BF}_{4}^{\mathrm{~-~}}$", "mathsf/mathrm 正常"),
    (r"$\approx5.0,^{[55]}\approx5.2,^{[56-58]}$", r"$\approx5.0,^{[55]}\approx5.2,^{[56-58]}$", "approx 后跟数字补空格"),
]

fails = 0
for src, want, note in cases:
    got = nf(src)
    ok = got == want
    if not ok:
        fails += 1
    print(("OK  " if ok else "FAIL"), note)
    if not ok:
        print("    in :", repr(src))
        print("    got:", repr(got))
        print("    wan:", repr(want))
print("---- %d/%d pass" % (len(cases) - fails, len(cases)))
raise SystemExit(1 if fails else 0)
