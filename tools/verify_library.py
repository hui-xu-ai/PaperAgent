#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json
from pathlib import Path

LIB = Path(r"D:\Python\DeepSeek\PaperAgent\library")
for doi in ("10.1002_adma.202407106", "10.1016_j.cej.2025.167798"):
    d = LIB / doi
    en = (d / "en.md").read_text(encoding="utf-8")
    doc = json.loads((d / "document.json").read_text(encoding="utf-8"))
    paras = doc.get("paragraphs") or []
    t = "\n".join(p.get("text_en") or "" for p in paras)
    residual = {k: (k in en) for k in
                ("\\DeltaV", "\\ttBF", "{\\bfh}", "\\Nu", "{ - 1 }$(bare", "\ufffd")}
    print(doi)
    print("  en.md 残留:", {k: v for k, v in residual.items() if v} or "无")
    print("  document.json 段数:", len(paras), "| 残留:",
          {k: (k in t) for k in ("\\DeltaV", "\\ttBF", "{\\bfh}", "\\Nu", "\ufffd")})
    print("  versions.md 存在:", (d / "versions.md").exists(),
          "| v1-mineru.md:", (d / "v1-mineru.md").exists(),
          "| source.pdf:", (d / "source.pdf").exists())
