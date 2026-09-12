#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""冻结"黄金数据夹具"（见 `docs/VERSIONING.md` §8.1）。

为什么不直接用真实 `data/`：夹具要能进版本库、要在 CI 里永远可复现，所以只保留
**schema + 每表少量行**（极小），代表"该数据格式长什么样"。每次发布前跑一次，
冻结当版格式 → 下个版本的升级测试就有基准。

用法: python tools/freeze_data_fixture.py [--name data-v1] [--rows 3]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages" / "paperkb"))
sys.path.insert(0, str(ROOT / "backend"))

DBS = ("system/app.db", "chat/chat.db", "biblio/biblio.db", "reference/journals.db")


def freeze(src_db: Path, dst_db: Path, rows: int) -> dict:
    dst_db.parent.mkdir(parents=True, exist_ok=True)
    if src_db.exists():
        shutil.copy2(src_db, dst_db)
    else:
        sqlite3.connect(dst_db).close()
    con = sqlite3.connect(dst_db)
    kept = {}
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_data' "
            "AND name NOT LIKE '%_idx' AND name NOT LIKE '%_content' "
            "AND name NOT LIKE '%_docsize' AND name NOT LIKE '%_config'")]
        for t in tables:
            try:
                n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            except sqlite3.Error:
                continue
            if n > rows:
                try:
                    con.execute(f'DELETE FROM "{t}" WHERE rowid NOT IN '
                                f'(SELECT rowid FROM "{t}" LIMIT {rows})')
                except sqlite3.Error:
                    pass            # 无 rowid 的表（虚拟表）保留原样
            kept[t] = min(n, rows)
        con.commit()
        con.execute("VACUUM")
    finally:
        con.close()
    return kept


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="data-v1")
    ap.add_argument("--rows", type=int, default=3)
    args = ap.parse_args()

    from app.services.container import _build_roots
    from paperkb.manifest import read_manifest

    roots = _build_roots()
    out = ROOT / "tests" / "fixtures" / args.name
    if out.exists():
        shutil.rmtree(out)
    (out / "data").mkdir(parents=True, exist_ok=True)
    mapping = {"system/app.db": roots.system_db, "chat/chat.db": roots.chat_db,
               "biblio/biblio.db": roots.biblio_db, "reference/journals.db": roots.reference_db}
    total = 0
    for rel, src in mapping.items():
        kept = freeze(Path(src), out / "data" / rel, args.rows)
        size = (out / "data" / rel).stat().st_size
        total += size
        print(f"  {rel:22s} {size/1024:7.1f} KB  表 {len(kept)} 个（每表 ≤{args.rows} 行）")
    man = read_manifest(roots) or {"data_format": 1, "layout": "v1"}
    man = {k: man.get(k) for k in ("layout", "data_format", "created_at")}
    man["fixture"] = True
    man["note"] = f"冻结自 {args.name}（每表 ≤{args.rows} 行；供升级兼容测试）"
    (out / "data" / "manifest.json").write_text(
        json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
    # ⚠️ **必须脱敏**（2026-09-12 发布前审计实证：直接拷贝真实 DB ⇒ 夹具里带着
    # 三个真实 API Key、用户真实提问/回答、文献标题与本机绝对路径，且随仓库公开）。
    from sanitize_fixtures import sanitize_db

    for rel in mapping:
        changes = sanitize_db(out / "data" / rel, dry=False)
        n = len([c for c in changes if " → " in c])
        if n:
            print(f"  [脱敏] {rel:22s} {n} 处（密钥/路径/用户文本 → 占位）")
    (out / "README.md").write_text(
        f"# 黄金数据夹具 {args.name}\n\n"
        f"冻结时间：{man.get('created_at')} · data_format={man.get('data_format')} · "
        f"layout={man.get('layout')}\n\n"
        "用途：`tools/check_data_compat.py --fixtures-only` 与 "
        "`backend/tests/test_version_contract.py` 用它验证"
        "「新代码能否打开并迁移旧格式数据」。**不要手工改这里的内容**——"
        "它是历史格式的快照。\n\n"
        "> 已由 `tools/sanitize_fixtures.py` **脱敏**：API Key / 本机绝对路径 / 用户生成文本"
        "（对话、笔记、标题）一律替换为占位值；schema、表结构与行数不变。\n", encoding="utf-8")
    print(f"\n夹具: {out.relative_to(ROOT)}  合计 {total/1024:.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
