#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P11 增强验证：canonical 公式判定后 adma 差异重分类"""
import sys, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from pathlib import Path
from paperparse.core.dual_align import build_report
from paperparse.middleware.schema import ParserBlocks

out = Path(r"work\dual\10.1002_adma.202407106")
m_blocks = json.loads((out / "mineru_blocks.json").read_text(encoding="utf-8"))
p_blocks = json.loads((out / "paddleocr_blocks.json").read_text(encoding="utf-8"))
mn = ParserBlocks(source="mineru", pages=15, blocks=m_blocks)
pn = ParserBlocks(source="paddleocr", pages=15, blocks=p_blocks)
rep = build_report(mn, pn, pdf_name="10.1002_adma.202407106.pdf",
                   out_path=out / "dual_report_v2.json")
print(json.dumps(rep.stats, ensure_ascii=False))
print("--- format_diff 明细 ---")
for it in rep.items:
    if it.diff_type == "format_diff":
        print("[p%d] M: %s" % (it.page, it.mineru.get("text", "")[:90]))
        print("      P: %s" % it.paddleocr.get("text", "")[:90])
        print("      latex_valid=%s" % it.evidence.get("latex_valid"))
