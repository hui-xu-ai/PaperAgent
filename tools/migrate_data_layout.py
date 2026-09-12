#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""旧单库 → 新五库布局迁移（2026-09-12 用户拍板 · 工业级数据布局）。

旧：`knowledge_base/.system/paperagent.db`（应用数据 + 文献元数据 + 会话全挤一起）
新：`data/system/app.db`（设置/Key/用量/插件/任务）
    `data/chat/chat.db`（会话/消息/答案缓存）
    `data/biblio/biblio.db`（papers + kb_edited + papers_meta/identifiers/compile_*/FTS）
    `data/reference/journals.db`（JCR/CAS）
    `data/diary/diary.db`（日记，当前为文件实现，库占位）

做法：先让应用建好新库 schema（`Store` + `paperkb.init_kb`），再用 ATTACH 逐表
`INSERT OR REPLACE` 搬数据（取列交集），最后逐表核对行数。**默认 dry-run**。

用法：
  python tools/migrate_data_layout.py                # 只报告计划
  python tools/migrate_data_layout.py --apply        # 真正执行
  python tools/migrate_data_layout.py --apply --keep-old   # 保留旧库（默认改名备份）
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages" / "paperkb"))
sys.path.insert(0, str(ROOT / "backend"))

OLD = ROOT / "knowledge_base" / ".system" / "paperagent.db"

# 表 → 目标库（"main" = 系统库 app.db 自身）
ROUTE: dict[str, str] = {
    # 系统数据
    "tasks": "main", "llm_usage": "main", "settings": "main", "plugins": "main",
    # 会话
    "sessions": "chat", "messages": "chat", "answer_cache": "chat",
    # 文献元数据 / 文献库记录
    "papers": "biblio", "kb_edited": "biblio",
    "papers_meta": "biblio", "identifiers": "biblio", "citations": "biblio",
    "doi_md5_map": "biblio", "compile_jobs": "biblio", "compile_ctx": "biblio",
    "concepts": "biblio", "kb_trash": "biblio",
    "meta_fts": "biblio", "notes_fts": "biblio", "fulltext_fts": "biblio",
}


def _cols(conn, schema: str, table: str) -> list[str]:
    q = f"PRAGMA {schema}.table_info({table})" if schema else f"PRAGMA table_info({table})"
    return [r[1] for r in conn.execute(q)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--old", default=str(OLD))
    ap.add_argument("--keep-old", action="store_true")
    args = ap.parse_args()

    old = Path(args.old)
    if not old.exists():
        print(f"旧库不存在，无需迁移：{old}")
        return 0

    from app.services.container import _build_roots       # noqa: E402
    from app.services.store import Store                  # noqa: E402

    roots = _build_roots().ensure()
    print("新布局：")
    for name, p in (("system", roots.system_db), ("chat", roots.chat_db),
                    ("diary", roots.diary_db), ("biblio", roots.biblio_db),
                    ("reference", roots.reference_db)):
        print(f"  {name:9s} {p.relative_to(ROOT)}")
    if not args.apply:
        print("\n[dry-run] 未做任何写入。加 --apply 执行。")
        return 0

    # 1) 建好新库 schema（后端 Store 建 papers/tasks/sessions/…；paperkb 建元数据表）
    # ⚠️ 主库路径**必须取 roots.system_db**（单一来源）：早期版本用 `Settings().db_path`，
    #    而它会被 .env 的 PAPERAGENT_DB 覆盖 ⇒ 数据被写进另一个库、随后被清理
    #    （2026-09-12 实测：4 个 settings 键就是这样丢的，已用校验步骤兜住）。
    Store(str(roots.system_db), chat_db=roots.chat_db, biblio_db=roots.biblio_db)
    from paperkb import api as kbapi
    kbapi.init_kb(roots)
    print("\n新库 schema 已就绪")

    # 2) 逐表搬运（ATTACH 旧库 + 两个附加新库；只搬两库共有的列）
    con = sqlite3.connect(str(roots.system_db))
    con.execute("ATTACH DATABASE ? AS old", (str(old),))
    con.execute("ATTACH DATABASE ? AS chat", (str(roots.chat_db),))
    con.execute("ATTACH DATABASE ? AS biblio", (str(roots.biblio_db),))
    conv = {}
    problems: list[str] = []
    for t, where in ROUTE.items():
        target_schema = "" if where == "main" else where
        try:
            old_cols = _cols(con, "old", t)
        except sqlite3.Error:
            print(f"  - {t:14s} 旧库无此表，跳过")
            continue
        if not old_cols:
            continue
        try:
            new_cols = _cols(con, target_schema, t)
        except sqlite3.Error:
            print(f"  ! {t:14s} 新库无此表（请检查 schema），跳过")
            problems.append(f"{t}: 新库无此表")
            continue
        cols = [c for c in old_cols if c in new_cols]
        if not cols:
            print(f"  ! {t:14s} 无公共列，跳过")
            problems.append(f"{t}: 无公共列")
            continue
        tgt = f"{target_schema}.{t}" if target_schema else t
        collist = ",".join(cols)
        n_old = con.execute(f"SELECT COUNT(*) FROM old.{t}").fetchone()[0]
        # FTS5 虚拟表：`INSERT OR REPLACE` 会**追加**（无唯一约束）⇒ 重跑会翻倍；
        # 对这类表先清空再灌（来源是权威）。
        if t.endswith("_fts"):
            con.execute(f"DELETE FROM {tgt}")
        con.execute(f"INSERT OR REPLACE INTO {tgt}({collist}) "
                    f"SELECT {collist} FROM old.{t}")
        n_new = con.execute(f"SELECT COUNT(*) FROM {tgt}").fetchone()[0]
        conv[t] = (n_old, n_new, where)
        flag = "OK " if n_new >= n_old else "!! "
        if n_new < n_old:
            problems.append(f"{t}: 旧 {n_old} 行 → 新只有 {n_new} 行")
        # 键值表额外校验**键集合**（行数相同但键不同 = 静默丢数据）
        if t == "settings":
            ks_old = {r[0] for r in con.execute(f"SELECT key FROM old.{t}")}
            ks_new = {r[0] for r in con.execute(f"SELECT key FROM {tgt}")}
            if not ks_old <= ks_new:
                problems.append(f"settings 缺键: {sorted(ks_old - ks_new)}")
                flag = "!! "
        print(f"  {flag}{t:14s} → {where:6s} 旧 {n_old:5d} → 新 {n_new:5d}")
    con.commit()
    con.execute("DETACH DATABASE old")
    con.close()
    if problems:
        print("\n❌ 搬运校验未通过（**不删除旧库**，请人工核对后重跑）：")
        for p in problems:
            print("   -", p)
        return 2
    print("\n✅ 逐表行数/键集合校验通过（旧库保留，见下一步）")

    # 3) 旧库归档/清理
    if args.keep_old:
        print(f"\n保留旧库：{old}")
    else:
        bak = ROOT / "data" / "_migrated"
        bak.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dst = bak / f"old-paperagent-{stamp}.db"
        shutil.move(str(old), str(dst))
        print(f"\n旧库已归档：{dst.relative_to(ROOT)}")
        for extra in old.parent.glob("paperagent.db-*"):     # WAL/SHM 残留
            extra.unlink(missing_ok=True)
        try:
            old.parent.rmdir()          # .system 空目录（知识库只留知识资产）
            print("已移除空目录：knowledge_base/.system")
        except OSError:
            print(f"注意：{old.parent} 非空，未删除")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
