# -*- coding: utf-8 -*-
r"""修复验证（CDP，9334）：bug1' 确认弹层留白 / bug2 测试连接 / bug4 父资源选择器。

用法（先起隔离浏览器 + 后端已重启到新代码）：
  python tools/dbg_fixes_verify.py
产出：work/scratch/fix-verify.json + fix-{confirm,provider,attpicker,cardatt}.png
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
                 {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False})

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

    # ---------- bug1'：确认弹层留白 ----------
    ui.ev("askConfirm('将删除 2 条失效记录（仅删记录，不动磁盘文件）。继续？')")
    time.sleep(0.7)
    R["bug1_confirm"] = ui.ev("""(() => {
      const m = document.querySelector('.modal.confirm-modal').getBoundingClientRect();
      const msg = document.getElementById('confirm-msg').getBoundingClientRect();
      const yes = document.getElementById('confirm-yes').getBoundingClientRect();
      const no = document.getElementById('confirm-no').getBoundingClientRect();
      return {modal: [Math.round(m.width), Math.round(m.height)],
              msg: [Math.round(msg.x), Math.round(msg.width)],
              padLeft: Math.round(msg.x - m.x),
              padRight: Math.round((m.x + m.width) - (yes.x + yes.width)),
              btn: [Math.round(yes.width), Math.round(yes.height)],
              gapBetweenBtns: Math.round(yes.x - (no.x + no.width))}; })()""")
    print("bug1' 弹层:", json.dumps(R["bug1_confirm"], ensure_ascii=False))
    print("  截图:", ui.shot("fix-confirm.png"))
    ui.ev("document.getElementById('confirm-no').click()")
    time.sleep(0.3)

    # ---------- bug2：掩码 Key 测试连接 ----------
    ui.ev("document.getElementById('settings-btn').click();")
    time.sleep(1.8)
    ui.ev("""(() => { const b = document.querySelector('#provider-table button[data-act="edit"]');
      if (b) b.click(); return 1; })()""")
    time.sleep(0.6)
    R["bug2_keyfield"] = ui.ev("document.getElementById('pf-key').value")
    ui.ev("document.getElementById('pf-test').click()")
    time.sleep(1.0)
    R["bug2_result_immediate"] = ui.ev("document.getElementById('pf-test-result').textContent")
    time.sleep(6.0)
    R["bug2_result_final"] = ui.ev("document.getElementById('pf-test-result').textContent")
    R["bug2_layout"] = ui.ev("""(() => {
      const b = document.getElementById('pf-test').getBoundingClientRect();
      const r = document.getElementById('pf-test-result').getBoundingClientRect();
      return {btn: [Math.round(b.width), Math.round(b.height)],
              result: [Math.round(r.width), Math.round(r.height)]}; })()""")
    print("bug2 Key 框 =", repr(R["bug2_keyfield"]))
    print("bug2 即时文案:", R["bug2_result_immediate"])
    print("bug2 最终文案:", R["bug2_result_final"])
    print("bug2 底栏尺寸:", json.dumps(R["bug2_layout"], ensure_ascii=False))
    print("  截图:", ui.shot("fix-provider.png"))
    ui.ev("document.getElementById('pf-cancel').click(); document.getElementById('settings-close').click()")
    time.sleep(0.5)

    # ---------- bug4：父资源搜索型选择器 ----------
    # 先加载文献库面板（state.papers 由该面板填充——旧脚本顺序错会导致 count=0 假阴性）
    ui.ev("""((n) => { const t = [...document.querySelectorAll('.side-tab')]
      .find(x => x.dataset.panel === n); if (t) t.click(); return 1; })('papers')""")
    time.sleep(2.5)
    R["bug4_papers"] = ui.ev("(state.papers || []).map(p => ({id: p.id, doi: p.doi}))")

    # 规模测试：注入 300 篇合成文献后立刻搜索（同一 eval 内完成——分开做会被
    # 异步 loadPapers 覆盖 state.papers，实测踩过：注入后只剩 1 篇）
    ui.ev("""window.__injectFake = function () {
      const real = (state.papers || []).filter(p => p.id < 10000);
      const fake = [];
      for (let i = 1; i <= 300; i++) {
        fake.push({ id: 10000 + i, title: (i % 3 === 0 ? '电化学储能材料' : i % 3 === 1 ? 'Magnetic soft robot' : '钙钛矿太阳能电池')
                    + ' 研究 #' + i,
                    doi: '10.9999/journal.' + (2000 + i), filename: 'paper_' + i + '.pdf',
                    created_at: '2026-0' + (1 + (i % 9)) + '-1' + (i % 9) });
      }
      state.papers = fake.concat(real);
      return state.papers.length; }; 'ok'""")
    R["bug4_scale"] = ui.ev("""(() => {
      const real = (state.papers || []).slice();
      const fake = [];
      for (let i = 1; i <= 300; i++) {
        fake.push({ id: 10000 + i, title: (i % 3 === 0 ? '电化学储能材料' : i % 3 === 1 ? 'Magnetic soft robot' : '钙钛矿太阳能电池')
                    + ' 研究 #' + i,
                    doi: '10.9999/journal.' + (2000 + i), filename: 'paper_' + i + '.pdf',
                    created_at: '2026-0' + (1 + (i % 9)) + '-1' + (i % 9) });
      }
      state.papers = fake.concat(real);
      const out = { total: state.papers.length, candidates: attParentCandidates().length };
      [['doi_frag', '10.9999/journal.2288'], ['title_cn', '电化学'],
       ['title_en', 'magnetic'], ['no_match', 'zzz-不存在-zzz'], ['empty_query', '']]
        .forEach(([k, q]) => {
          const m = attParentMatches(q);
          out[k] = { count: m.length, first: (m[0] ? m[0].key : ''), firstTitle: (m[0] ? m[0].title : '').slice(0, 30) };
        });
      // 走真实渲染路径，确认列表 DOM 也正确
      renderAttParentList('10.9999/journal.2288');
      out.rendered = document.querySelectorAll('#imp-att-parent-list .attp-item').length;
      out.renderedText = ((document.querySelector('#imp-att-parent-list .attp-item') || {}).textContent || '').trim().slice(0, 60);
      return out; })()""")
    print("bug4 规模测试(300 篇):", json.dumps(R["bug4_scale"], ensure_ascii=False))
    ui.ev("document.getElementById('import-btn').click(); setImportTab('att');")
    time.sleep(1.0)
    R["bug4_open"] = ui.ev("""(() => ({
      hidden: document.getElementById('imp-att-parent').value,
      chosen: document.getElementById('imp-att-parent-chosen').textContent.trim(),
      hasSearchInput: !!document.getElementById('imp-att-parent-q'),
      listVisible: getComputedStyle(document.getElementById('imp-att-parent-list')).display }))()""")
    print("bug4 打开态:", json.dumps(R["bug4_open"], ensure_ascii=False))
    # 交互：输入 → 过滤 → 点选 → chip（注入 + 过滤在同一 eval，避免竞态）
    R["bug4_pick"] = ui.ev("""(() => {
      const r = window.__injectFake();
      const q = document.getElementById('imp-att-parent-q');
      q.focus(); q.value = '10.9999/journal.2288';
      q.dispatchEvent(new Event('input', {bubbles:true}));
      const n = document.querySelectorAll('#imp-att-parent-list .attp-item').length;
      const el = document.querySelector('#imp-att-parent-list .attp-item');
      if (el) el.click();
      return {injected: r, listed: n,
              hidden: document.getElementById('imp-att-parent').value,
              chosen: document.getElementById('imp-att-parent-chosen').textContent.trim()}; })()""")
    print("bug4 交互点选:", json.dumps(R["bug4_pick"], ensure_ascii=False))
    print("  截图:", ui.shot("fix-attpicker.png"))
    ui.ev("document.getElementById('import-modal').style.display='none';")

    # ---------- bug4b：文献卡「⋯ 更多 → 📎 添加附件」上下文入口 ----------
    ui.ev("""((n) => { const t = [...document.querySelectorAll('.side-tab')]
      .find(x => x.dataset.panel === n); if (t) t.click(); return 1; })('papers')""")
    time.sleep(2.5)
    ui.ev("document.querySelector('#papers-list .p-more-btn').click()")
    time.sleep(0.4)
    R["bug4b_menu"] = ui.ev("""(() => ({items: [...document.querySelectorAll('#papers-list .p-more-menu button')]
      .filter(b => getComputedStyle(b.closest('.p-more-menu')).display !== 'none')
      .map(b => b.textContent.trim())}))()""")
    print("bug4b 更多菜单:", json.dumps(R["bug4b_menu"], ensure_ascii=False))
    ui.ev("document.querySelector('#papers-list .paper-att-add').click()")
    time.sleep(1.0)
    R["bug4b_after_click"] = ui.ev("""(() => ({
      modal: getComputedStyle(document.getElementById('import-modal')).display,
      tab: (document.querySelector('#import-modal .stab.active') || {}).dataset,
      hidden: document.getElementById('imp-att-parent').value,
      chosen: document.getElementById('imp-att-parent-chosen').textContent.trim() }))()""")
    print("bug4b 点「添加附件」后:", json.dumps(R["bug4b_after_click"], ensure_ascii=False))
    print("  截图:", ui.shot("fix-cardatt.png"))

    R["js_errors"] = ui.ev("(window.__errs || []).slice(0, 6)")
    print("JS 错误:", R["js_errors"])
    (OUT / "fix-verify.json").write_text(json.dumps(R, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
    print("落盘: work/scratch/fix-verify.json")
    ui.ws.close()


if __name__ == "__main__":
    main()
