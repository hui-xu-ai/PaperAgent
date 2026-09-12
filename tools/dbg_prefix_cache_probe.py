# -*- coding: utf-8 -*-
r"""真实链路探针：验证"原文进稳定前缀"后，同一篇的**第 2 轮提问是否命中前缀缓存**。

用户设计（2026-09-12）："我都上传文献了，为什么不能利用这个上传原文前缀进行提问，
这样不是能避免重复上传浪费 token 吗？" —— 本脚本给出数值答案。

用法：python tools\dbg_prefix_cache_probe.py [paper_id]
零残留：自建临时 paper 会话，跑完删除。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

BASE = "http://127.0.0.1:8900"
DB = Path("data/system/app.db")
PAPER_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 5
QUESTIONS = ["论文里用了哪些原位表征？",
             "驱动电压和位移的典型数值是多少？",
             "作者认为该方法的主要限制是什么？"]


def _post(path: str, payload: dict | None = None):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload or {}).encode(),
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _delete(path: str):
    req = urllib.request.Request(BASE + path, method="DELETE")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _ask(sid: int, question: str) -> dict:
    body = json.dumps({"session_id": sid, "question": question, "effort": "auto"}).encode()
    req = urllib.request.Request(BASE + "/api/chat/stream", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    out = {"retrieved": None, "reasoning": 0, "answer": ""}
    with urllib.request.urlopen(req, timeout=300) as resp:
        buf = ""
        while True:
            chunk = resp.read(1)
            if not chunk:
                break
            buf += chunk.decode("utf-8", errors="replace")
            while "\n\n" in buf:
                part, buf = buf.split("\n\n", 1)
                if not part.startswith("data: "):
                    continue
                ev = json.loads(part[6:])
                if ev.get("type") == "start":
                    out["retrieved"] = ev.get("retrieved")
                    out["fulltext_prefix"] = ev.get("fulltext_prefix")
                elif ev.get("type") == "reasoning":
                    out["reasoning"] += 1
                elif ev.get("type") == "delta":
                    out["answer"] += ev.get("text", "")
    return out


def _usage(sid: int) -> dict | None:
    con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM llm_usage WHERE context=? ORDER BY id DESC LIMIT 1",
                      (f"session:{sid}",)).fetchone()
    con.close()
    return dict(row) if row else None


def main() -> None:
    created = _post(f"/api/papers/{PAPER_ID}/sessions", {})
    sid = created.get("id")
    print(f"临时 paper 会话 id={sid}（paper={PAPER_ID}）")
    try:
        for i, q in enumerate(QUESTIONS, 1):
            r = _ask(sid, q)
            u = _usage(sid) or {}
            print(f"\n—— 第 {i} 轮：{q}")
            print(f"   retrieved={r.get('retrieved')}  fulltext_prefix={r.get('fulltext_prefix')}"
                  f"  reasoning 片段={r['reasoning']}  回答长度={len(r['answer'])}")
            print(f"   用量: prompt={u.get('prompt_tokens')}  缓存命中={u.get('cache_hit_tokens')}"
                  f"  输出={u.get('completion_tokens')}  费用=¥{u.get('cost')}")
    finally:
        try:
            _delete(f"/api/sessions/{sid}")
            print(f"\n已删除临时会话 {sid}")
        except Exception as e:  # noqa: BLE001
            print("删除临时会话失败:", e)


if __name__ == "__main__":
    main()
