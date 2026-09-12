# -*- coding: utf-8 -*-
"""P2-9 补充验证：反复进出阅读模式不重复建窗；对话弹出后双阅读器平分。"""
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
    # 重置状态：非阅读模式、清窗口存储
    evaluate("localStorage.setItem('read-mode','0'); localStorage.removeItem('reader-floats-v1')")
    cmd("Page.reload")
    time.sleep(4)

    def panel_count():
        return evaluate("document.querySelectorAll('#desk > .desk-panel').length")

    def desk_widths():
        return evaluate(
            "[...document.querySelectorAll('#desk > .desk-panel')].map(e=>"
            "Math.round(e.getBoundingClientRect().width)).join(',')")

    print("初始 docked 面板:", panel_count())
    # 进入阅读模式
    evaluate("document.querySelector('#read-mode-btn').click()")
    time.sleep(1)
    print("第一次进入 → 面板数:", panel_count(), "| 宽度:", desk_widths(),
          "| chat display:", evaluate("document.querySelector('#chat-panel').style.display"))
    # 退出再进入（应不重复建窗）
    evaluate("document.querySelector('#read-mode-btn').click()")
    time.sleep(1)
    evaluate("document.querySelector('#read-mode-btn').click()")
    time.sleep(1)
    print("退出再进入 → 面板数:", panel_count(), "(应仍为 3)")
    # 对话弹出 → 双阅读器平分
    evaluate("document.querySelector('#chat-pop').click()")
    time.sleep(1)
    print("对话弹出 → chat 隐藏:", evaluate("getComputedStyle(document.querySelector('#chat-panel')).display"),
          "| 阅读器宽度:", desk_widths())
    evaluate("document.querySelector('#chat-float .fw-dockchat').click()")
    time.sleep(1)
    print("对话放回 → chat:", evaluate("getComputedStyle(document.querySelector('#chat-panel')).display"))
    ws.close()


if __name__ == "__main__":
    main()
