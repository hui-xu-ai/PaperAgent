#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Q5 阶段1 CDP 实测：文献卡片 kb 徽标 + 单篇操作组（编译/纳入/重新同步/校验）。"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dbg_cdp_cancel import CDP, get_page_ws  # noqa: E402


def wait_for(cdp, expr, timeout=20, interval=0.8):
    for _ in range(int(timeout / interval)):
        r = cdp.eval(expr)
        if r.get("value"):
            return r
        time.sleep(interval)
    return r


def main() -> int:
    cdp = CDP(get_page_ws())
    cdp.call("Emulation.setDeviceMetricsOverride",
             {"width": 1400, "height": 950, "deviceScaleFactor": 1, "mobile": False})
    wait_for(cdp, "typeof renderPapers === 'function'")
    wait_for(cdp, "document.querySelectorAll('#papers-list .paper-card').length >= 1",
             timeout=20)
    print("[0] 文献卡片已渲染")

    # ---- 1) kb 徽标 + 操作按钮 ----
    r = cdp.eval("""(() => {
        const card = document.querySelector('#papers-list .paper-card');
        const badge = card.querySelector('.kb-badge');
        const ops = Array.from(card.querySelectorAll('.paper-kbop')).map(b => b.dataset.op);
        return {badge: badge ? badge.textContent : null,
                badgeTitle: badge ? badge.getAttribute('title') : null,
                ops: ops};
    })()""")
    print("[1] 卡片:", r.get("value"))
    v = r.get("value") or {}
    assert v.get("ops") and set(v["ops"]) >= {"compile", "sync", "resync", "verify"}, "操作按钮缺失"

    # ---- 2) 点「编译L1」→ 真实 API（compile/queue）→ 卡片刷新 ----
    cdp.eval("window.alert = () => {}; window.confirm = () => true;")  # headless 无对话框
    r = cdp.eval("""(() => {
        const btn = Array.from(document.querySelectorAll('.paper-kbop'))
            .find(b => b.dataset.op === 'compile');
        btn.click();
        return 'clicked';
    })()""")
    time.sleep(2)
    r = cdp.eval("""(() => {
        const card = document.querySelector('#papers-list .paper-card');
        return {btn_text: Array.from(card.querySelectorAll('.paper-kbop')).map(b => b.textContent),
                badge: card.querySelector('.kb-badge')?.textContent};
    })()""")
    print("[2] 编译L1 后:", r.get("value"))

    # ---- 3) 管理面板元数据页：无 sync/resync/verify 行按钮 ----
    cdp.eval("""(() => {
        document.getElementById('kb-admin-btn').click();
        document.querySelector('#kb-admin-modal .stab[data-stab="meta"]').click();
        return 'opened';
    })()""")
    time.sleep(1.5)
    r = cdp.eval("""Array.from(document.querySelectorAll('#kba-meta-body button'))
        .map(b => b.dataset.act || b.textContent)""")
    print("[3] 元数据页行按钮:", r.get("value"))
    acts = r.get("value") or []
    assert all(a in ("detail", "详情") for a in acts), "元数据页不应再有逐篇 sync/verify 按钮"
    # 面板应有「补齐全部 kb」按钮
    r = cdp.eval("!!document.getElementById('kba-backfill')")
    print("[3] backfill 按钮存在:", r.get("value"))
    assert r.get("value"), "backfill 按钮缺失"

    print("\n=== Q5 STAGE1 CHECKS PASSED ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
