# -*- coding: utf-8 -*-
import json
import sys
import time
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


# 退出阅读模式，验证普通布局铺满
ev("document.querySelector('#read-mode-btn').click()")
time.sleep(1)
print("wsClass:", ev("document.querySelector('#workspace').className"))
print("面板宽度:", ev("[...document.querySelectorAll('#desk > .desk-panel')].map(e=>"
                     "Math.round(e.getBoundingClientRect().width)).join(',')"))
print("deskW:", ev("Math.round(document.querySelector('#desk').getBoundingClientRect().width)"))
ws.close()
