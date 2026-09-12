# -*- coding: utf-8 -*-
"""只读探针：打印会话与消息（核实"文献提问没吃正文"的真实问答内容）。"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

DB = Path("data/system/app.db")
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 2400


def main() -> None:
    con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    print("== sessions ==")
    for r in con.execute("select * from sessions"):
        d = dict(r)
        print("  ", {k: d[k] for k in d if k in ("id", "kind", "mode", "paper_id", "title", "created_at")})
    print("\n== messages ==")
    for r in con.execute("select * from messages order by id"):
        d = dict(r)
        sid, role = d.get("session_id"), d.get("role")
        content = str(d.get("content") or "")
        print(f"\n--- id={d.get('id')} session={sid} role={role} len={len(content)} ---")
        print(content[:LIMIT])
    con.close()


if __name__ == "__main__":
    main()
