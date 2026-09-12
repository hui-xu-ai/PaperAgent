# -*- coding: utf-8 -*-
"""已确认修复库（自 paperparse.core.anomaly_detect.KNOWN_FIXES 迁入）。

AI 判断后的确定性替换；新异常经确认后追加。
"""
KNOWN_FIXES: list[tuple[str, str, str]] = [
    (r"\$\\Nu\s*_\s*\{\s*2\s*\}\$", "$\\mathrm { N } _ { 2 }$",
     "\\Nu_2 为希腊字母 Nu 误识别，应为氮气 N2"),
    (r"\(\s*Φ\s*and\s*\uFFFD\s*", "(Φ and φ ",
     "� 应为 φ（与前文 ΔV = Φ − φ 对应）"),
    (r"poly\(\s*\uFFFD\s*-?\s*caprolactone", "poly($\\varepsilon$-caprolactone",
     "� 应为 ε（PCL = 聚 ε-己内酯）"),
    (r"\\mathsf\s*\{\s*A\s*\}\s*\\mathsf\s*\{\s*m\s*\}\s*\^\s*\{\s*\\bar\s*\{\s*2\s*\}\s*\}",
     "\\mathsf { A } \\mathsf { m } ^ { 2 }",
     "\\mathsf{A m}^{\\bar{2}} 应为 \\mathsf{A m}^{2}（单位 A·m²·kg⁻¹，OCR 误把 2 写成 \\bar{2}）"),
    (r"\\mathsf\s*\{\s*A\s*\}\s*\\mathsf\s*\{\s*m\s*\}\s*\^\s*\{\s*[\x00-\x1f]?ar\s*\{\s*2\s*\}\s*\}",
     "\\mathsf { A } \\mathsf { m } ^ { 2 }",
     "\\mathsf{A m}^{bar{2}} 中反斜杠缺失/被控制字符污染 → 应为 \\mathsf{A m}^{2}（A·m²·kg⁻¹）"),
]
