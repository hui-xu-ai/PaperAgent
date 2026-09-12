# -*- coding: utf-8 -*-
r"""第二批反馈复现（CDP 9334）：#4 导入弹窗 tab 高亮+高度 / #6 设置弹窗首次打开空白。

用法：python tools/dbg_repro_round2.py
产出：work/scratch/r2-*.png + work/scratch/r2-repro.json
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
R: dict = {}

TAB_STATE = r"""((sel) => {
  const out = [];
  document.querySelectorAll(sel).forEach(b => {
    const cs = getComputedStyle(b);
    const r = b.getBoundingClientRect();
    out.push({text: b.textContent.trim().slice(0, 14),
              itab: b.dataset.itab || '', stab: b.dataset.stab || '',
              active: b.classList.contains('active'),
              bg: cs.backgroundColor, color: cs.color,
              visible: r.width > 0 && r.height > 0});
  });
  return out;
})"""


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
        self.cmd("Emulation.setDeviceMetricsOverride",
                 {"width": 1600, "height": 1000, "deviceScaleFactor": 1, "mobile": False})

    def cmd(self, method, params=None):
        self.i += 1
        self.ws.send(json.dumps({"id": self.i, "method": method, "params": params or {}}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == self.i:
                return m.get("result", {})

    def ev(self, js, await_promise=False):
        r = self.cmd("Runtime.evaluate", {"expression": js, "returnByValue": True,
                                          "awaitPromise": await_promise})
        if "exceptionDetails" in r:
            return {"__err__": str(r["exceptionDetails"].get("exception", {}))[:250]}
        return r.get("result", {}).get("value")

    def shot(self, name):
        OUT.mkdir(parents=True, exist_ok=True)
        p = OUT / name
        p.write_bytes(base64.b64decode(
            self.cmd("Page.captureScreenshot", {"format": "png"})["data"]))
        return str(p)


def main() -> None:
    ui = UI()
    ui.cmd("Page.navigate", {"url": f"{BASE}/?t={int(time.time())}"})
    time.sleep(6)
    ui.ev("""(() => { localStorage.setItem('side-collapsed','0');
      localStorage.setItem('panel-w-left','440');
      const w = document.querySelector('.workspace');
      if (w) w.classList.remove('side-collapsed','read-mode');
      window.alert = () => {}; window.__errs = [];
      window.addEventListener('error', e => window.__errs.push(String(e.message)));
      return 1; })()""")

    # ---------------- #4 导入弹窗 tab ----------------
    R["localStorage_all"] = ui.ev("Object.fromEntries(Object.entries(localStorage))")
    ui.ev("document.getElementById('import-btn').click()")
    time.sleep(1.0)
    R["import_tabs_initial"] = ui.ev(TAB_STATE + "('#import-modal .stab')")
    R["import_modal_size"] = ui.ev("""(() => { const m = document.querySelector('#import-modal .modal');
      const b = m.getBoundingClientRect();
      return [Math.round(b.width), Math.round(b.height)]; })()""")
    print("打开导入弹窗：tab 状态（初始）")
    for t in R["import_tabs_initial"]:
        print("   ", t)
    print("  弹窗尺寸:", R["import_modal_size"], "截图:", ui.shot("r2-import-pdf.png"))

    for tab in ("md", "bib", "att"):
        ui.ev("document.querySelector('#import-modal .stab[data-itab=\"%s\"]').click()" % tab)
        time.sleep(0.8)
        st = ui.ev(TAB_STATE + "('#import-modal .stab')")
        page = ui.ev("""(() => { const on = [...document.querySelectorAll('#import-modal .stab-page')]
          .filter(p => getComputedStyle(p).display !== 'none').map(p => p.id);
          const m = document.querySelector('#import-modal .modal').getBoundingClientRect();
          return {visiblePages: on, modal: [Math.round(m.width), Math.round(m.height)]}; })()""")
        R[f"import_tabs_{tab}"] = {"tabs": st, "pages": page}
        print(f"  点 {tab}: 活动 tab = {[x['text'] for x in st if x['active']]} 页={page['visiblePages']} 高={page['modal'][1]}")
        ui.shot(f"r2-import-{tab}.png")

    # 全局 .stab（含设置弹窗的）是否被 setImportTab 误改？反过来：setSettingsTab 会不会改导入弹窗
    R["all_stab_after_import_switch"] = ui.ev(TAB_STATE + "('.stab')")
    ui.ev("document.getElementById('import-modal').style.display='none'")

    # ---------------- #6 设置弹窗首个标签页 ----------------
    R["ls_before_settings"] = ui.ev("Object.fromEntries(Object.entries(localStorage))")
    ui.ev("document.getElementById('settings-btn').click()")
    time.sleep(2.0)
    R["settings_open"] = ui.ev("""(() => {
      const tabs = [...document.querySelectorAll('#settings-modal .stab')];
      const active = tabs.filter(b => b.classList.contains('active')).map(b => b.dataset.stab);
      const pages = [...document.querySelectorAll('#settings-modal .stab-page')]
        .map(p => ({id: p.id, display: getComputedStyle(p).display,
                    textLen: (p.textContent || '').trim().length}));
      const first = tabs[0] ? tabs[0].dataset.stab : '';
      const firstPage = first ? document.getElementById('settings-page-' + first) : null;
      return {activeTabs: active, firstTab: first,
              pagesVisible: pages.filter(p => p.display !== 'none').map(p => p.id),
              firstPageId: firstPage ? firstPage.id : '(未按 settings-page-<tab> 命名)',
              pages: pages}; })()""")
    print("\n设置弹窗打开:", json.dumps({k: v for k, v in R["settings_open"].items() if k != "pages"},
                                   ensure_ascii=False))
    print("  各页 display/文本长度:", json.dumps(R["settings_open"]["pages"], ensure_ascii=False)[:400])
    print("  截图:", ui.shot("r2-settings-open.png"))

    # 逐个点设置 tab，看是否影响到导入弹窗的 tab（全局 selector 污染）
    ui.ev("document.getElementById('import-btn').click()")
    time.sleep(0.6)
    ui.ev("document.querySelector('#settings-modal .stab[data-stab=\"about\"]')?.click()")
    time.sleep(0.8)
    R["import_tabs_after_settings_tab_click"] = ui.ev(TAB_STATE + "('#import-modal .stab')")
    print("\n点设置弹窗『关于』tab 之后，导入弹窗 tab 状态:")
    for t in R["import_tabs_after_settings_tab_click"]:
        print("   ", t)

    R["js_errors"] = ui.ev("(window.__errs || []).slice(0, 6)")
    print("\nJS 错误:", R["js_errors"])
    (OUT / "r2-repro.json").write_text(json.dumps(R, ensure_ascii=False, indent=2), encoding="utf-8")
    print("落盘: work/scratch/r2-repro.json")
    ui.ws.close()


if __name__ == "__main__":
    main()
