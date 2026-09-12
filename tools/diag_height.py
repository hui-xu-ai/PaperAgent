# -*- coding: utf-8 -*-
"""P2-10 诊断：阅读模式高度（桌面高度一半问题定位）。"""
import json
import sys
import time
import urllib.request

import websocket

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:9222"
APP = "http://127.0.0.1:8900/"


def main():
    tabs = json.load(urllib.request.urlopen(f"{BASE}/json"))
    ws_url = next(t["webSocketDebuggerUrl"] for t in tabs if t.get("type") == "page")
    ws = websocket.create_connection(ws_url, timeout=20)
    msg_id = 0

    def cmd(method, params=None):
        nonlocal msg_id
        msg_id += 1
        ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            r = json.loads(ws.recv())
            if r.get("id") == msg_id:
                return r

    def evaluate(expr):
        r = cmd("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        return r.get("result", {}).get("result", {}).get("value")

    cmd("Page.navigate", {"url": APP})
    time.sleep(4)
    evaluate("localStorage.setItem('read-mode','0'); localStorage.removeItem('reader-floats-v1')")
    cmd("Page.reload")
    time.sleep(4)
    evaluate("document.querySelector('#read-mode-btn').click()")
    time.sleep(1)

    diag = evaluate(r"""
      (() => {
        const q = s => document.querySelector(s);
        const r = e => e ? e.getBoundingClientRect() : null;
        const ws = q('#workspace'), desk = q('#desk');
        const cp = q('#chat-panel'), rd = q('#reader');
        return JSON.stringify({
          viewport: { w: innerWidth, h: innerHeight },
          body: r(document.body) && { h: Math.round(r(document.body).height) },
          workspace: r(ws) && { top: Math.round(r(ws).top), h: Math.round(r(ws).height) },
          desk: r(desk) && { top: Math.round(r(desk).top), h: Math.round(r(desk).height) },
          chat: r(cp) && { h: Math.round(r(cp).height) },
          reader: r(rd) && { h: Math.round(r(rd).height) },
          wsRows: getComputedStyle(document.body).gridTemplateRows,
          eventBar: r(q('#event-panel')) && { h: Math.round(r(q('#event-panel')).height) },
        });
      })()
    """)
    print("=== 阅读模式高度诊断 ===")
    print(json.dumps(json.loads(diag), ensure_ascii=False, indent=2))
    ws.close()


if __name__ == "__main__":
    main()
