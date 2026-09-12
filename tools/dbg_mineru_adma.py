#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P11-2 准备：mineru-v4 解析 adma → ParserBlocks + content_list.json 落盘（供双通道对齐）"""
import sys, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from pathlib import Path
from paperparse.config import load_config
from paperparse.core.mineru_client import MineruClient

PDF = Path(r"learning_workspace\corpus\pdfs\10.1002_adma.202407106.pdf")
OUT = Path(r"work/dual/adma_mineru")

def main():
    cfg = load_config()
    print("mineru_key_len:", len(cfg.mineru_api_key))
    client = MineruClient(cfg)
    t0 = time.time()
    blocks = client.extract_v4_batch(PDF, workdir="work/mineru_cache")
    t1 = time.time()
    print("elapsed_sec: %.1f" % (t1 - t0))
    print("pages:", blocks.pages, "blocks:", len(blocks.blocks))
    print("raw_path:", blocks.raw_path)
    print("raw_content_list:", blocks.raw_content_list)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "blocks.json").write_text(
        "[%s]" % ",\n".join(b.model_dump_json() for b in blocks.blocks), encoding="utf-8")
    if blocks.raw_content_list and Path(blocks.raw_content_list).exists():
        import shutil
        shutil.copy2(blocks.raw_content_list, OUT / "content_list.json")
        print("content_list saved ->", OUT / "content_list.json")
    if blocks.raw_path and Path(blocks.raw_path).exists():
        import shutil
        shutil.copy2(blocks.raw_path, OUT / "mineru_full.md")
    print("DONE")

if __name__ == "__main__":
    main()
