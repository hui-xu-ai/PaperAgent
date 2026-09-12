# -*- coding: utf-8 -*-
r"""用户报告 4 个问题的可视化复现（CDP，隔离 headless Edge，端口 9334）。

用法（先起浏览器）：
  msedge --headless=new --remote-debugging-port=9334 --remote-allow-origins=* \
         --user-data-dir=<工作区>\work\.edge-bugs http://127.0.0.1:8900/
  python tools/dbg_bugs_repro.py

产出：work/scratch/bug{1,2,3,4}-*.png + work/scratch/bug-repro.json
"""
from __future__ import annotations

import base64
import json
import sys
import time
import urllib.request
from pathlib import Path

import websocket

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

PORT = 9334
BASE = "http://127.0.0.1:8900"
OUT = Path("work/scratch")
RESULT: dict = {}


def _page() -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=5) as r:
        for t in json.loads(r.read().decode("utf-8")):
            if t.get("type") == "page" and "8900" in (t.get("url") or ""):
                return t
    raise SystemExit(f"未找到页面（{BASE}）")


class UI:
    def __init__(self) -> None:
        self.ws = websocket.create_connection(_page()["webSocketDebuggerUrl"], timeout=60)
        self.i = 0
        self.cmd("Emulation.setDeviceMetricsOverride",
                 {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False})

    def cmd(self, method: str, params: dict | None = None):
        self.i += 1
        self.ws.send(json.dumps({"id": self.i, "method": method, "params": params or {}}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == self.i:
                return m.get("result", {})

    def ev(self, js: str, ap: bool = False):
        r = self.cmd("Runtime.evaluate", {"expression": js, "returnByValue": True,
                                          "awaitPromise": ap})
        if "exceptionDetails" in r:
            return {"__err__": str(r["exceptionDetails"].get("exception", {}))[:300]}
        return r.get("result", {}).get("value")

    def goto(self, url: str, wait: float = 6.0) -> None:
        self.cmd("Page.navigate", {"url": url})
        time.sleep(wait)

    def shot(self, name: str) -> str:
        OUT.mkdir(parents=True, exist_ok=True)
        p = OUT / name
        p.write_bytes(base64.b64decode(
            self.cmd("Page.captureScreenshot", {"format": "png"})["data"]))
        return str(p)

    def prep(self) -> None:
        self.ev("""(() => {
          localStorage.setItem('side-collapsed', '0');
          localStorage.setItem('panel-w-left', '440');
          const ws = document.querySelector('.workspace');
          if (ws) ws.classList.remove('side-collapsed', 'read-mode');
          window.__errs = []; window.alert = () => {}; window.confirm = () => false;
          window.addEventListener('error', e => window.__errs.push(String(e.message)));
          return 'ok';
        })()""")

    def panel(self, name: str) -> None:
        self.ev("""((n) => { const t = [...document.querySelectorAll('.side-tab')]
          .find(x => x.dataset.panel === n); if (t) t.click(); return 1; })('%s')""" % name)
        time.sleep(2.0)


RECT_JS = """((sels) => {
  const out = {};
  for (const [k, sel] of Object.entries(sels)) {
    const el = document.querySelector(sel);
    if (!el) { out[k] = null; continue; }
    const b = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    out[k] = {rect: [Math.round(b.x), Math.round(b.y), Math.round(b.width), Math.round(b.height)],
              display: cs.display, height: cs.height, minHeight: cs.minHeight,
              alignItems: cs.alignItems, flex: cs.flex, text: (el.textContent || '').slice(0, 40)};
  }
  return out;
})(%s)"""


def bug1(ui: UI) -> None:
    print("\n=== Bug1: 文献库「清理这些记录」确认弹层 ===")
    ui.panel("papers")
    # 复现用户所见状态：有失效记录时提示条可见（真实入口 = 该条上的按钮）
    ui.ev("""(() => {
      const bar = document.getElementById('papers-lost');
      bar.style.display = '';
      bar.innerHTML = '<span>⚠ 2 条文献记录在磁盘上已找不到解析产物（记录仍在，文件已被删/移走）：'
        + '10.1002_adma.202407106.pdf、JMADE-D-26-04277_R1.pdf</span>'
        + '<button class="btn small" id="papers-lost-clean" title="只删除数据库记录">🧹 清理这些记录</button>';
      return 1; })()""")
    time.sleep(0.4)
    print("  提示条:", ui.shot("bug1-lost-bar.png"))
    # 真实入口 app.js:1073 —— 注入的按钮没绑事件，故用与 handler 逐字一致的调用
    ui.ev("window.__c = askConfirm('将删除 2 条失效记录（仅删记录，不动磁盘文件）。继续？')")
    time.sleep(1.0)
    print("  弹层可见性:", ui.ev("(() => { const m = document.getElementById('confirm-modal');"
                            " return {mask: getComputedStyle(m).display,"
                            " inner: m.querySelector('.modal').getBoundingClientRect().height}; })()"))
    RESULT["bug1"] = ui.ev(RECT_JS % json.dumps({
        "mask": "#confirm-modal", "modal": ".modal.confirm-modal",
        "body": ".modal.confirm-modal .settings-body", "msg": "#confirm-msg",
        "actions": ".confirm-actions", "yes": "#confirm-yes", "no": "#confirm-no",
        "hdr": ".modal.confirm-modal .modal-header"}))
    print("  rect:", json.dumps(RESULT["bug1"], ensure_ascii=False))
    print("  截图:", ui.shot("bug1-confirm.png"))
    ui.ev("document.getElementById('confirm-no').click()")
    time.sleep(0.3)


def bug2(ui: UI) -> None:
    print("\n=== Bug2: 模型供应商「测试连接」（掩码占位） ===")
    ui.ev("document.getElementById('settings-btn').click()")
    time.sleep(2.0)
    btn = ui.ev("""(() => { const b = document.querySelector('#provider-table button[data-act="edit"]');
      if (!b) return null; b.click(); return b.closest('tr').cells[0].textContent.trim(); })()""")
    time.sleep(0.8)
    RESULT["bug2_provider"] = btn
    print("  编辑的供应商:", btn)
    RESULT["bug2_form"] = ui.ev(RECT_JS % json.dumps({
        "form": "#provider-form", "key": "#pf-key", "result": "#pf-test-result"}))
    print("  表单:", json.dumps(RESULT["bug2_form"], ensure_ascii=False))
    print("  截图:", ui.shot("bug2-provider-form.png"))
    ui.ev("document.getElementById('pf-test').click()")
    time.sleep(2.5)
    RESULT["bug2_result_text"] = ui.ev("document.getElementById('pf-test-result').textContent")
    print("  测试结果文案:", RESULT["bug2_result_text"])
    print("  截图:", ui.shot("bug2-test-result.png"))
    ui.ev("document.getElementById('pf-cancel').click(); document.getElementById('settings-close').click()")
    time.sleep(0.5)


def bug4(ui: UI) -> None:
    print("\n=== Bug4: 导入弹窗「父资源」下拉 ===")
    ui.ev("document.getElementById('import-btn').click()")
    time.sleep(1.0)
    ui.ev("setImportTab('att')")
    time.sleep(1.0)
    RESULT["bug4"] = ui.ev("""(() => {
      const s = document.getElementById('imp-att-parent');
      if (!s) return null;
      const b = s.getBoundingClientRect();
      return {count: s.options.length, tag: s.tagName, type: s.type || '',
              rect: [Math.round(b.x), Math.round(b.y), Math.round(b.width), Math.round(b.height)],
              first5: [...s.options].slice(0, 5).map(o => o.textContent.trim()),
              hasSearch: !!document.querySelector('#itab-att input[type=search], #itab-att input[list]'),
              papersInState: (window.state && state.papers || []).length}; })()""")
    print("  下拉:", json.dumps(RESULT["bug4"], ensure_ascii=False))
    print("  截图:", ui.shot("bug4-att-parent.png"))


def main() -> None:
    ui = UI()
    ui.goto(f"{BASE}/?t={int(time.time())}")
    ui.prep()
    print("就位:", ui.ev("(() => ({askConfirm: typeof askConfirm, papers: (state.papers||[]).length}))()"))
    bug1(ui)
    bug2(ui)
    bug4(ui)
    RESULT["js_errors"] = ui.ev("(window.__errs || []).slice(0, 5)")
    (OUT / "bug-repro.json").write_text(json.dumps(RESULT, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    print("\nJS 错误:", RESULT["js_errors"])
    print("汇总落盘: work/scratch/bug-repro.json")
    ui.ws.close()


if __name__ == "__main__":
    main()
