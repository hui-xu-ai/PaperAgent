# -*- coding: utf-8 -*-
"""CDP 最小探测：连接隔离 Edge，执行 JS 求值。

用法：
    python tools/cdp_eval.py "<js>" [--port 9333] [--page "PaperAgent..."]
说明：
    通过 /json 找到匹配 title 的 page target，打开 WebSocket，执行 Runtime.evaluate
    返回 JSON 结果。用于前端运行时取证（布局 rect / 点击 / 输入 / 状态断言）。
"""
import argparse
import json
import sys
import urllib.request

try:
    import websocket
except Exception:  # noqa BLE001
    sys.stderr.write("需要 websocket-client：pip install websocket-client\n")
    sys.exit(2)


def _targets(port):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("js")
    ap.add_argument("--port", type=int, default=9333)
    ap.add_argument("--page", default="PaperAgent")
    ap.add_argument("--await", type=float, default=0.0)
    args = ap.parse_args()

    targets = _targets(args.port)
    pages = [t for t in targets if t.get("type") == "page" and args.page in (t.get("title") or "")]
    if not pages:
        sys.stderr.write(f"未找到匹配页面 title={args.page!r}\n")
        sys.exit(3)
    ws_url = pages[0]["webSocketDebuggerUrl"]
    ws = websocket.create_connection(ws_url, timeout=20)
    # 先吞掉 alert/confirm 弹窗（headless 弹窗会阻塞后续 CDP 调用）
    ws.send(json.dumps({"id": 0, "method": "Runtime.evaluate",
                        "params": {"expression":
                                   "window.alert=()=>window.__alert_hook_delete='1';"
                                   "window.confirm=()=>true;window.prompt=()=>null;1",
                                   "returnByValue": True}}))
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") == 0:
            break
    ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                        "params": {"expression": args.js, "returnByValue": True,
                                   "awaitPromise": True}}))
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") == 1:
            if "error" in msg:
                print(json.dumps({"__error__": msg["error"]}, ensure_ascii=False))
            elif "result" in msg:
                res = msg["result"]
                if "exceptionDetails" in res:
                    print(json.dumps({"__exc__": res["exceptionDetails"].get("text",
                                          res["exceptionDetails"].get("exception", {}).get("description", ""))},
                                     ensure_ascii=False))
                else:
                    v = res.get("result", {}).get("value")
                    print(json.dumps(v, ensure_ascii=True, default=str))
            break
    ws.close()


if __name__ == "__main__":
    main()
