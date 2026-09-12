#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 Library 规范化所需文件：
1) v1-mineru.md：纯 MinerU 解析拼接版（paddle=False，当前代码 M1-M6）
2) mineru_full.md 拷贝（原始 MinerU 输出）
3) source.pdf 拷贝
"""
import sys, shutil, time
from pathlib import Path
sys.path.insert(0, r"D:\Python\DeepSeek\PaperAgent\packages\paperparse")
from paperparse.core.p14_pipeline import process_pdf_v2

ROOT = Path(r"D:\Python\DeepSeek\PaperAgent")
JOBS = [
    {
        "doi": "10.1002_adma.202407106",
        "pdf": ROOT / r"用户提供的文献\PDF文献\10.1002_adma.202407106.pdf",
        "md": ROOT / r"learning_workspace\corpus\raw\10.1002_adma.202407106\full.md",
        "out": ROOT / r"work\p16-v1mineru-adma",
    },
    {
        "doi": "10.1016_j.cej.2025.167798",
        "pdf": ROOT / r"用户提供的文献\PDF文献\10.1016_j.cej.2025.167798.pdf",
        "md": ROOT / r"learning_workspace\corpus\raw\10.1016_j.cej.2025.167798\full.md",
        "out": ROOT / r"work\p16-v1mineru-cej",
    },
]

for j in JOBS:
    t0 = time.time()
    print("=== %s ===" % j["doi"], flush=True)
    res = process_pdf_v2(str(j["pdf"]), md_path=str(j["md"]),
                         out_dir=str(j["out"]), paddle=False, ai_review=False)
    print("  status=%s 耗时 %.1fs" % (res.get("status"), time.time() - t0), flush=True)
print("DONE")
