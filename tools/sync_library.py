#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同步 library 主产物为 p16-fix2（C/D 版 be35695）+ 验证"""
import shutil, re
from pathlib import Path

ROOT = Path(r"D:\Python\DeepSeek\PaperAgent")
JOBS = [("10.1002_adma.202407106", "p16-fix2-adma"),
        ("10.1016_j.cej.2025.167798", "p16-fix2-cej")]

for doi, fix in JOBS:
    src = ROOT / "work" / fix / doi
    dst = ROOT / "library" / doi
    for name in ("en.md", "document.json", "qa_report.json"):
        shutil.copy2(src / name, dst / name)
        print("同步", name, "->", doi)
    for sub in ("images", "work"):
        tgt = dst / sub
        for f in (src / sub).iterdir():
            if f.is_dir():
                t2 = tgt / f.name
                t2.mkdir(parents=True, exist_ok=True)
                for ff in f.iterdir():
                    shutil.copy2(ff, t2 / ff.name)
            else:
                shutil.copy2(f, tgt / f.name)
        print("同步", sub, "->", doi)

print("=== 顶层 .en.md（应为空）===")
tops = list((ROOT / "library").glob("*.en.md"))
print("顶层残留:", [p.name for p in tops] or "无")

print("=== 主产物验证 ===")
for doi, _ in JOBS:
    en = (ROOT / "library" / doi / "en.md").read_text(encoding="utf-8")
    sup = len(re.findall(r"<sup>\[\d", en))
    bad = {k: (k in en) for k in
           ("\\DeltaV", "\\ttBF", "{\\bfh}", "\\Nu", "{ - 1 }$(bare", "\ufffd")}
    print(doi, "| 上标引用:", sup, "| 残留:", {k: v for k, v in bad.items() if v} or "无")
