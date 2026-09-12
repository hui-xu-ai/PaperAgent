#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
sys.path.insert(0, r"D:\Python\DeepSeek\PaperAgent\packages\paperparse")
from paperparse.core.p14_pipeline import _is_paddle_authoritative, _formula_equivalent, _strip_math

cases = [
    # (mineru, paddle, 期望_百度准, 期望_等价, 说明)
    (r"$\mathrm{BF}_{4}^{-}$", "BF⁻", False, False, "BF丢4（百度漏识别）→ 不替换进复核"),
    (r"$\mathrm{BF}_{4}^{-}$", "BF₄⁻", False, True, "BF4 等价 → both 保留 mineru"),
    (r"$100^{\circ}\mathrm{C}$", "100°C", False, True, "单位等价 → both 保留 mineru"),
    (r"$9.80\,\mathrm{A\,m}^{2}\mathrm{kg}^{-1}$", "9.80 Am² kg⁻¹", False, True, "单位等价2"),
    ("Eficient", "Efficient", True, None, "拼写 → 百度准"),
    ("ab", "abx", True, None, "断词 insert → 百度准"),
    ("C4", "C", False, None, "数字丢失 → 复核"),
    ("100 C", "100 °C", True, None, "空格+单位符号 → 百度准（纯空白差异）"),
    ("(HF)", ")240", False, None, "百度垃圾 → 复核"),
]
fails = 0
for m, p, want_auth, want_eq, note in cases:
    c = {"mineru": {"text": m}, "paddleocr": {"text": p}}
    auth = _is_paddle_authoritative(c)
    eq = _formula_equivalent(c) if want_eq is not None else None
    ok = (auth == want_auth) and (eq is None or eq == want_eq)
    if not ok:
        fails += 1
    print(("OK  " if ok else "FAIL"), note, "| 百度准=%s 等价=%s" % (auth, eq))
    if not ok:
        print("     m=%r p=%r" % (m, p))
print("---- %d/%d" % (len(cases) - fails, len(cases)))
raise SystemExit(1 if fails else 0)
