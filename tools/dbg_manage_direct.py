# -*- coding: utf-8 -*-
"""直接测 manage 会话问答 API（不依赖前端），收集 SSE 事件。"""
import json
import sys
import urllib.request
import uuid

BASE = "http://127.0.0.1:8900"


def post(path, body):
    req = urllib.request.Request(BASE + path,
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def stream(session_id, question):
    req = urllib.request.Request(BASE + "/api/chat/stream",
                                 data=json.dumps(
                                     {"session_id": session_id, "question": question}).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        buf = b""
        while True:
            chunk = r.read(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n\n" in buf:
                raw, buf = buf.split(b"\n\n", 1)
                line = raw.decode("utf-8")
                if line.startswith("data: "):
                    ev = json.loads(line[6:])
                    t = ev.get("type")
                    if t == "tool":
                        print(f"  [tool] {ev['name']} args={ev.get('args')} ok={ev.get('ok')} sum={str(ev.get('summary'))[:80]}")
                    elif t == "delta":
                        print(f"  [delta] {ev.get('text','')[:150]}")
                    elif t in ("start", "done", "error"):
                        print(f"  [{t}] {json.dumps(ev, ensure_ascii=False)[:200]}")


sid = post("/api/sessions", {"kind": "global", "mode": "manage"})["id"]
print("manage session:", sid)
q = "列出知识库中所有文献的价值评分" + f" [{uuid.uuid4().hex[:6]}]"
print("Q:", q)
stream(sid, q)
