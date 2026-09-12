# -*- coding: utf-8 -*-
"""真实链路验证：附件导入 → 落位/索引/读回/越权（P0-B step3 T2/T3）。

用真实后端（127.0.0.1:8900）+ 真实数据（cej 文献），导入一份测试 SI 与一份审稿意见。
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8900"
RID = "10.1016_j.cej.2025.167798"     # 前端传的目录键
LIB = Path("library") / RID


def post_attachment(rid: str, kind: str, filename: str, data: bytes) -> dict:
    boundary = "----step3test"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: text/markdown\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    q = urllib.parse.urlencode({"rid": rid, "kind": kind, "index": "true"})
    req = urllib.request.Request(f"{BASE}/api/kb-meta/attachments/import?{q}", data=body,
                                 method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def get_status(path: str) -> int:
    try:
        with urllib.request.urlopen(BASE + path, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:  # noqa: PERF203
        return e.code


print("== 1) 导入 SI 与审稿意见 ==")
si = post_attachment(RID, "si", "supporting-information.md",
                     "# Supporting Information\n\nWe measured a record mobility of 12.3 cm2/Vs for the step3 probe compound.".encode())
rev = post_attachment(RID, "review", "reviewer-comments.md",
                      b"# Reviewer 2\n\nThe authors should clarify the device stability data." )
print(json.dumps(si, ensure_ascii=False))
print(json.dumps(rev, ensure_ascii=False))

print("\n== 2) 磁盘落位（应挂在该文献目录下） ==")
for p in sorted(LIB.glob("attachments/**/*")):
    print("  ", p.as_posix(), p.stat().st_size if p.is_file() else "<dir>")

print("\n== 3) 列表端点 ==")
out = get("/api/kb-meta/attachments?rid=" + urllib.parse.quote(RID))
print(json.dumps({k: out[k] for k in ("count", "by_kind", "parented", "kind", "indexed_count")}, ensure_ascii=False))

print("\n== 4) 附件文本读回（A4）==")
rd = get("/api/kb-meta/attachment/read?rid=" + urllib.parse.quote(RID) + "&path=" + urllib.parse.quote("si/supporting-information.md"))
print("ok=", rd.get("ok"), "chars=", rd.get("chars"), "head=", (rd.get("text") or "")[:48].replace("\n", " "))

print("\n== 5) 越权防护（应 403/404）==")
for bad in ("../../etc/passwd", "C:/Windows/win.ini", "../document.json"):
    code = get_status("/api/kb-meta/attachment/read?rid=" + urllib.parse.quote(RID) + "&path=" + urllib.parse.quote(bad))
    print(f"   {bad} -> HTTP {code}")

print("\n== 6) 附件进检索（kb_recall / A5）==")
rec = get("/api/kb-meta/recall?q=" + urllib.parse.quote("12.3 cm2/Vs"))
print(json.dumps(rec, ensure_ascii=False)[:400])

print("\n== 7) 列表徽标（kind/attachments 字段）==")
papers = get("/api/papers?all=true&page_size=200")["papers"]
print([{k: p.get(k) for k in ("id", "filename", "kind", "attachments")} for p in papers])
kb = get("/api/kb-meta/kb/list?compile_status=&page_size=50")["items"]
print([{k: it.get(k) for k in ("rid", "dir", "kind", "attachments", "has_attachment")} for it in kb])
print("chips:", json.dumps(get("/api/kb-meta/kind/options"), ensure_ascii=False))
