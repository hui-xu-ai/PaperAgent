# -*- coding: utf-8 -*-
"""P2-10 全量验证：阅读模式宽高铺满 + 新建窗口贴右等宽缩小。"""
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

    def widths():
        return evaluate(
            "[...document.querySelectorAll('#desk > .desk-panel')].map(e=>"
            "Math.round(e.getBoundingClientRect().width)).join(',')")

    def heights():
        return evaluate(
            "Math.round(document.querySelector('#reader').getBoundingClientRect().height)")

    print("1) 普通布局:", widths())
    # 新建两个阅读窗口 → 等宽缩小贴右
    evaluate("document.querySelector('#float-win-btn').click()")
    time.sleep(0.6)
    print("2) 新建1个:", widths())
    evaluate("document.querySelector('#float-win-btn').click()")
    time.sleep(0.6)
    print("3) 新建2个:", widths())
    # 进入阅读模式 → 铺满宽高
    evaluate("document.querySelector('#read-mode-btn').click()")
    time.sleep(1)
    print("4) 阅读模式宽:", widths(), "| 阅读器高:", heights(),
          "| 视口:", evaluate("innerWidth + 'x' + innerHeight"))
    ws.close()


if __name__ == "__main__":
    main()
