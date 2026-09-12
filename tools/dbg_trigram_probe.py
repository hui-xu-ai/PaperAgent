# -*- coding: utf-8 -*-
"""A trigram 查询语法兼容性探测：引号短语 / AND OR / snippet / bm25。"""
import sqlite3

c = sqlite3.connect(":memory:")
c.execute("CREATE VIRTUAL TABLE t USING fts5(doi, filename, content, tokenize='trigram')")
c.execute("INSERT INTO t VALUES ('10.1/a', '_note.md', '离子液体增强Nafion基离子聚合物传感器：浸泡温度与咪唑鎓类型的影响')")
c.execute("INSERT INTO t VALUES ('10.1/b', '_note.md', 'Ionic liquid-enhanced Nafion-based ionic polymer sensors with imidazolium types')")

cases = [
    ('短语引号-中文', '"离子液体传感器"'),
    ('裸词-中文', '离子液体传感器'),
    ('AND-英文', '"ionic" AND "liquid"'),
    ('OR-英文', '"ionic" OR "liquid"'),
    ('短语-英文', '"ionic liquid"'),
    ('裸英文2词', 'ionic liquid'),
    ('短词-中文2字', '"液体"'),
    ('混合', '"离子液体" AND "sensor"'),
]
for label, q in cases:
    try:
        n = c.execute("SELECT count(*) FROM t WHERE t MATCH ?", (q,)).fetchone()[0]
        snip = c.execute("SELECT snippet(t, 2, '[', ']', '…', 8) FROM t WHERE t MATCH ? LIMIT 1", (q,)).fetchone()
        print(f"{label}: hits={n} snip={str(snip)[:70]}")
    except Exception as e:
        print(f"{label}: ERROR {e}")

# 注入防护：_fts_query 已去引号
try:
    n = c.execute("SELECT count(*) FROM t WHERE t MATCH ?", ('"x" OR 1=1',)).fetchone()[0]
    print("注入防护: hits=", n)
except Exception as e:
    print("注入防护 ERROR:", e)
