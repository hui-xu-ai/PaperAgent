#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Library 规范化组装：每篇文件夹自包含（source.pdf/mineru_full.md/v1-mineru.md/
主产物 en.md+document.json/qa_report/work/images/versions.md），删除顶层重复 .en.md。
用法: python tools/assemble_library.py
"""
import shutil
from pathlib import Path

ROOT = Path(r"D:\Python\DeepSeek\PaperAgent")
LIB = ROOT / "library"
PDFS = ROOT / "用户提供的文献" / "PDF文献"
RAW = ROOT / "learning_workspace" / "corpus" / "raw"
FIX = ROOT / "work"          # p16-fix-<x>（双通道修复版）、p16-v1mineru-<x>（MinerU 拼接版）

JOBS = [
    {"doi": "10.1002_adma.202407106", "fix": "p16-fix-adma", "v1": "p16-v1mineru-adma"},
    {"doi": "10.1016_j.cej.2025.167798", "fix": "p16-fix-cej", "v1": "p16-v1mineru-cej"},
]

def cp(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    shutil.copy2(src, dst)
    print("  copy %s -> %s" % (src.name, dst.relative_to(ROOT)))

def rmtree_merge(src, dst):
    if not src.exists():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for f in src.iterdir():
        t = dst / f.name
        if f.is_dir():
            rmtree_merge(f, t)
        else:
            if t.exists():
                t.unlink()
            shutil.copy2(f, t)
    print("  merge %s -> %s" % (src.name, dst.relative_to(ROOT)))

for j in JOBS:
    doi = j["doi"]
    d = LIB / doi
    print("=== %s ===" % doi)
    fix_out = FIX / j["fix"] / doi
    v1_out = FIX / j["v1"] / doi
    # 1) 源 PDF
    cp(PDFS / (doi + ".pdf"), d / "source.pdf")
    # 2) MinerU 原始
    cp(RAW / doi / "full.md", d / "mineru_full.md")
    # 3) MinerU 解析拼接版
    cp(v1_out / "en.md", d / "v1-mineru.md")
    # 4) 双通道+AI 审核版（消费方主产物）
    cp(fix_out / "en.md", d / "en.md")
    cp(fix_out / "document.json", d / "document.json")
    cp(fix_out / "qa_report.json", d / "qa_report.json")
    # 5) images
    rmtree_merge(fix_out / "images", d / "images")
    # 6) work 中间产物
    rmtree_merge(fix_out / "work", d / "work")
    # 7) versions.md
    (d / "versions.md").write_text(
        "# %s 版本记录\n\n"
        "| 版本 | 时间 | 说明 |\n"
        "|---|---|---|\n"
        "| v2-dual | 2026-08-26 | 双通道+AI 审核（d46440c 修复版）：拼接/公式残留全清 |\n"
        "| v1-mineru | 2026-08-26 | MinerU 解析拼接（paddle=False 重跑，当前代码 M1-M6） |\n\n"
        "## 源文件\n"
        "- MinerU 原始：`mineru_full.md`（corpus/raw 缓存）\n"
        "- 百度 OCR：`backend/work/paddleocr_backup/20260826-*`（blocks.json）\n"
        "## 说明\n"
        "- `en.md`/`document.json` = 双通道+AI 审核版（GUI/翻译/导出消费方依赖，文件名固定）\n"
        "- `v1-mineru.md` = 纯 MinerU 拼接版（无百度通道）\n"
        "- 旧备份留存 `work/backup_*`（未并入）\n" % doi,
        encoding="utf-8")

# 删除顶层重复 .en.md
print("=== 清理顶层重复 .en.md ===")
for f in LIB.glob("*.en.md"):
    f.unlink()
    print("  delete", f.relative_to(ROOT))
print("DONE")
