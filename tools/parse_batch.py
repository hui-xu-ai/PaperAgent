#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量语料解析（P-ENHANCE R07）：遍历 corpus/pdfs/*.pdf，未解析的走云端 MinerU v4
（convert_pdf parser=mineru-v4），产物落 corpus/baseline/<名>/；归档 raw 产物
（content_list.json + full.md → corpus/raw/<名>/）；已解析的跳过。

用法:
    .venv\\Scripts\\python.exe tools/parse_batch.py [--force] [--only <pdf名>]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CORPUS = Path(__file__).resolve().parents[1] / "learning_workspace" / "corpus"


def parse_one(pdf: Path, force: bool = False) -> dict:
    name = pdf.stem
    baseline = CORPUS / "baseline" / name
    doc_json = baseline / "intermediate" / "document.json"
    if not force and doc_json.exists():
        return {"pdf": name, "status": "skip", "reason": "已解析"}
    from paperparse.api import convert_pdf
    t0 = time.time()
    result = convert_pdf(str(pdf), parser="mineru-v4",
                         out_dir=str(CORPUS / "baseline"), template="recognized")
    out = {"pdf": name, "status": result.status,
           "duration_sec": round(time.time() - t0, 1)}
    if result.status == "success":
        out["document_json"] = str(result.outputs.document_json)
        out["latex"] = result.stats.latex_count if result.stats else 0
        # 归档 raw 产物
        raw_dir = CORPUS / "raw" / name
        raw_dir.mkdir(parents=True, exist_ok=True)
        intermediate = Path(result.outputs.document_json).parent
        for src_name, dst_name in (("mineru_content_list.json", "content_list.json"),
                                   ("mineru_v1.md", "full.md")):
            src = intermediate / src_name
            if src.exists():
                shutil.copy2(src, raw_dir / dst_name)
        (raw_dir / "manifest.json").write_text(json.dumps(
            {"channel": "v4batch", "pdf": pdf.name, "name": name,
             "time": time.strftime("%Y-%m-%d %H:%M:%S")},
            ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        out["error"] = str(result.error)[:200]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="强制重跑已解析的")
    ap.add_argument("--only", help="只解析指定 PDF 名（如 10.1007_s40820-023-01133-2.pdf）")
    args = ap.parse_args()

    pdfs = sorted((CORPUS / "pdfs").glob("*.pdf"))
    if args.only:
        pdfs = [p for p in pdfs if p.name == args.only]
    results = []
    for pdf in pdfs:
        print("==> %s" % pdf.name, flush=True)
        try:
            r = parse_one(pdf, force=args.force)
        except Exception as e:
            r = {"pdf": pdf.stem, "status": "error", "error": str(e)[:200]}
        results.append(r)
        print(json.dumps(r, ensure_ascii=False), flush=True)
    ok = sum(1 for r in results if r.get("status") == "success")
    print("汇总: %d 成功 / %d 跳过 / %d 失败" % (
        ok, sum(1 for r in results if r.get("status") == "skip"),
        sum(1 for r in results if r.get("status") not in ("success", "skip"))))
    return 0 if ok or all(r.get("status") == "skip" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
