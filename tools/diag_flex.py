# -*- coding: utf-8 -*-
import json
import sys
import urllib.request

import websocket

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
tabs = json.load(urllib.request.urlopen("http://127.0.0.1:9222/json"))
ws = websocket.create_connection(
    next(t["webSocketDebuggerUrl"] for t in tabs if t.get("type") == "page"),
    timeout=20)
mid = 0


def cmd(m, p=None):
    global mid
    mid += 1
    ws.send(json.dumps({"id": mid, "method": m, "params": p or {}}))
    while True:
        r = json.loads(ws.recv())
        if r.get("id") == mid:
            return r


def ev(x):
    return cmd("Runtime.evaluate", {"expression": x, "returnByValue": True}
               ).get("result", {}).get("result", {}).get("value")


print("deskW:", ev("Math.round(document.querySelector('#desk').getBoundingClientRect().width)"))
print("chat computed flex:", ev("getComputedStyle(document.querySelector('#chat-panel')).flex"))
print("chat inline flex:", repr(ev("document.querySelector('#chat-panel').style.flex")))
print("reader computed flex:", ev("getComputedStyle(document.querySelector('#reader')).flex"))
print("reader inline flex:", repr(ev("document.querySelector('#reader').style.flex")))
print("wsClass:", ev("document.querySelector('#workspace').className"))
ws.close()
