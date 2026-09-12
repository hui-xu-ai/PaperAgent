# -*- coding: utf-8 -*-
"""P2-9 诊断：用 Edge CDP 实测阅读模式布局（铺满问题根因定位）。"""
import json
import sys
import time
import urllib.request

import websocket

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:9222"
APP = "http://127.0.0.1:8900/"


def get_ws_url():
    tabs = json.load(urllib.request.urlopen(f"{BASE}/json"))
    for t in tabs:
        if t.get("type") == "page":
            return t["webSocketDebuggerUrl"]
    return None


def main():
    ws_url = get_ws_url()
    if not ws_url:
        print("无 page target")
        return
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
    time.sleep(5)

    # 设置阅读模式并刷新
    evaluate("localStorage.setItem('read-mode','1'); localStorage.setItem('side-pinned','0')")
    cmd("Page.reload")
    time.sleep(5)

    diag = evaluate(r"""
      (() => {
        const q = s => document.querySelector(s);
        const ws = q('#workspace'), desk = q('#desk');
        const cp = q('#chat-panel'), rd = q('#reader');
        const fws = [...document.querySelectorAll('.float-win')].map(e => ({
          id: e.id, rect: e.getBoundingClientRect().width }));
        const docked = [...document.querySelectorAll('#desk > .desk-panel')].map(e => ({
          id: e.id, cls: e.className, w: Math.round(e.getBoundingClientRect().width),
          flex: getComputedStyle(e).flex, display: getComputedStyle(e).display,
          inlineFlex: e.style.flex }));
        return JSON.stringify({
          viewport: window.innerWidth,
          wsClass: ws.className,
          wsGrid: getComputedStyle(ws).gridTemplateColumns,
          deskW: Math.round(desk.getBoundingClientRect().width),
          deskLeft: Math.round(desk.getBoundingClientRect().left),
          chat: cp ? Math.round(cp.getBoundingClientRect().width) : null,
          reader: rd ? Math.round(rd.getBoundingClientRect().width) : null,
          docked, floats: fws,
        });
      })()
    """)
    print("=== 阅读模式布局诊断 ===")
    print(json.dumps(json.loads(diag), ensure_ascii=False, indent=2))
    ws.close()


if __name__ == "__main__":
    main()
