#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P16：从 mineru 官方 v4 API 重新拉取 adma 的 full.md（干净版对比）。
用途：核实 corpus/raw 里的 full.md 是否过期（含公式碎片/旧解析），
用当前官方 API 重拉一份作对照。"""
import sys
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/paperparse"))

from paperparse.config import load_config
from paperparse.core.mineru_client import MineruClient

PDF = "learning_workspace/corpus/pdfs/10.1002_adma.202407106.pdf"
OUT = "work/p16-fetch/adma_mineru_full.md"

load_config()
print("拉取 adma 官方 v4 full.md ...", flush=True)
mblocks = MineruClient(load_config()).extract_v4_batch(
    PDF, workdir="work/p16-fetch/cache")
src = Path(mblocks.raw_path)
print("官方 full.md raw_path =", src, flush=True)
shutil.copy2(src, OUT)
print("已复制到", OUT, flush=True)
print("MD5 =", __import__("hashlib").md5(Path(OUT).read_bytes()).hexdigest(), flush=True)
print("行数 =", Path(OUT).read_text(encoding="utf-8").count("\n") + 1, flush=True)
