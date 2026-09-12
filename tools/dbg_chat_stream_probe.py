# -*- coding: utf-8 -*-
r"""真实链路探针：一次完整流式问答（新建临时会话 → 提问 → 记录事件序列 → 删会话）。

验证 4 件事：
  1) 请求带 effort=high 时后端不报错（端点拒绝则内部降级重试，仍应出正文）；
  2) SSE 事件序列里出现新增的 {"type":"status"} / {"type":"reasoning"}（有无 reasoning
     取决于供应商是否真的返回思维链——**没有也不算失败**，会如实打印）；
  3) 正文 delta 正常拼接出回答；
  4) 用量计费：llm_usage 里该会话行的 provider 不再是 'chat'（否则单价查不到 → ¥0）。

零残留：跑完 DELETE 该会话。用法：python tools\dbg_chat_stream_probe.py [effort]
"""
from __future__ import annotations

import json
import sqlite3
import sys
import urllib.request
from collections import Counter
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

BASE = "http://127.0.0.1:8900"
DB = Path("data/system/app.db")
EFFORT = sys.argv[1] if len(sys.argv) > 1 else "high"


def _post(path: str, payload: dict | None = None):
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _delete(path: str):
    req = urllib.request.Request(BASE + path, method="DELETE")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> None:
    created = _post("/api/sessions", {"kind": "chat"})
    sid = created.get("id")
    print(f"临时会话 id={sid}（effort={EFFORT}）")
    kinds: Counter = Counter()
    answer, reasoning = "", ""
    try:
        body = json.dumps({"session_id": sid, "question": "只回答：1+1 等于几？",
                           "effort": EFFORT}).encode("utf-8")
        req = urllib.request.Request(BASE + "/api/chat/stream", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as resp:
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
                    kinds[ev.get("type", "?")] += 1
                    if ev.get("type") == "delta":
                        answer += ev.get("text", "")
                    elif ev.get("type") == "reasoning":
                        reasoning += ev.get("text", "")
                    elif ev.get("type") == "error":
                        print("  ⚠ error 事件:", ev.get("message"))
        print("事件序列统计:", dict(kinds))
        print("status 事件:", kinds.get("status", 0), "· reasoning 片段:", kinds.get("reasoning", 0))
        print("回答前 80 字:", answer.strip()[:80].replace("\n", " "))
        print("思维链前 80 字:", (reasoning.strip()[:80].replace("\n", " ") or "(供应商本轮未返回思维链)"))
    finally:
        try:
            _delete(f"/api/sessions/{sid}")
            print(f"已删除临时会话 {sid}")
        except Exception as e:  # noqa: BLE001
            print("删除临时会话失败:", e)

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    print("\nllm_usage 最近 4 行（provider 应为真实供应商 id，而不是 'chat'）:")
    for r in con.execute("SELECT * FROM llm_usage ORDER BY rowid DESC LIMIT 4"):
        print("  ", dict(r))
    con.close()


if __name__ == "__main__":
    main()
