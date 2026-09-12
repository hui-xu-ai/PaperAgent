# -*- coding: utf-8 -*-
r"""真实链路探针：单篇文献会话的中文提问（验证"提问真正吃到正文+笔记"）。

用法：python tools\dbg_paper_qa_probe.py [paper_id] [question]
零残留：自己建临时 paper 会话，跑完删除。
"""
from __future__ import annotations

import json
import sys
import urllib.request
from collections import Counter

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

BASE = "http://127.0.0.1:8900"
PAPER_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 5
QUESTION = sys.argv[2] if len(sys.argv) > 2 else "这篇文献的创新点是什么"


def _post(path: str, payload: dict):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode("utf-8"),
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _delete(path: str):
    req = urllib.request.Request(BASE + path, method="DELETE")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> None:
    created = _post(f"/api/papers/{PAPER_ID}/sessions", {})
    sid = created.get("id")
    print(f"临时 paper 会话 id={sid}（paper={PAPER_ID}）\n问题：{QUESTION}")
    kinds: Counter = Counter()
    retrieved_n = None
    answer = ""
    try:
        body = json.dumps({"session_id": sid, "question": QUESTION, "effort": "auto"}).encode()
        req = urllib.request.Request(BASE + "/api/chat/stream", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=240) as resp:
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
                    if ev.get("type") == "start":
                        retrieved_n = ev.get("retrieved")
                    elif ev.get("type") == "delta":
                        answer += ev.get("text", "")
        print("事件统计:", dict(kinds))
        print(f"**进上下文的检索片段数 retrieved = {retrieved_n}**（0 ⇒ 模型又只有题名+章节索引）")
        print("\n回答：\n" + answer.strip()[:900])
    finally:
        try:
            _delete(f"/api/sessions/{sid}")
            print(f"\n已删除临时会话 {sid}")
        except Exception as e:  # noqa: BLE001
            print("删除临时会话失败:", e)


if __name__ == "__main__":
    main()
