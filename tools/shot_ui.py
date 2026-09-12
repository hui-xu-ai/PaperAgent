# -*- coding: utf-8 -*-
r"""前端实测工具：截图 + 数值化硬检查（CDP，隔离 headless Edge）。

为什么需要它（2026-09-12 教训）：只做 DOM 数值断言**看不出观感问题**——
本轮确认弹层"正文窄列 + 按钮被拉成整高蓝条"就是这样漏掉的（同优先级 `.modal`
规则在文件后部覆盖了 `.confirm-modal`）。凡视觉/交互改动，必须**截图自看**
（`Page.captureScreenshot` 落盘 → 主线 `read_image`）。

用法：
    1) 起隔离浏览器（工作区内 profile，勿关 Harness 页面）：
       msedge --headless=new --remote-debugging-port=9333 --remote-allow-origins=* \
              --user-data-dir=<工作区>\work\.edge-shot http://127.0.0.1:8900/
    2) python tools/shot_ui.py            # 截 papers / confirm / import-att / kb 四张
       python tools/shot_ui.py --probe    # 额外做像素探针（验证截图是否为当前帧）

坑（都已踩过，写进来省时间）：
- 必须先清 `localStorage['side-collapsed']`：测试 profile 会残留折叠态 → 侧栏宽 1px，
  截图看起来"面板是空的"（其实是折叠）。
- 静态资源虽 no-cache，但**已加载的文档**仍用旧 CSS/JS → 换 `?t=<ts>` 导航一次。
- headless 偶发截到旧帧 → 用 `--probe` 种一个纯色 `#px-probe` 方块读像素自证。
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

PORT = 9333
BASE = "http://127.0.0.1:8900"
OUT = Path("work/scratch")


def _page() -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=5) as r:
        for t in json.loads(r.read().decode("utf-8")):
            if t.get("type") == "page" and "8900" in (t.get("url") or ""):
                return t
    raise SystemExit(f"未找到页面（{BASE}）；请先按文件头注释启动隔离浏览器")


class UI:
    def __init__(self) -> None:
        self.ws = websocket.create_connection(_page()["webSocketDebuggerUrl"], timeout=60)
        self.i = 0
        self.cmd("Emulation.setDeviceMetricsOverride",
                 {"width": 1280, "height": 860, "deviceScaleFactor": 1, "mobile": False})

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
            return {"__err__": str(r["exceptionDetails"])[:200]}
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
        """清掉测试 profile 的折叠态 + 给侧栏正常宽度（否则截图看起来是空的）。"""
        self.ev("""(() => {
          localStorage.setItem('side-collapsed', '0');
          localStorage.setItem('panel-w-left', '440');
          const ws = document.querySelector('.workspace');
          if (ws) ws.classList.remove('side-collapsed', 'read-mode');
          window.alert = () => {};
          return 'ok';
        })()""")

    def panel(self, name: str) -> None:
        self.ev("""((n) => {
          const t = [...document.querySelectorAll('.side-tab')].find(x => x.dataset.panel === n);
          if (t) t.click();
          return 'ok';
        })('%s')""" % name)
        time.sleep(2.5)

    def rects(self) -> dict:
        return self.ev("""(() => {
          const r = el => { if (!el) return null; const b = el.getBoundingClientRect();
            return [Math.round(b.x), Math.round(b.y), Math.round(b.width), Math.round(b.height)]; };
          return {sidebar: r(document.getElementById('sidebar')),
                  panel: r(document.querySelector('.side-panel:not([style*="none"])')),
                  chips: r(document.getElementById('papers-kind-chips') || document.getElementById('kb-kind-chips')),
                  cards: document.querySelectorAll('#papers-list .paper-card').length,
                  kbRows: document.querySelectorAll('#kb-list-wrap .kbl-row').length,
                  errs: (window.__errs || []).slice(0, 3)};
        })()""")


def probe_pixels(ui: UI) -> None:
    """像素探针：种纯色方块 → 截图 → 读该点像素，自证截图是否为当前帧。"""
    ui.ev("""(() => { const d = document.createElement('div'); d.id = 'px-probe';
      d.style.cssText = 'position:fixed;left:600px;top:400px;width:60px;height:60px;background:#ff00ff;z-index:99999';
      document.body.appendChild(d); return 1; })()""")
    time.sleep(0.4)
    raw = base64.b64decode(ui.cmd("Page.captureScreenshot", {"format": "png"})["data"])
    (OUT / "px-probe.png").write_bytes(raw)
    try:
        import io

        from PIL import Image

        im = Image.open(io.BytesIO(raw)).convert("RGB")
        px = im.getpixel((630, 430))
        print(f"像素探针: 尺寸={im.size} 采样={px} → {'当前帧 ✅' if px == (255, 0, 255) else '旧帧 ⚠️'}")
    except Exception as e:  # noqa: BLE001
        print("PIL 不可用，跳过像素判定:", e)


def main() -> None:
    ui = UI()
    ui.goto(f"{BASE}/?t={int(time.time())}")
    ui.prep()
    print("就位:", ui.ev("(() => ({toast: typeof toast, askConfirm: typeof askConfirm, guardBtn: typeof guardBtn}))()"))

    ui.panel("papers")
    print("papers:", ui.rects(), "\n  shot:", ui.shot("ui-papers.png"))

    ui.ev("askConfirm('删除「示例文献.pdf」的导入记录？\\n\\n删除后此 PDF 可重新导入。', {title:'确认删除'})")
    time.sleep(0.7)
    print("confirm rect:", ui.ev("""(() => {
      const m = document.querySelector('.modal.confirm-modal').getBoundingClientRect();
      const y = document.getElementById('confirm-yes').getBoundingClientRect();
      return {modal: [Math.round(m.width), Math.round(m.height)],
              btnH: Math.round(y.height), fits: Math.round(m.width) <= 460};
    })()"""), "\n  shot:", ui.shot("ui-confirm.png"))

    ui.ev("document.getElementById('confirm-no').click(); document.getElementById('import-btn').click(); setImportTab('att');")
    time.sleep(1.0)
    print("import-att 父资源下拉:", ui.ev("document.getElementById('imp-att-parent').options.length"),
          "\n  shot:", ui.shot("ui-import-att.png"))

    ui.ev("document.getElementById('import-modal').style.display='none';")
    ui.panel("kb")
    print("kb:", ui.rects(), "\n  shot:", ui.shot("ui-kb.png"))

    if "--probe" in sys.argv:
        probe_pixels(ui)
    ui.ws.close()


if __name__ == "__main__":
    main()
