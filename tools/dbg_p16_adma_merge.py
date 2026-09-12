#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P16 拼接回归诊断：跑 repair_md_paragraphs(adma)，dump 拼接点2 判定链。
用法: python tools/dbg_p16_adma_merge.py [out_dir]
"""
import sys, json, re
from pathlib import Path
from paperparse.core.skeleton_local import build_local_skeleton
from paperparse.core.md_align import align_md_to_skeleton, parse_md_paragraphs, norm_text
from paperparse.core.repair_paragraphs import repair_md_paragraphs

PDF = Path(r"D:\Python\DeepSeek\PaperAgent\用户提供的文献\PDF文献\10.1002_adma.202407106.pdf")
MD = Path(r"D:\Python\DeepSeek\PaperAgent\work\dual\10.1002_adma.202407106\mineru_full.md")
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else r"D:\Python\DeepSeek\PaperAgent\work\dbg_adma_merge")

def main():
    md_text = MD.read_text(encoding="utf-8")
    sk = build_local_skeleton(str(PDF))
    res = repair_md_paragraphs(md_text, sk)
    OUT.mkdir(parents=True, exist_ok=True)
    # 1) md 段序（前 14 段：idx + 前 70 字 + 尾 40 字）
    align = align_md_to_skeleton(md_text, sk)
    md_paras = align.md_paras
    lines = ["=== mineru md 段序（前 14 段）==="]
    for p in md_paras[:14]:
        t = (p.text or "").strip().replace("\n", " ")
        lines.append("md#%d [%s] %s...%s" % (p.idx, p.kind, t[:70], t[-40:]))
    # 2) repair audit（合并动作）
    lines.append("")
    lines.append("=== repair audit（合并动作）===")
    for a in res.audit:
        lines.append(json.dumps(a, ensure_ascii=False)[:300])
    lines.append("")
    lines.append("=== 最终段落（前 12 段）===")
    for r in res.paragraphs[:12]:
        t = (r.text or "").strip().replace("\n", " ")
        lines.append("RP[%s] %s...%s" % (r.para_id, t[:80], t[-60:]))
    # 3) 拼接点2 相关段定位
    lines.append("")
    lines.append("=== 拼接点2（efficient/method via）在最终段的归属 ===")
    for r in res.paragraphs:
        t = (r.text or "").strip()
        if "efficient" in t or "method via" in t or "Electro-ionic" in t or "Artificial muscles" in t:
            lines.append("%s [%s] md_idx=%s: ...%s" % (
                r.para_id, r.source, r.md_idx, t[:140]))
    (OUT / "diagnose.txt").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))

if __name__ == "__main__":
    main()
