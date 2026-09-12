#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P11-1 实测：PaddleOCR-VL-1.6 解析 adma 基线 PDF → 确认 JSONL 结构/配额/耗时"""
import sys, json, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from pathlib import Path
from paperparse.config import load_config
from paperparse.core.paddleocr_client import PaddleOCRClient

PDF = Path(r"learning_workspace\corpus\pdfs\10.1002_adma.202407106.pdf")

def main():
    cfg = load_config()
    print("token_len:", len(cfg.paddleocr_access_token), "model:", cfg.paddleocr_model_version)
    client = PaddleOCRClient(cfg)
    ok, msg = client.check_connectivity()
    print("connectivity:", ok, msg)

    t0 = time.time()
    blocks = client.parse_pdf(PDF)
    t1 = time.time()
    print("elapsed_sec: %.1f" % (t1 - t0))
    print("pages:", blocks.pages, "blocks:", len(blocks.blocks), "raw_path:", blocks.raw_path)
    print("raw_content_list:", (blocks.raw_content_list or "")[:120])
    # 结构摘要
    kinds = {}
    for b in blocks.blocks:
        kinds[b.kind] = kinds.get(b.kind, 0) + 1
    print("kinds:", kinds)
    for b in blocks.blocks[:5]:
        print("  sample:", b.block_id, "p%d" % b.page, b.kind, repr(b.text[:80]), b.bbox)
    # JSONL 原始结构探测
    raw = Path(blocks.raw_path)
    if raw.exists():
        lines = raw.read_text(encoding="utf-8").strip().split("\n")
        print("jsonl_lines:", len(lines))
        first = json.loads(lines[0])
        print("page0_keys:", list(first.keys())[:20])
        for key in ("layoutParsingResults", "items", "markdown_text"):
            v = first.get(key)
            if isinstance(v, list):
                print("  %s: list len=%d, item0 keys=%s" % (key, len(v), list(v[0].keys())[:15] if v and isinstance(v[0], dict) else "?"))
            elif isinstance(v, str):
                print("  %s: str len=%d" % (key, len(v)))
        if isinstance(first.get("markdown"), dict):
            print("  markdown keys:", list(first["markdown"].keys())[:15])

if __name__ == "__main__":
    main()
