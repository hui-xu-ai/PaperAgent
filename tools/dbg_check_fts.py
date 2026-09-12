# -*- coding: utf-8 -*-
"""深查 notes_fts trigram 中文命中（真实数据）。"""
import sqlite3

c = sqlite3.connect("data/paperagent.db")
rows = c.execute("SELECT doi, filename, length(content), substr(content, 1, 120) FROM notes_fts").fetchall()
print("rows:", rows)

for q in ("浸渍温度", "浸泡温度", "咪唑鎓", "离子液体", "Nafion"):
    try:
        n = c.execute("SELECT count(*) FROM notes_fts WHERE notes_fts MATCH ?", (q,)).fetchone()[0]
    except Exception as e:
        n = f"ERR {e}"
    like = c.execute("SELECT count(*) FROM notes_fts WHERE content LIKE ?", (f"%{q}%",)).fetchone()[0]
    print(f"{q!r}: MATCH={n} LIKE={like}")

# _fts_query 的引号包裹形态 vs trigram
print("\n--- 引号短语形态（_fts_query 输出）---")
for q in ('"浸渍温度"', '"咪唑鎓"', '"离子液体"', '"Nafion"'):
    try:
        n = c.execute("SELECT count(*) FROM notes_fts WHERE notes_fts MATCH ?", (q,)).fetchone()[0]
        print(f"{q!r}: MATCH={n}")
    except Exception as e:
        print(f"{q!r}: ERR {e}")
