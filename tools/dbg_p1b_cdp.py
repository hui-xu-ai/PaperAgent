# -*- coding: utf-8 -*-
"""P1 收口实测（CDP）：防重接线后关键写入口仍可用 + 无 JS 错误。

只读 + 模拟点击（不真正保存：设置类按钮点击后只观察忙态与返回，避免写坏配置——
故只在"点下瞬间"断言 busy，然后立刻读取状态并无副作用地结束）。
结果落盘 work/scratch/interact-p1b.json。
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

OUT = Path("work/scratch/interact-p1b.json")
log: list[dict] = []


def _ws() -> str:
    with urllib.request.urlopen("http://127.0.0.1:9333/json", timeout=5) as r:
        for t in json.loads(r.read().decode("utf-8")):
            if t.get("type") == "page" and "8900" in (t.get("url") or ""):
                return t["webSocketDebuggerUrl"]
    raise SystemExit("no page")


class CDP:
    def __init__(self) -> None:
        self.ws = websocket.create_connection(_ws(), timeout=60)
        self.i = 0

    def ev(self, js: str, ap: bool = False):
        self.i += 1
        self.ws.send(json.dumps({"id": self.i, "method": "Runtime.evaluate", "params": {
            "expression": js, "returnByValue": True, "awaitPromise": ap}}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == self.i:
                r = m.get("result", {})
                if "exceptionDetails" in r:
                    return {"__error__": str(r["exceptionDetails"])[:300]}
                return r.get("result", {}).get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


def step(name, cdp, js, wait=0.0, ap=False):
    if wait:
        time.sleep(wait)
    v = cdp.ev(js, ap=ap)
    log.append({"step": name, "value": v})
    print(f"--- {name} ---")
    print(json.dumps(v, ensure_ascii=False, indent=1)[:1000])


c = CDP()
# 捕获运行时错误（后续断言）
c.ev("""(() => { window.__errs = window.__errs || [];
  window.addEventListener('error', e => window.__errs.push(String(e.message||e)));
  window.addEventListener('unhandledrejection', e => window.__errs.push('rej:' + String(e.reason)));
  return 'ok'; })()""")

step("1.关键函数与入口存在", c, """(() => ({
  guardBtn: typeof guardBtn, askConfirm: typeof askConfirm, toast: typeof toast,
  off: typeof bindConnectivity,
  importBtns: ['imp-pdf-go','imp-md-go','imp-bib-go','imp-att-go'].map(i => !!document.getElementById(i)),
  hasSaveBtns: ['pf-save','parse-save','mineru-save','kb-path-save','retrieval-save','md-template-save',
                'sys-prompt-save','custom-css-save','kb-rules-save','tb-reset']
     .filter(i => document.getElementById(i)).length
}))()""")

step("2.设置中心：点「保存解析设置」→ 忙态→完成（无异常）", c, """(async () => {
  const tab = [...document.querySelectorAll('.side-tab')].find(x => x.dataset.panel === 'sessions');
  if (tab) tab.click();
  await new Promise(r => setTimeout(r, 300));
  const btn = document.getElementById('parse-save');
  if (!btn) return 'no-btn';
  const seen = [];
  btn.click();
  await new Promise(r => setTimeout(r, 40));
  seen.push({busy: btn.dataset.busy || '', disabled: btn.disabled});
  await new Promise(r => setTimeout(r, 1200));
  seen.push({busy: btn.dataset.busy || '', disabled: btn.disabled, text: btn.textContent.trim()});
  return {during: seen[0], after: seen[1]};
})()""", ap=True)

step("3.会话批量删除：空选时按钮 disabled（不被 guardBtn 复活）", c, """(async () => {
  const b = document.getElementById('sess-batch-del');
  if (!b) return 'no-btn';
  const before = {disabled: b.disabled};
  b.click();                                   // 无选中 → 应立即返回
  await new Promise(r => setTimeout(r, 300));
  return {before, after: {disabled: b.disabled, busy: b.dataset.busy || ''},
          confirmClosed: getComputedStyle(document.getElementById('confirm-modal')).display};
})()""", ap=True)

step("4.知识库管理面板写按钮接线（读 DOM 不点击，避免真写）", c, """(() => {
  const ids = ['kba-queue-all','kba-process1','kba-process5','kba-fts-rebuild','kba-backfill',
               'kba-journals-import','kba-ov-save'];
  const found = ids.filter(i => document.getElementById(i));
  // guardBtn 包裹的按钮在点击时才会写入 dataset.busy，这里只校验元素存在与可用
  return {found: found.length, ids: found,
          anyDisabledUnexpected: found.filter(i => document.getElementById(i).disabled)};
})()""")

step("5.无 JS 运行时错误 + 布局未破", c, """(() => ({
  errs: (window.__errs || []).slice(0, 5),
  chips: document.querySelectorAll('#papers-kind-chips .tc-chip').length,
  cards: document.querySelectorAll('#papers-list .paper-card').length,
  toastWrapFits: (() => { const r = document.getElementById('toast-wrap').getBoundingClientRect();
                          return r.right <= innerWidth + 1 && r.bottom <= innerHeight + 1; })()
}))()""")

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
c.close()
print("saved:", OUT)
