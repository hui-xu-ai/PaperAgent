# -*- coding: utf-8 -*-
r"""真实链路探针：知识库回收站（移出 → 不显示/不可检索 → 恢复）。

用法：python tools\dbg_trash_flow.py [资源键]
默认对 cej 篇做一次完整往返（**会自动恢复**，产物不动）。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

BASE = "http://127.0.0.1:8900"
KEY = sys.argv[1] if len(sys.argv) > 1 else "10.1016/j.cej.2025.167798"
QUERY = sys.argv[2] if len(sys.argv) > 2 else "Nafion"


def _get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(path: str, payload: dict):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"HTTP": e.code, "detail": e.read().decode("utf-8", errors="replace")[:200]}


def _kb_keys() -> list[str]:
    return [i.get("doi") or i.get("rid") for i in _get("/api/kb-meta/kb/list")["items"]]


def _index_rows() -> list[dict]:
    """直接读索引表（只读），用于证明"不再被检索"是**索引真的摘掉了**，而非仅列表过滤。"""
    import sqlite3
    from pathlib import Path

    db = Path("data/system/app.db")
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("SELECT doi, filename FROM notes_fts")]
    con.close()
    return rows


def main() -> None:
    print(f"目标资源 = {KEY}")
    before = _kb_keys()
    print("① 移出前 知识库列表:", before)
    print("   索引行:", [f"{r['doi']}/{r['filename']}" for r in _index_rows()])

    print("\n② 移出到回收站:", _post("/api/kb-meta/kb/trash", {"key": KEY}))
    after = _kb_keys()
    print("   知识库列表:", after, "→ 已从列表消失:", KEY not in after)
    remain = [r for r in _index_rows()
              if KEY in r["doi"] or KEY.replace("/", "_") in r["doi"]]
    print("   残留索引行:", remain, "→ 检索已摘除:", not remain)
    hits2 = _get("/api/kb-meta/recall?q=" + urllib.parse.quote(QUERY))
    print(f"   全局召回 {QUERY!r}: {len(hits2)} 条（notes 源应已消失）")
    print("   回收站清单:", [i["key"] for i in _get("/api/kb-meta/kb/trash")["items"]])

    print("\n③ 恢复:", _post("/api/kb-meta/kb/restore", {"key": KEY}))
    back = _kb_keys()
    print("   知识库列表:", back, "→ 已回列表:",
          KEY in back or any(KEY.split("/")[-1] in k for k in back))
    print("   索引行已重建:", [f"{r['doi']}/{r['filename']}" for r in _index_rows()
                              if KEY in r["doi"] or KEY.replace("/", "_") in r["doi"]])
    print("   回收站清单:", [i["key"] for i in _get("/api/kb-meta/kb/trash")["items"]])


if __name__ == "__main__":
    main()
