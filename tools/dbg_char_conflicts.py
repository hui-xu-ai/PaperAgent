#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E 修复验证：0.98°s 场景 char_conflicts 整块公式处理（用 8:59 真实段落文本）"""
import sys
sys.path.insert(0, r"D:\Python\DeepSeek\PaperAgent\packages\paperparse")
from paperparse.core.p14_pipeline import (char_conflicts, _apply_arbitrations,
                                          _wrap_paddle_formulas)
from paperparse.core.dual_ai_review import Arbitration

M = ("The deformation charge density further reveals the significant electron transfer, "
     "suggesting a potent interaction as evidenced by the absorption of IL ions (Figure 4d). "
     "This is similarly manifested in a substantial augmentation of adsorption energies, "
     "specifically, an elevation from \u22121.73 to \u22122.74 eV marking a 58.3% enhancement for "
     "EMIM<sup>+</sup>, and from \u22120.72 to \u22121.84 eV (155.5%) for $\\mathrm{BF_{4}}^{-}$ "
     "(Figure 4e), which supports the distinguished ability to capture IL for high possibility "
     "of contacting with more active sites (Figure S16a, Supporting Information) that the IL "
     "droplet swiftly dispersed and absorbed with a considerate change rate of dynamic contact "
     "angle of $2.44^{\\circ}\\mathbf{s}^{-1}$ . As the doping within LIG, the change rates also "
     "significantly increase from $0 . 9 8 ^ { \\circ } \\mathbf { s } ^ { - 1 }$ (bare LIG), to "
     "$1 . 9 2 ^ { \\circ } \\mathrm { s ^ { - 1 } }$ (P-LIG) and $1 . 4 8 ^ { \\circ } \\mathbf { S } ^ { - 1 }$ (Co-LIG).")

P = ("The deformation charge density further reveals the significant electron transfer, "
     "suggesting a potent interaction as evidenced by the absorption of IL ions (Figure 4d). "
     "This is similarly manifested in a substantial augmentation of adsorption energies, "
     "specifically, an elevation from \u22121.73 to \u22122.74 eV marking a 58.3% enhancement for "
     "EMIM<sup>+</sup>, and from \u22120.72 to \u22121.84 eV (155.5%) for $\\mathrm{BF_{4}}^{-}$ "
     "(Figure 4e), which supports the distinguished ability to capture IL for high possibility "
     "of contacting with more active sites (Figure S16a, Supporting Information) that the IL "
     "droplet swiftly dispersed and absorbed with a considerate change rate of dynamic contact "
     "angle of 2.44°s $ ^{-1} $ . As the doping within LIG, the change rates also "
     "significantly increase from 0.98°s $ ^{-1} $ (bare LIG), to 1.92°s $ ^{-1} $ (P-LIG) "
     "and 1.48°s $ ^{-1} $ (Co-LIG).")

cf = char_conflicts(M, P, page=7)
print("=== 冲突项（应含 0.98/1.92 整块公式，而非半截）===")
for c in cf:
    m = c["mineru"]["text"]
    p = c["paddleocr"]["text"]
    if "0.98" in m + p or "1.92" in m + p or "2.44" in m + p:
        print("M:", repr(m))
        print("P:", repr(p))
        print("---")
print("总冲突数:", len(cf))

# opcode 级诊断
import difflib
from paperparse.core.p14_pipeline import _mask_formulas_keep_len
mm = _mask_formulas_keep_len(M, "\u0001")
pp = _mask_formulas_keep_len(P, "\u0002")
sm = difflib.SequenceMatcher(None, mm, pp)
print("=== 0.98 区域 opcode ===")
for tag, i1, i2, j1, j2 in sm.get_opcodes():
    if "0.98" in M[i1:i2] + P[j1:j2] or "1.92" in M[i1:i2] + P[j1:j2]:
        print(tag, (i1, i2), (j1, j2), "M=", repr(M[i1:i2])[:60], "P=", repr(P[j1:j2])[:60])

# 模拟仲裁：百度为准 → 应用
arb = []
for i, c in enumerate(cf):
    if "0.98" in (c["mineru"]["text"] + c["paddleocr"]["text"]) \
            or "1.92" in (c["mineru"]["text"] + c["paddleocr"]["text"]):
        arb.append(Arbitration(id=len(arb), diff_type="text_conflict", page=7,
                               verdict="paddleocr", reason="百度OCR为准",
                               confidence=1.0, mineru=c["mineru"],
                               paddleocr=c["paddleocr"]))
    else:
        arb.append(Arbitration(id=len(arb), diff_type="text_conflict", page=7,
                               verdict="mineru", reason="百度缺内容",
                               confidence=1.0, mineru=c["mineru"],
                               paddleocr=c["paddleocr"]))
final, audit = _apply_arbitrations(M, cf, arb)
# 模拟真实管线：build_markdown 对 body 段再 normalize（0.98 大块保护后保留 mineru → normalize 修复）
from paperparse.core.latex_normalize import normalize_formula_fragments as nf
final = nf(final)
i = final.find("significantly increase from")
print("=== 仲裁+normalize 后 0.98 段落片段 ===")
print(final[i:i + 130])
print("=== 残留检查（真残留特征：$ 外孤立 { / 半截 -1}} / 双 $$）===")
for bad in ("{ - 1 }$(bare", "to$$", "-1}}", "°s$ { - 1"):
    if bad in final:
        k = final.find(bad)
        print("残留:", repr(bad), "ctx:", repr(final[max(0, k - 40):k + 40]))
print("done")
