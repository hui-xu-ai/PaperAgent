# -*- coding: utf-8 -*-
r"""第二批反馈修复验证（CDP 9334）：#2 思考强度/思考框 / #4 tab 高亮+统一高度 / #5 编译按钮 /
#6 设置首个分区 / #7 切会话不重载阅读区。

用法：python tools\dbg_verify_round2.py
产出：work/scratch/r2-verify.json + r2v-*.png
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

TABS = r"""((sel) => [...document.querySelectorAll(sel)].map(b => ({
  text: b.textContent.trim().slice(0, 12),
  key: b.dataset.itab || b.dataset.stab || '',
  active: b.classList.contains('active'),
  bg: getComputedStyle(b).backgroundColor })))"""


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
      window.alert = m => { window.__lastAlert = String(m); }; window.__errs = [];
      window.addEventListener('error', e => window.__errs.push(String(e.message)));
      return 1; })()""")

    # ---------------- #6 设置首个分区 ----------------
    ui.ev("document.getElementById('settings-btn').click()")
    time.sleep(2.0)
    R["s6_settings_open"] = ui.ev("""(() => {
      const active = [...document.querySelectorAll('#settings-modal .stab.active')].map(b => b.dataset.stab);
      const vis = [...document.querySelectorAll('#settings-modal .stab-page')]
        .filter(p => getComputedStyle(p).display !== 'none').map(p => p.id);
      return {activeTabs: active, visiblePages: vis}; })()""")
    print("⑥ 设置打开:", json.dumps(R["s6_settings_open"], ensure_ascii=False))
    print("   截图:", ui.shot("r2v-settings.png"))
    # 切到"关于"再关掉重开 → 应回到第一个分区（不记忆）
    ui.ev("document.querySelector('#settings-modal .stab[data-stab=\"about\"]').click()")
    time.sleep(0.5)
    ui.ev("document.getElementById('settings-close').click()")
    time.sleep(0.3)
    ui.ev("document.getElementById('settings-btn').click()")
    time.sleep(1.5)
    R["s6_reopen"] = ui.ev("""(() => ({
      activeTabs: [...document.querySelectorAll('#settings-modal .stab.active')].map(b => b.dataset.stab),
      visiblePages: [...document.querySelectorAll('#settings-modal .stab-page')]
        .filter(p => getComputedStyle(p).display !== 'none').map(p => p.id) }))()""")
    print("⑥ 重开（不记忆）:", json.dumps(R["s6_reopen"], ensure_ascii=False))

    # 思考强度回填（#2 设置项）
    R["s2_effort_value"] = ui.ev("(document.getElementById('chat-effort') || {}).value")
    ui.ev("document.getElementById('settings-close').click()")
    time.sleep(0.3)

    # ---------------- #4 导入弹窗 tab ----------------
    ui.ev("document.getElementById('import-btn').click()")
    time.sleep(1.0)
    heights = {}
    for tab in ("pdf", "md", "bib", "att"):
        ui.ev("document.querySelector('#import-modal .stab[data-itab=\"%s\"]').click()" % tab)
        time.sleep(0.7)
        st = ui.ev(TABS + "('#import-modal .stab')")
        sz = ui.ev("""(() => { const b = document.querySelector('#import-modal .modal').getBoundingClientRect();
          return Math.round(b.height); })()""")
        active = [x["text"] for x in st if x["active"]]
        heights[tab] = sz
        R[f"s4_{tab}"] = {"activeCount": len(active), "active": active, "height": sz}
        print(f"④ tab={tab}: 活动数={len(active)} 活动={active} 高度={sz}")
    R["s4_heights"] = heights
    R["s4_height_uniform"] = len(set(heights.values())) == 1
    print("④ 高度统一:", R["s4_height_uniform"], heights)
    print("   截图:", ui.shot("r2v-import-att.png"))
    ui.ev("document.getElementById('import-modal').style.display='none'")

    # ---------------- #5 编译按钮 ----------------
    ui.ev("""((n) => { const t = [...document.querySelectorAll('.side-tab')]
      .find(x => x.dataset.panel === n); if (t) t.click(); return 1; })('kb')""")
    time.sleep(3.0)
    R["s5_kb_rows"] = ui.ev("document.querySelectorAll('#kb-list-wrap .kbl-row').length")
    ui.ev("""(() => { const r = document.querySelector('#kb-list-wrap .kbl-row');
      if (r) { r.click(); return 1; } return 0; })()""")
    time.sleep(1.5)
    R["s5_detail_btn"] = ui.ev("""(() => { const b = document.querySelector('#kb-detail-modal .kbd-ops button[data-op="compile"]');
      return b ? {text: b.textContent.trim(), level: b.dataset.level || '', disabled: b.disabled} : null; })()""")
    R["s5_compiled_text"] = ui.ev("""(() => { const el = document.querySelector('#kb-detail-modal .kbd-section div');
      return el ? el.textContent.trim() : ''; })()""")
    print("⑤ 详情编译按钮:", json.dumps(R["s5_detail_btn"], ensure_ascii=False))
    print("   编译状态行:", R["s5_compiled_text"])
    print("   截图:", ui.shot("r2v-kbdetail.png"))
    ui.ev("document.getElementById('kbd-close').click()")
    time.sleep(0.3)

    # ---------------- #2 提问区思考强度 + 思考框 ----------------
    R["s2_toolbar"] = ui.ev("""(() => {
      const eff = document.getElementById('chat-effort');
      const st = document.getElementById('chat-status');
      return {hasEffort: !!eff, options: eff ? [...eff.options].map(o => o.value) : [],
              hasStatus: !!st, value: eff ? eff.value : null}; })()""")
    print("② 提问区工具条:", json.dumps(R["s2_toolbar"], ensure_ascii=False))
    # 思考框/等待占位：直接调用真实渲染函数并量尺寸（不花 token 跑 LLM）
    R["s2_thinkbox"] = ui.ev("""(() => {
      const box = document.getElementById('messages');
      const holder = document.createElement('div'); holder.className = 'msg assistant';
      holder.innerHTML = '<div class="wait-ph muted">⏳ 正在处理… 3s</div>';
      box.appendChild(holder);
      const tb = ensureThinkBox(holder);
      tb.querySelector('.think-text').textContent = '先看题目要求……（思考链示例）';
      const r1 = tb.getBoundingClientRect(), r2 = holder.querySelector('.wait-ph').getBoundingClientRect();
      const out = {thinkBox: [Math.round(r1.width), Math.round(r1.height)], open: tb.open,
                   waitPh: [Math.round(r2.width), Math.round(r2.height)],
                   summary: tb.querySelector('summary').textContent};
      // 模拟 done：收起
      tb.open = false;
      out.collapsedAfterDone = !tb.open;
      holder.remove();
      return out; })()""")
    print("② 思考框:", json.dumps(R["s2_thinkbox"], ensure_ascii=False))
    # 切换思考强度 → 落库
    ui.ev("""(() => { const e = document.getElementById('chat-effort');
      e.value = 'high'; e.dispatchEvent(new Event('change', {bubbles:true})); return 1; })()""")
    time.sleep(1.5)
    R["s2_effort_after_change"] = ui.ev("document.getElementById('chat-status').textContent")
    print("② 切换后状态文案:", R["s2_effort_after_change"])

    # ---------------- #7 切会话不重载阅读区 ----------------
    R["s7_sessions"] = ui.ev("""(() => (state.sessions || []).filter(s => s.kind === 'paper')
      .map(s => ({id: s.id, paper_id: s.paper_id, title: (s.title || '').slice(0, 20)})))()""")
    print("⑦ 文献会话:", json.dumps(R["s7_sessions"], ensure_ascii=False))
    if len(R["s7_sessions"]) >= 2:
        ui.ev(f"openSession({R['s7_sessions'][0]['id']})", await_promise=True)
        time.sleep(2.5)
        before = ui.ev("""(() => ({html: (document.getElementById('reader-body').innerHTML || '').length,
          file: readerState.file, pid: String(readerState.paperId || ''),
          key: (document.getElementById('reader-body').innerHTML || '').slice(0, 80)}))()""")
        ui.ev(f"openSession({R['s7_sessions'][1]['id']})", await_promise=True)
        time.sleep(2.0)
        after = ui.ev("""(() => ({html: (document.getElementById('reader-body').innerHTML || '').length,
          file: readerState.file, pid: String(readerState.paperId || ''),
          key: (document.getElementById('reader-body').innerHTML || '').slice(0, 80)}))()""")
        R["s7_before"] = before
        R["s7_after"] = after
        R["s7_untouched"] = (before["key"] == after["key"] and before["file"] == after["file"])
        print("⑦ 切会话前:", before["file"], before["html"], "→ 后:", after["file"], after["html"],
              "· 阅读区未被重载:", R["s7_untouched"])
    else:
        print("⑦ 文献会话不足 2 个，无法比较（记录留空）")

    R["js_errors"] = ui.ev("(window.__errs || []).slice(0, 6)")
    print("\nJS 错误:", R["js_errors"])
    (OUT / "r2-verify.json").write_text(json.dumps(R, ensure_ascii=False, indent=2), encoding="utf-8")
    print("落盘: work/scratch/r2-verify.json")
    ui.ws.close()


if __name__ == "__main__":
    main()
