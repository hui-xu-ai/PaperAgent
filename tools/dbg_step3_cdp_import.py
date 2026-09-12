# -*- coding: utf-8 -*-
"""前端 F5→F3 端到端：用真实 UI 走「导入附件 → 卡片徽标 → 附件模态」。

通过 CDP 往 #imp-att-input 注入文件（File 构造 + DataTransfer），点「导入」，
再回文献库看 📎 徽标与模态内容。测试文件名为 step3-ui-probe.md（结束后人工删除）。
"""
from __future__ import annotations

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

PORT = 9333
OUT = Path("work/scratch/interact-step3-import.json")
log: list[dict] = []


def _ws_url() -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=5) as r:
        targets = json.loads(r.read().decode("utf-8"))
    for t in targets:
        if t.get("type") == "page" and "8900" in (t.get("url") or ""):
            return t["webSocketDebuggerUrl"]
    raise SystemExit("no page")


class CDP:
    def __init__(self) -> None:
        self.ws = websocket.create_connection(_ws_url(), timeout=60)
        self.i = 0

    def ev(self, js: str, await_promise: bool = False, timeout: float = 60.0):
        self.i += 1
        self.ws.send(json.dumps({"id": self.i, "method": "Runtime.evaluate", "params": {
            "expression": js, "returnByValue": True, "awaitPromise": await_promise}}))
        self.ws.settimeout(timeout)
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.i:
                r = msg.get("result", {})
                if "exceptionDetails" in r:
                    return {"__error__": str(r["exceptionDetails"])[:400]}
                return r.get("result", {}).get("value")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


def step(name: str, cdp: CDP, js: str, wait: float = 0.0, ap: bool = False):
    if wait:
        time.sleep(wait)
    v = cdp.ev(js, await_promise=ap)
    log.append({"step": name, "value": v})
    print(f"--- {name} ---")
    print(json.dumps(v, ensure_ascii=False, indent=1)[:1500])


def main() -> None:
    cdp = CDP()
    cdp.ev("window.alert=()=>{}; window.confirm=()=>true; 'ok'")

    step("0.回到文献库", cdp, """(() => {
      const b=[...document.querySelectorAll('.side-tab')].find(x=>x.dataset.panel==='papers'); b.click(); return 'ok';
    })()""", wait=2.0)

    step("1.打开导入弹窗并切附件 tab", cdp, """(() => {
      document.getElementById('import-btn').click();
      const t=[...document.querySelectorAll('#import-modal .stab')].find(b=>b.dataset.itab==='att'); t.click();
      const sel=document.getElementById('imp-att-parent');
      const opt=[...sel.options].find(o=>o.value.includes('adma'));
      if (opt) sel.value = opt.value;
      document.getElementById('imp-att-kind').value='si';
      return {rid: sel.value, options: sel.options.length};
    })()""")

    step("2.注入文件并点导入（真实 fetch）", cdp, """(async () => {
      const md = '# Supporting Information (UI probe)\\n\\nRecord mobility of the probe sample was 33.7 cm2/Vs in the step3 UI test.';
      const f = new File([md], 'step3-ui-probe.md', {type:'text/markdown'});
      const dt = new DataTransfer(); dt.items.add(f);
      const inp = document.getElementById('imp-att-input');
      inp.files = dt.files;
      inp.dispatchEvent(new Event('change', {bubbles:true}));
      document.getElementById('imp-att-go').click();
      await new Promise(r => setTimeout(r, 2500));
      return {result: document.getElementById('imp-att-result').textContent.replace(/\\s+/g,' ').trim().slice(0,220)};
    })()""", ap=True)

    step("3.回文献库看卡片（📎 徽标）", cdp, """(() => {
      const b=[...document.querySelectorAll('.side-tab')].find(x=>x.dataset.panel==='papers'); b.click();
      return 'switched';
    })()""", wait=2.5)

    step("3b.卡片与徽标", cdp, """(() => ({
      badges: [...document.querySelectorAll('#papers-list .p-att')].map(x => x.textContent.trim()),
      chips: [...document.querySelectorAll('#papers-kind-chips .tc-chip')].map(x => x.textContent.trim()),
      cards: [...document.querySelectorAll('#papers-list .paper-card')].map(c => c.textContent.replace(/\\s+/g,' ').trim().slice(0,90))
    }))()""")

    step("4.打开附件模态", cdp, """(() => {
      const b=document.querySelector('#papers-list .p-att'); if(!b) return 'no-badge';
      b.click(); return 'clicked';
    })()""", wait=1.5)

    step("4b.模态内容（轮询等数据到达）", cdp, """(async () => {
      for (let i=0;i<20;i++) {
        if (document.querySelectorAll('#att-body .att-table tbody tr').length) break;
        await new Promise(r=>setTimeout(r,250));
      }
      return {
        visible: getComputedStyle(document.getElementById('att-modal')).display,
        title: document.getElementById('att-modal-title').textContent,
        head: (document.querySelector('#att-body .att-head')||{}).textContent,
        rows: [...document.querySelectorAll('#att-body .att-table tbody tr')].map(r => r.textContent.replace(/\\s+/g,' ').trim().slice(0,90)),
        firstHref: (document.querySelector('#att-body a')||{}).getAttribute ? document.querySelector('#att-body a').getAttribute('href') : null
      };
    })()""", ap=True)

    step("5.关闭 + 清场提示", cdp, """(() => {
      document.getElementById('att-close').click();
      document.getElementById('import-modal').style.display='none';
      return {attModal: getComputedStyle(document.getElementById('att-modal')).display,
              importModal: getComputedStyle(document.getElementById('import-modal')).display};
    })()""")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    cdp.close()
    print("saved:", OUT)


if __name__ == "__main__":
    main()
