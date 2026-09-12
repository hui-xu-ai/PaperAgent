#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""项目规模盘点：模块数/行数/测试数/无用产物，支撑架构瘦身讨论"""
import os
from pathlib import Path

ROOT = Path(r"D:\Python\DeepSeek\PaperAgent")

def py_scope(root):
    files = list(root.rglob("*.py"))
    n = len(files)
    loc = sum(len(p.read_text(encoding="utf-8", errors="ignore").splitlines()) for p in files)
    return n, loc

print("=== 代码规模 ===")
for sub in ("packages/paperparse/paperparse", "backend/app", "tools"):
    p = ROOT / sub
    if p.exists():
        n, loc = py_scope(p)
        print("%-32s %4d 文件 %6d 行" % (sub, n, loc))

print("\n=== 测试 ===")
for sub in ("packages/paperparse/tests", "backend/tests"):
    p = ROOT / sub
    if p.exists():
        n = len(list(p.rglob("test_*.py")))
        loc = sum(len(f.read_text(encoding="utf-8", errors="ignore").splitlines()) for f in p.rglob("test_*.py"))
        print("%-32s %4d 文件 %6d 行" % (sub, n, loc))

print("\n=== paperparse 核心模块（core/）===")
core = ROOT / "packages/paperparse/paperparse/core"
if core.exists():
    for f in sorted(core.glob("*.py")):
        if f.name.startswith("__"): continue
        loc = len(f.read_text(encoding="utf-8", errors="ignore").splitlines())
        print("  %-34s %5d 行" % (f.name, loc))

print("\n=== backend services 模块 ===")
svc = ROOT / "backend/app/services"
if svc.exists():
    for f in sorted(svc.glob("*.py")):
        loc = len(f.read_text(encoding="utf-8", errors="ignore").splitlines())
        print("  %-34s %5d 行" % (f.name, loc))

print("\n=== work/ 下的实验目录（非代码，量大）===")
work = ROOT / "work"
if work.exists():
    dirs = [d for d in work.iterdir() if d.is_dir()]
    print("  work/ 目录数:", len(dirs))
    # 按前缀归类
    from collections import Counter
    pref = Counter()
    for d in dirs:
        name = d.name
        p = name.split("_")[0].split("-")[0].split("2")[0][:6]
        pref[p] += 1
    print("  常见前缀:", dict(pref.most_common(12)))
    tot = sum(sum(f.stat().st_size for f in d.rglob("*")) for d in dirs)
    print("  work/ 总大小: %.1f MB" % (tot/1e6))
