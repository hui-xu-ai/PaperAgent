#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dbg_cdp_kbadmin.py — M5 知识库管理面板 CDP 实测（无头 Edge + websocket）

验证（真实 API 链路，backend 已重启加载新路由）：
1. 页面加载无 JS 异常；「知识库管理」按钮存在
2. 点击打开模态：5 个 tab 渲染、布局 rect 正确（980 宽 / 136 导航）
3. 编译 tab：jobs 表真实加载
4. 元数据 tab：papers 表 + kb 原文层徽标真实加载
5. 期刊 tab：journals stats 真实加载
6. 导入 tab：真实 md（10.1063_1.5004573）导入 → library/kb 四件 + 一致性校验通过
7. 问答 tab：召回 + LLM 回答（真实调用）
"""
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dbg_cdp_cancel import CDP, get_page_ws  # noqa: E402

CDP_BASE = "http://127.0.0.1:9223"
TARGET_URL = "http://127.0.0.1:8900/"


def wait_for(cdp, expr, timeout=15, interval=0.6):
    for _ in range(int(timeout / interval)):
        r = cdp.eval(expr)
        if r.get("value"):
            return r
        time.sleep(interval)
    return r


def main() -> int:
    cdp = CDP(get_page_ws())
    # 覆盖视口为宽屏（默认 800x600 会触发 modal max-width:94vw 收缩，非 bug）
    cdp.call("Emulation.setDeviceMetricsOverride",
             {"width": 1280, "height": 900, "deviceScaleFactor": 1,
              "mobile": False})
    # 页面 JS 就绪
    wait_for(cdp, "typeof bindKbAdmin === 'function'")
    print("[0] 页面就绪:", cdp.eval("document.title"))

    # ---- 1) 按钮存在 + 打开模态 ----
    r = cdp.eval("!!document.getElementById('kb-admin-btn')")
    assert r.get("value"), "知识库管理按钮缺失"
    print("[1] 按钮存在 ✓")
    r = cdp.eval("document.getElementById('kb-admin-btn').click(); 'clicked'")
    time.sleep(0.8)
    r = cdp.eval("""(() => {
        const m = document.getElementById('kb-admin-modal');
        const tabs = Array.from(m.querySelectorAll('.stab')).map(b => b.dataset.stab);
        const rect = m.querySelector('.modal').getBoundingClientRect();
        const cs = getComputedStyle(m.querySelector('.modal'));
        return {display: m.style.display, tabs: tabs,
                w: Math.round(rect.width), h: Math.round(rect.height),
                css_w: cs.width,
                nav_w: Math.round(m.querySelector('.settings-nav').getBoundingClientRect().width),
                status: document.getElementById('kba-status').textContent};
    })()""")
    v = r.get("value") or {}
    print("[1] 模态:", v)
    assert v.get("display") == "flex", "模态未打开"
    assert v.get("tabs") == ["import", "compile", "meta", "ask", "journals"], "tab 缺失"
    assert v.get("css_w") == "980px", f"模态 CSS 宽度异常 {v.get('css_w')}"
    assert v.get("w") == 980, f"模态宽度异常 {v.get('w')}"
    assert v.get("nav_w") == 136, f"导航宽度异常 {v.get('nav_w')}"
    assert "元数据" in (v.get("status") or ""), f"状态未加载: {v.get('status')}"

    # ---- 2) 编译 tab ----
    cdp.eval("setKbaTab('compile')")
    time.sleep(1.2)
    r = cdp.eval("""(() => {
        const rows = document.querySelectorAll('#kba-jobs-body tr').length;
        const txt = document.querySelector('#kba-jobs-body').textContent.slice(0, 120);
        return {rows, txt};
    })()""")
    print("[2] 编译 jobs 表:", r.get("value"))
    assert r["value"]["rows"] >= 1, "jobs 表未加载"

    # ---- 3) 元数据 tab ----
    cdp.eval("setKbaTab('meta')")
    time.sleep(1.5)
    r = cdp.eval("""(() => {
        const rows = document.querySelectorAll('#kba-meta-body tr').length;
        const first = document.querySelector('#kba-meta-body tr')?.textContent.slice(0, 160) || '';
        const btns = document.querySelectorAll('#kba-meta-body button').length;
        return {rows, btns, first};
    })()""")
    print("[3] 元数据表:", r.get("value"))
    assert r["value"]["rows"] >= 1 and r["value"]["btns"] >= 4, "元数据表未加载"

    # ---- 4) 期刊 stats ----
    cdp.eval("setKbaTab('journals')")
    time.sleep(1.2)
    r = cdp.eval("document.getElementById('kba-journals-result').textContent.slice(0, 120)")
    print("[4] 期刊 stats:", r.get("value"))
    assert r.get("value") and "jcr" in r["value"].lower(), "期刊 stats 未加载"

    # ---- 5) 真实 md 导入（10.1063_1.5004573，官方 md 未入库）----
    cdp.eval("setKbaTab('import')")
    md_path = str(Path(TARGET_URL.replace("http://127.0.0.1:8900", ".")) if False else
                  Path(__file__).resolve().parents[1] / "用户提供的文献" / "10.1063_1.5004573.md")
    md_path = str(md_path).replace("\\", "/")
    print("[5] 导入路径:", md_path)
    r = cdp.eval("""(() => {
        const inp = document.getElementById('kba-md-path');
        inp.value = %s;
        document.getElementById('kba-md-import').click();
        return 'clicked';
    })()""" % json.dumps(md_path))
    print("[5] 点击:", r.get("value"))
    time.sleep(1.0)
    r = wait_for(cdp, "document.getElementById('kba-md-result').textContent.includes('✅ 导入成功')",
                 timeout=30)
    v = r.get("value")
    txt = cdp.eval("document.getElementById('kba-md-result').textContent")["value"]
    print("[5] 导入结果:", txt[:400])
    assert v, "导入未成功: " + txt[:200]

    # 磁盘侧验证：library + kb 四件
    from paperkb_side_check import check_import  # noqa: E402
    disk = check_import("10.1063/1.5004573")
    print("[5] 磁盘侧:", disk)
    assert disk["library_doc"] and disk["kb_doc"] and disk["verify_ok"], "磁盘产物异常"

    # ---- 6) 问答 tab（召回 + LLM 回答）----
    cdp.eval("setKbaTab('ask')")
    r = cdp.eval("""(() => {
        document.getElementById('kba-ask-input').value = 'What is the main contribution of the ionic liquid sensor paper? Answer in one sentence.';
        document.getElementById('kba-ask-btn').click();
        return 'asked';
    })()""")
    print("[6] 提问:", r.get("value"))
    r = wait_for(cdp, "document.getElementById('kba-ask-answer').textContent.length > 30",
                 timeout=120)
    ans = cdp.eval("document.getElementById('kba-ask-answer').textContent")["value"]
    recall = cdp.eval("document.getElementById('kba-ask-recall').textContent")["value"]
    print("[6] 召回:", recall[:150])
    print("[6] 回答:", ans[:250])
    assert r.get("value") and len(ans) > 30, "问答未返回"

    # ---- 7) 布局快照（关键元素 rect）----
    r = cdp.eval("""(() => {
        const m = document.getElementById('kb-admin-modal').querySelector('.modal');
        const pages = document.getElementById('kb-admin-modal').querySelector('.settings-pages');
        const r1 = m.getBoundingClientRect(), r2 = pages.getBoundingClientRect();
        return {modal: [r1.x, r1.y, r1.width, r1.height],
                pages: [r2.width, r2.height],
                scrollable: pages.scrollHeight >= pages.clientHeight};
    })()""")
    print("[7] 布局:", r.get("value"))

    print("\n=== ALL KBADMIN CHECKS PASSED ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
