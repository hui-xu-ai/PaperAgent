#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dbg_cdp_cancel.py — P15 前端取消按钮 CDP 实测（无头 Edge + websocket）

验证：
1. 页面加载后论文列表渲染正常（无 JS 错误）
2. 注入 parsing 状态论文 → 取消按钮 .paper-cancel 出现且文案正确
3. 点击取消 → 正确调用 POST /api/tasks/{id}/cancel（fetch 拦截记录）
4. 非 parsing 状态 → 无取消按钮（不误显示）
"""
import json
import sys
import time
import urllib.parse
import urllib.request

import websocket

sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # GBK 无法编码 ✕ 等字符

CDP_BASE = "http://127.0.0.1:9223"
TARGET_URL = "http://127.0.0.1:8900/"


def _probe(ws_url: str) -> bool:
    """连接 + 1+1 探测该页面是否可响应（卡死页面返回 False）"""
    try:
        ws = websocket.create_connection(ws_url, timeout=6, suppress_origin=True)
        time.sleep(1)
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                            "params": {"expression": "1+1",
                                       "returnByValue": True}}))
        ok = '"value":2' in str(ws.recv())
        ws.close()
        return ok
    except Exception:  # noqa: BLE001 - 卡死页面
        return False


def get_page_ws() -> str:
    """优先 /json/new 创建干净页面（实测：启动 URL 页面会卡死，/json/new 正常）；
    探测不可用则遍历现有页面兜底。"""
    for attempt in range(3):
        # 1) /json/new
        try:
            url = CDP_BASE + "/json/new?" + urllib.parse.quote(TARGET_URL, safe="")
            page = json.loads(urllib.request.urlopen(
                urllib.request.Request(url, method="PUT"), timeout=10).read())
            time.sleep(4)
            if _probe(page["webSocketDebuggerUrl"]):
                print("  可用页面(/json/new):", page["id"][:8], flush=True)
                return page["webSocketDebuggerUrl"]
        except Exception:  # noqa: BLE001
            pass
        # 2) 现有页面兜底
        with urllib.request.urlopen(CDP_BASE + "/json", timeout=5) as r:
            targets = json.loads(r.read())
        for t in targets:
            if t.get("type") == "page" and t.get("url", "").startswith(TARGET_URL):
                if _probe(t["webSocketDebuggerUrl"]):
                    print("  可用页面(现有):", t["id"][:8], flush=True)
                    return t["webSocketDebuggerUrl"]
        time.sleep(2)
    raise RuntimeError("无可用 CDP 页面 target")


class CDP:
    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, timeout=30,
                                              suppress_origin=True)
        self._id = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        print("  >> CDP call:", method, flush=True)
        self.ws.send(json.dumps({"id": self._id, "method": method,
                                 "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self._id:
                return msg
            if msg.get("method") == "Runtime.exceptionThrown":
                print("  [page exception]", msg["params"].get("exceptionDetails", {})
                      .get("text", ""))
                return {"error": {"exception": msg}}

    def eval(self, expr: str) -> dict:
        r = self.call("Runtime.evaluate", {"expression": expr,
                                           "returnByValue": True,
                                           "awaitPromise": True})
        if "error" in r:
            return {"exception": True}
        res = r.get("result", {}).get("result", {})
        if res.get("type") == "object" and res.get("subtype") == "error":
            return {"exception": res.get("description", "")}
        return {"value": res.get("value")}


def main() -> int:
    cdp = CDP(get_page_ws())
    # 页面 JS 就绪等待（不做 Page 域调用——reload 会重置 ws 事件流）
    for _ in range(8):
        r = cdp.eval("typeof renderPapers === 'function'")
        if r.get("value") is True:
            break
        time.sleep(1)
    r = cdp.eval("document.title")
    print("[1] title =", r)
    r = cdp.eval("typeof renderPapers === 'function'")
    print("[1] renderPapers 可用 =", r)

    # 2) 注入 parsing 论文 → 取消按钮
    js = """
    (() => {
      const paper = {
        id: 999, filename: "TEST_PARSING.pdf", title: "TEST_PARSING.pdf",
        status: "parsing", error: "", doc_json: null, parse_source: "dual",
        md_template: "", pipeline_mode: "full", paragraph_count: 0,
        translated_paragraphs: 0
      };
      state.papers = [paper];
      renderPapers();
      const btn = document.querySelector('.paper-cancel');
      const all = Array.from(document.querySelectorAll('.paper-card'));
      return {
        btn_exists: !!btn,
        btn_text: btn ? btn.textContent.trim() : null,
        btn_title: btn ? btn.getAttribute('title') : null,
        card_count: all.length,
        status_label: document.querySelector('.p-status')?.textContent
      };
    })()
    """
    r = cdp.eval(js)
    print("[2] parsing 卡片 =", r)
    assert r.get("value", {}).get("btn_exists"), "取消按钮未渲染！"
    assert "取消解析" in r["value"]["btn_text"], "按钮文案错误"

    # 3) 点击取消 → 拦截 fetch 确认 API 调用（不 awaitPromise——实测该组合易卡）
    js = """
    (() => {
      window.__calls = [];
      window.fetch = function(url, opts) {
        window.__calls.push({url: String(url), method: (opts||{}).method || 'GET'});
        return Promise.resolve({ ok: true, status: 200,
          headers: { get: () => 'application/json' },
          json: () => Promise.resolve({status: 'ok'}) });
      };
      window.confirm = () => true;   // 跳过确认框
      window.alert = () => {};       // 防错误路径 alert 阻塞主线程
      document.querySelector('.paper-cancel').click();
      return 'clicked';
    })()
    """
    r = cdp.eval(js)
    print("[3] click =", r)
    time.sleep(1.0)
    r = cdp.eval("JSON.stringify(window.__calls)")
    print("[3] fetch 调用 =", r)
    calls = json.loads(r.get("value") or "[]")
    assert any("/api/tasks/999/cancel" in c["url"] and c["method"] == "POST"
               for c in calls), "未调用取消 API！"

    # 4) 非 parsing 状态（parsed/failed）→ 无取消按钮
    js = """
    (() => {
      const base = {
        id: 999, filename: "T.pdf", title: "T.pdf", error: "", doc_json: null,
        parse_source: "dual", md_template: "", pipeline_mode: "full",
        paragraph_count: 0, translated_paragraphs: 0
      };
      state.papers = [{...base, status: 'parsed'}];
      renderPapers();
      const a = !!document.querySelector('.paper-cancel');
      state.papers = [{...base, status: 'failed'}];
      renderPapers();
      const b = !!document.querySelector('.paper-cancel');
      return {parsed_has_cancel: a, failed_has_cancel: b};
    })()
    """
    r = cdp.eval(js)
    print("[4] 非解析状态 =", r)
    assert not r["value"]["parsed_has_cancel"] and not r["value"]["failed_has_cancel"], \
        "parsed/failed 不应显示取消按钮"

    print("\n=== ALL CHECKS PASSED ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
