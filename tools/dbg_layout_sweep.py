# -*- coding: utf-8 -*-
r"""弹层/按钮布局扫描（CDP，端口 9334）。

目的：把"按钮太高/被拉伸/文字竖排"这类观感问题变成**数值判定**——
遍历各弹层里的 .btn，量 rect 与文本内容，凡是
  height > 44px  （异常高）或
  width < 字号*文本长度*0.6（被挤到换行/竖排）
就列为可疑项，落盘 work/scratch/layout-sweep.json。

用法：python tools/dbg_layout_sweep.py
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

BTN_AUDIT = r"""(() => {
  const bad = [];
  document.querySelectorAll('.btn, button').forEach(b => {
    const r = b.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return;          // 不可见跳过
    const cs = getComputedStyle(b);
    if (cs.visibility === 'hidden') return;
    const t = (b.textContent || '').trim();
    const fs = parseFloat(cs.fontSize) || 12;
    // 期望单行宽 ≈ 字号*字数 + 内边距；实际宽度远小于它 = 被挤压换行/竖排
    const expect = t.length * fs * 0.95 + 24;
    const squeezed = t.length >= 2 && r.width < expect * 0.55;
    if (r.height > 44 || squeezed) {
      bad.push({sel: (b.id ? '#' + b.id : '') + (b.className ? '.' + String(b.className).split(' ').join('.') : ''),
                text: t.slice(0, 24), w: Math.round(r.width), h: Math.round(r.height),
                x: Math.round(r.x), y: Math.round(r.y),
                reason: r.height > 44 ? 'too_tall' : 'squeezed'});
    }
  });
  return bad;
})()"""


def _page() -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=5) as r:
        for t in json.loads(r.read().decode("utf-8")):
            if t.get("type") == "page" and "8900" in (t.get("url") or ""):
                return t
    raise SystemExit("未找到页面")


class UI:
    def __init__(self) -> None:
        self.ws = websocket.create_connection(_page()["webSocketDebuggerUrl"], timeout=60)
        self.i = 0

    def cmd(self, method, params=None):
        self.i += 1
        self.ws.send(json.dumps({"id": self.i, "method": method, "params": params or {}}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == self.i:
                return m.get("result", {})

    def ev(self, js, await_promise=True):
        r = self.cmd("Runtime.evaluate", {"expression": js, "returnByValue": True,
                                          "awaitPromise": await_promise})
        if "exceptionDetails" in r:
            return {"__err__": str(r["exceptionDetails"].get("exception", {}))[:200]}
        return r.get("result", {}).get("value")

    def viewport(self, w, h):
        self.cmd("Emulation.setDeviceMetricsOverride",
                 {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": False})
        time.sleep(0.5)

    def shot(self, name):
        OUT.mkdir(parents=True, exist_ok=True)
        p = OUT / name
        p.write_bytes(base64.b64decode(
            self.cmd("Page.captureScreenshot", {"format": "png"})["data"]))
        return str(p)


def close_all(ui):
    ui.ev("""(() => { ['confirm-modal','settings-modal','provider-form','import-modal',
      'att-modal','kb-admin-modal'].forEach(id => { const e = document.getElementById(id);
      if (e) e.style.display = 'none'; }); return 1; })()""")
    time.sleep(0.3)


def main() -> None:
    ui = UI()
    ui.cmd("Page.navigate", {"url": f"{BASE}/?t={int(time.time())}"})
    time.sleep(6)
    ui.ev("""(() => { localStorage.setItem('side-collapsed','0');
      localStorage.setItem('panel-w-left','440');
      const w = document.querySelector('.workspace');
      if (w) w.classList.remove('side-collapsed','read-mode');
      window.alert = () => {}; window.confirm = () => false;
      window.__errs = []; window.addEventListener('error', e => window.__errs.push(String(e.message)));
      return 1; })()""")

    report = {}
    sizes = [(1920, 1000), (1440, 900), (1280, 720), (1100, 620), (900, 600)]
    for (w, h) in sizes:
        ui.viewport(w, h)
        tag = f"{w}x{h}"
        entry = {}
        # --- 确认弹层（bug1）---
        ui.ev("askConfirm('将删除 2 条失效记录（仅删记录，不动磁盘文件）。继续？')",
              await_promise=False)
        time.sleep(0.6)
        entry["confirm"] = ui.ev("""(() => { const m = document.querySelector('.modal.confirm-modal');
          const b = m.getBoundingClientRect();
          const y = document.getElementById('confirm-yes').getBoundingClientRect();
          return {modal: [Math.round(b.width), Math.round(b.height)],
                  btn: [Math.round(y.width), Math.round(y.height)],
                  visible: getComputedStyle(document.getElementById('confirm-modal')).display}; })()""")
        entry["confirm_btns"] = ui.ev(BTN_AUDIT)
        if (w, h) == (1920, 1000):
            ui.shot("sweep-confirm-1920.png")
        ui.ev("document.getElementById('confirm-no').click()")
        time.sleep(0.3)

        # --- 供应商表单（bug2 的表单底栏）---
        ui.ev("document.getElementById('settings-btn').click();")
        time.sleep(1.5)
        ui.ev("""(() => { const b = document.querySelector('#provider-table button[data-act="edit"]');
          if (b) b.click(); return 1; })()""")
        time.sleep(0.6)
        entry["provider_form"] = ui.ev(BTN_AUDIT)
        entry["pf_row"] = ui.ev("""(() => { const el = document.getElementById('pf-test').parentElement;
          const r = el.getBoundingClientRect();
          return {row: [Math.round(r.width), Math.round(r.height)],
                  kids: [...el.children].map(c => { const b = c.getBoundingClientRect();
                    return [(c.id || c.className || c.tagName), Math.round(b.width), Math.round(b.height)]; })}; })()""")
        ui.ev("document.getElementById('pf-test').click()")
        time.sleep(2.0)
        entry["provider_form_after_test"] = ui.ev(BTN_AUDIT)
        if (w, h) == (1920, 1000):
            ui.shot("sweep-provider-1920.png")
        close_all(ui)

        # --- 导入弹窗 4 tab ---
        ui.ev("document.getElementById('import-btn').click();")
        time.sleep(0.8)
        for tab in ("pdf", "md", "bib", "att"):
            ui.ev(f"setImportTab('{tab}')")
            time.sleep(0.5)
            bad = ui.ev(BTN_AUDIT)
            if bad:
                entry[f"import_{tab}"] = bad
        if (w, h) == (1920, 1000):
            ui.shot("sweep-import-att-1920.png")
        close_all(ui)
        report[tag] = entry
        print(f"[{tag}] confirm={entry['confirm']} badBtns(confirm)={len(entry['confirm_btns'])} "
              f"provider={len(entry['provider_form'])}/{len(entry['provider_form_after_test'])} "
              f"import_bad={ {k: len(v) for k, v in entry.items() if k.startswith('import_')} }")

    report["js_errors"] = ui.ev("(window.__errs||[]).slice(0,5)")
    (OUT / "layout-sweep.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                           encoding="utf-8")
    print("JS 错误:", report["js_errors"])
    print("落盘: work/scratch/layout-sweep.json")
    ui.ws.close()


if __name__ == "__main__":
    main()
