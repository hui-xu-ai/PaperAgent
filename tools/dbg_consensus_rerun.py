#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""共识错误词典层真实链路重跑：adma + cej 离线（复用 mineru/paddle 缓存）。
用法: python tools/dbg_consensus_rerun.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, r"D:\Python\DeepSeek\PaperAgent\packages\paperparse")
from paperparse.core.p14_pipeline import process_pdf_v2  # noqa: E402

JOBS = [
    {
        "name": "adma",
        "pdf": r"D:\Python\DeepSeek\PaperAgent\用户提供的文献\PDF文献\10.1002_adma.202407106.pdf",
        "md": r"D:\Python\DeepSeek\PaperAgent\work\mineru_cache\1a8c10c7-bd0d-42da-b1c5-78b685db7742\full.md",
        "paddle": r"D:\Python\DeepSeek\PaperAgent\backend\work\paddleocr_backup\20260826-085929\blocks.json",
        "out": r"D:\Python\DeepSeek\PaperAgent\work\consensus-fix-adma",
        "ref": r"D:\Python\DeepSeek\PaperAgent\work\p16-fix2-adma\10.1002_adma.202407106\en.md",
    },
    {
        "name": "cej",
        "pdf": r"D:\Python\DeepSeek\PaperAgent\用户提供的文献\PDF文献\10.1016_j.cej.2025.167798.pdf",
        "md": r"D:\Python\DeepSeek\PaperAgent\work\mineru_cache\992cad63-b812-48c4-a4c5-0ad592fd930f\full.md",
        "paddle": r"D:\Python\DeepSeek\PaperAgent\backend\work\paddleocr_backup\20260826-090517\blocks.json",
        "out": r"D:\Python\DeepSeek\PaperAgent\work\consensus-fix-cej",
        "ref": r"D:\Python\DeepSeek\PaperAgent\work\p16-fix2-cej\10.1016_j.cej.2025.167798\en.md",
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
            r = {"status": res.get("status"),
                 "stats": res.get("stats", {}),
                 "en_md": res.get("en_md")}
            md_path = res.get("en_md")
            if md_path:
                t = Path(md_path).read_text(encoding="utf-8")
                r["checks"] = {
                    "fffd": "\ufffd" in t,
                    "Nu": "\\Nu" in t,
                    "DeltaV": "\\DeltaV" in t,
                }
                # 共识错误残留扫描（缺下标带电荷形态）
                import re
                ion_missing = re.findall(
                    r"[A-Z][a-z]?(?:[A-Z][a-z]?)*[−⁻⁺]\b", t)
                r["ion_missing_suspicious"] = ion_missing[:10]
                # 与 p16-fix2 参考对比（词典层不应改错正确内容）
                if Path(j["ref"]).exists():
                    ref = Path(j["ref"]).read_text(encoding="utf-8")
                    dom = res.get("stats", {}).get("domain_fix", {})
                    applied = dom.get("applied_paras", 0)
                    r["vs_ref"] = {"applied_paras": applied,
                                   "fix_count": len(dom.get("fixes", [])),
                                   "ref_same_len": len(ref) == len(t)}
            report[j["name"]] = r
        except Exception as e:  # noqa: BLE001
            report[j["name"]] = {"status": "error", "error": str(e)[:400]}
        print("  耗时 %.1fs" % (time.time() - t0), flush=True)
    out = Path(r"D:\Python\DeepSeek\PaperAgent\work\consensus-fix-report.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("REPORT ->", out)
    for k, v in report.items():
        if v.get("status") == "error":
            print(k, "ERROR:", v.get("error"))
            continue
        dom = v.get("stats", {}).get("domain_fix", {})
        print(k, "| domain_fix:", dom.get("applied_paras"), "paras,",
              len(dom.get("fixes", [])), "fixes |",
              "fffd:", v.get("checks", {}).get("fffd"),
              "| suspicious:", v.get("ion_missing_suspicious"))


if __name__ == "__main__":
    main()
