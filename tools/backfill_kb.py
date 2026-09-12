# -*- coding: utf-8 -*-
"""kb 原文层四件全量补齐（KB-DESIGN v0.6 D10/D17 落地，历史文献一键补齐）。

1) source.pdf 归位：library/<DOI>/ 缺 source.pdf 时，从 用户提供的文献/PDF文献/<dir>.pdf
   复制进 library（数据归位，解析产物自包含）
2) 扫 library/ 全部 → sync_source_to_kb(force=False)（冻结：只补缺失，不覆盖 kb 已有）

用法：python tools/backfill_kb.py [--force]
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "paperkb"))

from paperkb.config import Roots  # noqa: E402
from paperkb.doi import dirname_to_doi  # noqa: E402
from paperkb.imports import sync_source_to_kb  # noqa: E402

PDF_SRC = ROOT / "用户提供的文献" / "PDF文献"


def main() -> int:
    ap = argparse.ArgumentParser(description="kb 原文层四件全量补齐")
    ap.add_argument("--force", action="store_true", help="强制覆盖 kb（重新同步）")
    args = ap.parse_args()

    roots = Roots(data_dir=ROOT / "data", library_dir=ROOT / "library",
                  kb_dir=ROOT / "knowledge_base").ensure()

    # 1) source.pdf 归位
    restored = 0
    if PDF_SRC.exists() and roots.library_dir.exists():
        for d in sorted(roots.library_dir.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            if not (d / "source.pdf").exists():
                src = PDF_SRC / f"{d.name}.pdf"
                if src.exists():
                    shutil.copy2(src, d / "source.pdf")
                    print(f"[pdf归位] {d.name} ← {src.name}")
                    restored += 1

    # 2) 全量 sync
    done = skipped = failed = 0
    for d in sorted(roots.library_dir.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        doi = dirname_to_doi(d.name)
        if not doi:
            continue
        try:
            r = sync_source_to_kb(doi, roots, force=args.force)
            if r["copied"]:
                v = (r.get("verify") or {}).get("ok")
                print(f"[补齐] {doi} +{','.join(r['copied'])} verify={v}")
                done += 1
            else:
                print(f"[已存在] {doi}（{','.join(r['skipped']) or '全部' } 冻结跳过）")
                skipped += 1
        except Exception as e:  # noqa: BLE001
            print(f"[失败] {doi}: {e}")
            failed += 1
    print(f"\n完成：pdf归位 {restored}，补齐 {done}，已存在 {skipped}，失败 {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
