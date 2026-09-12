#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P16 四项修复（A/B/C/E）真实链路重跑验证：adma + cej 离线（复用 8:59 paddle 缓存）。
用法: python tools/dbg_p16_fix_rerun.py
"""
import sys, json, time
sys.path.insert(0, r"D:\Python\DeepSeek\PaperAgent\packages\paperparse")
from paperparse.core.p14_pipeline import process_pdf_v2

JOBS = [
    {
        "name": "adma",
        "pdf": r"D:\Python\DeepSeek\PaperAgent\用户提供的文献\PDF文献\10.1002_adma.202407106.pdf",
        "md": r"D:\Python\DeepSeek\PaperAgent\work\dual\10.1002_adma.202407106\mineru_full.md",
        "paddle": r"D:\Python\DeepSeek\PaperAgent\backend\work\paddleocr_backup\20260826-085929\blocks.json",
        "out": r"D:\Python\DeepSeek\PaperAgent\work\p16-fix2-adma",
    },
    {
        "name": "cej",
        "pdf": r"D:\Python\DeepSeek\PaperAgent\用户提供的文献\PDF文献\10.1016_j.cej.2025.167798.pdf",
        "md": r"D:\Python\DeepSeek\PaperAgent\work\dual\10.1016_j.cej.2025.167798\mineru_full.md",
        "paddle": r"D:\Python\DeepSeek\PaperAgent\backend\work\paddleocr_backup\20260826-090517\blocks.json",
        "out": r"D:\Python\DeepSeek\PaperAgent\work\p16-fix2-cej",
    },
]

def main():
    report = {}
    for j in JOBS:
        t0 = time.time()
        print("=== run %s ===" % j["name"], flush=True)
        try:
            res = process_pdf_v2(
                j["pdf"], md_path=j["md"], out_dir=j["out"],
                paddle_blocks_path=j["paddle"], ai_review=False)
            report[j["name"]] = {
                "status": res.get("status"),
                "stats": res.get("stats", {}),
                "en_md": res.get("en_md"),
                "document_json": res.get("document_json"),
            }
            # 关键残留扫描
            md_path = res.get("en_md")
            if md_path:
                t = Path(md_path).read_text(encoding="utf-8")
                checks = {
                    "DeltaV": "\\DeltaV" in t,
                    "ttBF4": "\\ttBF" in t,
                    "bfh": "{\\bfh}" in t,
                    "Nu2": "\\Nu" in t,
                    "deg_s_frag": "{ - 1 }$(bare" in t or "to$$" in t,
                    "fffd": "\ufffd" in t,
                }
                report[j["name"]]["residual"] = {k: v for k, v in checks.items() if v}
                report[j["name"]]["checked"] = checks
            # 拼接点
            if j["name"] == "adma" and md_path:
                t = Path(md_path).read_text(encoding="utf-8")
                report[j["name"]]["merge_p2"] = "efficient method via" in t.replace("\n", " ")
                report[j["name"]]["merge_p1"] = "applications. Electro-ionic soft actuators" in t.replace("\n", " ")
        except Exception as e:  # noqa: BLE001
            report[j["name"]] = {"status": "error", "error": str(e)[:300]}
        print("  耗时 %.1fs" % (time.time() - t0), flush=True)
    out = Path(r"D:\Python\DeepSeek\PaperAgent\work\p16-fix-report.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("REPORT ->", out)
    print(json.dumps({k: {"status": v.get("status"),
                          "stats_keys": list((v.get("stats") or {}).keys())[:6],
                          "residual": v.get("residual", {}) if v.get("status") != "error" else v.get("error")}
                     for k, v in report.items()}, ensure_ascii=False, indent=1))

if __name__ == "__main__":
    from pathlib import Path
    main()




