# -*- coding: utf-8 -*-
"""只读探针：journals.db（JCR 影响因子 / 中科院分区）实况 + 按期刊名试查。"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

CANDIDATES = [
    Path("data/reference/journals.db"),
    Path("data/journals.db"),
    Path("work/scratch/kb-m2-live/data/journals.db"),
]
NAMES = ["Advanced Materials", "Nano Energy", "Chemical Engineering Journal"]


def probe(p: Path) -> None:
    print(f"\n=== {p} （存在={p.is_file()}）===")
    if not p.is_file():
        return
    con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    tabs = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
    print("  表:", tabs)
    for t in ("jcr", "cas"):
        if t in tabs:
            n = con.execute(f"select count(*) from {t}").fetchone()[0]
            yrs = [r[0] for r in con.execute(f"select distinct year from {t}")]
            print(f"  {t}: {n} 行，年份={yrs}")
    if "jcr" in tabs:
        for name in NAMES:
            row = con.execute(
                "select * from jcr where journal_name = ? collate nocase limit 1",
                (name,)).fetchone()
            print(f"  查 {name!r}: {dict(row) if row else '未命中'}")
        rows = con.execute("select journal_name,jif,quartile from jcr limit 5").fetchall()
        print("  样例:", [tuple(r) for r in rows])
    con.close()


def main() -> None:
    for p in CANDIDATES:
        probe(p)
    print("\n=== 可能的 JCR 源文件 ===")
    for pat in ("**/*JCR*", "**/*jcr*", "**/*分区*", "**/*中科院*"):
        for hit in Path(".").glob(pat):
            s = str(hit)
            if any(x in s for x in ("work\\.edge", "node_modules", ".venv", "pytest-tmp")):
                continue
            print("  ", s)


if __name__ == "__main__":
    main()
