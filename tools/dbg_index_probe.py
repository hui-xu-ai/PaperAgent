# -*- coding: utf-8 -*-
"""只读探针：索引/元数据/编译状态实况（用于定位"文献提问没吃正文"）。

用法：python tools\\dbg_index_probe.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

DB = Path("data/system/app.db")
KEYS = ("retrieval_mode", "kb_copy_mode", "mineru", "auto_compile",
        "prices", "parse", "chat_reasoning_effort")
# 2026-09-12 批1：kb_include / retrieval_include 已删（装饰键，无消费点）


def main() -> None:
    con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    print("== 行数 ==")
    for t in ("papers", "papers_meta", "identifiers", "notes_fts", "fulltext_fts",
              "meta_fts", "compile_jobs", "messages", "sessions"):
        try:
            n = con.execute(f"select count(*) from {t}").fetchone()[0]
            print(f"  {t:16s} {n}")
        except Exception as e:  # noqa: BLE001
            print(f"  {t:16s} ERR {e}")

    print("== settings 关键项 ==")
    q = "select key,value from settings where key in (%s)" % ",".join("?" * len(KEYS))
    for r in con.execute(q, KEYS):
        print(f"  {r['key']} = {str(r['value'])[:220]}")

    print("== compile_jobs ==")
    for r in con.execute("select * from compile_jobs"):
        print("  ", dict(r))

    print("== papers_meta ==")
    try:
        for r in con.execute("select rid,doi,title,kind,year,journal from papers_meta"):
            print("  ", dict(r))
    except Exception as e:  # noqa: BLE001
        print("  ERR", e)

    print("== notes_fts 样本（前 5 行 key/filename） ==")
    try:
        cols = [c[1] for c in con.execute("PRAGMA table_info(notes_fts)")]
        print("  cols:", cols)
        for r in con.execute("select * from notes_fts limit 5"):
            d = dict(r)
            for k in list(d):
                if isinstance(d[k], str) and len(d[k]) > 60:
                    d[k] = d[k][:60] + "…"
            print("  ", d)
    except Exception as e:  # noqa: BLE001
        print("  ERR", e)

    print("== fulltext_fts 样本 ==")
    try:
        cols = [c[1] for c in con.execute("PRAGMA table_info(fulltext_fts)")]
        print("  cols:", cols)
        for r in con.execute("select * from fulltext_fts limit 3"):
            d = dict(r)
            for k in list(d):
                if isinstance(d[k], str) and len(d[k]) > 60:
                    d[k] = d[k][:60] + "…"
            print("  ", d)
    except Exception as e:  # noqa: BLE001
        print("  ERR", e)
    con.close()


if __name__ == "__main__":
    main()
