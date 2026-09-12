#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据文件系统盘点（只读）：目录/文件分类 + 体积 + SQLite 表与行数。

用法: python tools/dbg_data_inventory.py [--root .] [--out work/scratch/data-inventory.md]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT_DEFAULT = Path(__file__).resolve().parent.parent

# 顶层目录分类（人工核定；脚本只统计，不改动）
KIND = {
    "knowledge_base": "知识库（含系统库 .system / 回收站 .trash / _global / 卡片）",
    "library": "解析库（解析产物 + 译文变体 + source.pdf）",
    "input": "导入暂存（上传的 PDF 副本）",
    "attachments": "无父资源附件根",
    "work": "中间产物 / 测试残留（可清理）",
    "logs": "运行日志",
    "data": "（废弃？历史）",
    "frontend": "源码",
    "backend": "源码",
    "packages": "源码",
    "tools": "源码/脚本",
    "archive": "归档（已剥离模块）",
    "rules": "外部规则根（learned/user）",
    "docs": "文档",
    "output": "旧输出目录（历史）",
    "用户提供的文献": "用户原始资料（只读）",
    "feedback": "反馈草稿",
    ".dsh-memory": "记忆系统（独立 git）",
    ".venv": "虚拟环境",
    ".git": "版本库",
}


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}GB"


def scan_tree(root: Path, depth: int = 1) -> list[dict]:
    rows = []
    for p in sorted(root.iterdir()):
        if not p.is_dir():
            rows.append({"path": p.name, "dir": False, "files": 1,
                         "bytes": p.stat().st_size, "kind": KIND.get(p.name, "根级文件")})
            continue
        n = 0
        b = 0
        for f in p.rglob("*"):
            if f.is_file():
                n += 1
                try:
                    b += f.stat().st_size
                except OSError:
                    pass
        rows.append({"path": p.name, "dir": True, "files": n, "bytes": b,
                     "kind": KIND.get(p.name, "未分类")})
    return rows


def subdirs(root: Path, name: str, limit: int = 12) -> list[dict]:
    """某顶层目录下的一级子目录（每篇资源一个目录）"""
    base = root / name
    if not base.is_dir():
        return []
    out = []
    for p in sorted(base.iterdir()):
        if not p.is_dir():
            out.append({"path": p.name + "/", "files": 1,
                        "bytes": p.stat().st_size})
            continue
        n = b = 0
        exts: dict[str, int] = defaultdict(int)
        for f in p.rglob("*"):
            if f.is_file():
                n += 1
                exts[f.suffix.lower() or "(无后缀)"] += 1
                try:
                    b += f.stat().st_size
                except OSError:
                    pass
        out.append({"path": p.name + "/", "files": n, "bytes": b,
                    "exts": dict(sorted(exts.items(), key=lambda kv: -kv[1])[:6])})
    out.sort(key=lambda r: -r["bytes"])
    return out[:limit]


def db_tables(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        names = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        for t in names:
            try:
                n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            except sqlite3.Error:
                n = -1
            out.append({"table": t, "rows": n})
    finally:
        con.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(ROOT_DEFAULT))
    ap.add_argument("--out", default="work/scratch/data-inventory.md")
    args = ap.parse_args()
    root = Path(args.root).resolve()

    top = scan_tree(root)
    kb = subdirs(root, "knowledge_base", 20)
    lib = subdirs(root, "library", 20)
    inp = subdirs(root, "input", 20)
    work = subdirs(root, "work", 12)
    dbs = {
        "data/system/app.db": db_tables(
            root / "data" / "system" / "app.db"),
        "data/reference/journals.db": db_tables(
            root / "data" / "reference" / "journals.db"),
    }
    rep = {"root": str(root), "top": top, "kb_sub": kb, "lib_sub": lib,
           "input_sub": inp, "work_sub": work, "dbs": dbs}
    out = root / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")

    total = sum(r["bytes"] for r in top)
    print(f"根目录: {root}  合计 {human(total)}")
    print(f"{'目录':26s} {'文件数':>7s} {'体积':>9s}  说明")
    for r in sorted(top, key=lambda r: -r["bytes"]):
        print(f"{r['path'][:26]:26s} {r['files']:7d} {human(r['bytes']):>9s}  {r['kind']}")
    for label, rows in (("knowledge_base/", kb), ("library/", lib), ("input/", inp),
                        ("work/", work)):
        print(f"\n[{label}] 一级子项（按体积）")
        for r in rows:
            extra = (" " + json.dumps(r.get("exts"), ensure_ascii=False)) if r.get("exts") else ""
            print(f"  {r['path'][:44]:44s} {r['files']:6d} {human(r['bytes']):>9s}{extra}")
    for name, rows in dbs.items():
        print(f"\n[{name}]")
        for r in rows:
            print(f"  {r['table'][:30]:30s} {r['rows']:>8d}")
    print(f"\n报告: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
