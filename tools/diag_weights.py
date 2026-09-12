# -*- coding: utf-8 -*-
"""P2-13 验证：权重模型——拖动/新建/删除/阅读模式，右缘恒贴桌面右缘。"""
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
    ws = websocket.create_connection(ws_url, timeout=30)
    mid = 0

    def cmd(m, p=None):
        nonlocal mid
        mid += 1
        ws.send(json.dumps({"id": mid, "method": m, "params": p or {}}))
        while True:
            r = json.loads(ws.recv())
            if r.get("id") == mid:
                return r

    def ev(x):
        return cmd("Runtime.evaluate", {"expression": x, "returnByValue": True}
                   ).get("result", {}).get("result", {}).get("value")

    def widths():
        return ev(
            "Array.from(document.querySelectorAll('#desk > .desk-panel')).filter("
            "e=>getComputedStyle(e).display!=='none').map("
            "e=>e.id+':'+Math.round(e.getBoundingClientRect().width)).join('  ')")

    def desk_w():
        return ev("Math.round(document.querySelector('#desk').getBoundingClientRect().width)")

    def drag_rz(el_expr, dx):
        """通过 CDP 鼠标事件模拟拖动某面板右缘 desk-rz。"""
        pos = ev(f"({{r:document.querySelector({el_expr}).querySelector('.desk-rz').getBoundingClientRect()}})&&"
                 f"[Math.round(document.querySelector({el_expr}).querySelector('.desk-rz').getBoundingClientRect().left),"
                 f"Math.round(document.querySelector({el_expr}).querySelector('.desk-rz').getBoundingClientRect().top)]")
        x, y = pos[0], pos[1]
        cmd("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
        for step in range(1, 7):
            cmd("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x + dx * step / 6, "y": y, "button": "left"})
            time.sleep(0.03)
        cmd("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x + dx, "y": y, "button": "left", "clickCount": 1})
        time.sleep(0.3)

    cmd("Page.navigate", {"url": APP})
    time.sleep(4)
    ev("localStorage.setItem('read-mode','0'); localStorage.removeItem('reader-floats-v1')")
    cmd("Page.reload")
    time.sleep(4)

    print("视口宽:", ev("innerWidth"), "| deskW:", desk_w())
    print("1) 初始:", widths())
    # 拖动对话区右缘向右 300（对话变宽 → 阅读器应自动缩，右缘贴右）
    drag_rz("'#chat-panel'", 300)
    print("2) 对话拖宽+300:", widths(), "| deskW:", desk_w())
    # 拖动对话区左移 200（变窄 → 阅读器自动扩，右缘贴右）
    drag_rz("'#chat-panel'", -200)
    print("3) 对话拖窄-200:", widths(), "| deskW:", desk_w())
    # 新建 2 个窗口
    ev("document.querySelector('#float-win-btn').click()"); time.sleep(0.6)
    ev("document.querySelector('#float-win-btn').click()"); time.sleep(0.6)
    print("4) 新建2窗:", widths())
    # 拖动第一个阅读器变宽
    drag_rz("'#reader'", 250)
    print("5) reader拖宽+250:", widths())
    # 关闭一个
    ev("document.querySelectorAll('#desk .fw-close')[0].click()"); time.sleep(0.6)
    print("6) 关闭1窗:", widths())
    # 阅读模式
    ev("document.querySelector('#read-mode-btn').click()"); time.sleep(1)
    print("7) 阅读模式:", widths())
    drag_rz("'#reader'", 200)
    print("8) 阅读模式拖宽:", widths())
    ws.close()


if __name__ == "__main__":
    main()
