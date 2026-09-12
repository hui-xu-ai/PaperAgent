# -*- coding: utf-8 -*-
"""只读探针：打印 settings / compile_jobs / papers / kb 目录实况（验收 bug3 用）。"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB = Path("data/system/app.db")


def main() -> None:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    print("== settings ==")
    for r in cur.execute("SELECT key,value FROM settings ORDER BY key"):
        v = r["value"]
        print(f"  {r['key']} = {v[:60] if isinstance(v, str) else v}")
    print("== papers ==")
    for r in cur.execute("SELECT * FROM papers"):
        d = dict(r)
        d.pop("doc_json", None)
        print("  ", d)
    print("== compile_jobs ==")
    cols = [c[1] for c in cur.execute("PRAGMA table_info(compile_jobs)")]
    print("  cols:", cols)
    for r in cur.execute("SELECT * FROM compile_jobs ORDER BY rowid DESC LIMIT 10"):
        d = {k: (str(v)[:70]) for k, v in dict(r).items()}
        print("  ", d)
    print("== papers_meta ==")
    for r in cur.execute("SELECT rid,doi,title,kind FROM papers_meta"):
        print("  ", dict(r))
    con.close()


if __name__ == "__main__":
    sys.exit(main())
