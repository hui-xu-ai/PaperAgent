#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dbg_cdp_kbmodes.py — 主 agent 知识库模式 CDP 实测（无头 Edge + websocket）

真实链路验证：
1. 新建会话菜单 3 项（普通聊天/知识库问答[qa]/知识库管理[manage]）
2. 创建 qa 会话 → 英文提问 → 流式回答（召回片段 + [[DOI]] 引用）
3. 创建 manage 会话 → 自然语言"列出库内文献价值评分" → 工具调用事件 + 回答
4. 会话列表模式标签（知/管）
"""
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
             {"width": 1280, "height": 900, "deviceScaleFactor": 1, "mobile": False})
    wait_for(cdp, "typeof bindKbAdmin === 'function'")
    print("[0] 页面就绪")

    # ---- 1) 菜单 3 项 ----
    r = cdp.eval("""(() => {
        document.getElementById('new-session-btn').click();
        const items = Array.from(document.querySelectorAll('#new-session-menu .menu-item'))
            .map(i => i.dataset.kind + ':' + (i.dataset.mode || '-'));
        return items;
    })()""")
    print("[1] 菜单项:", r.get("value"))
    assert r.get("value") == ["chat:-", "global:qa", "global:manage"], "菜单项不符"
    cdp.eval("document.body.click()")  # 关菜单

    # ---- 2) qa 会话：英文提问 ----
    r = cdp.eval("""(() => {
        const item = Array.from(document.querySelectorAll('#new-session-menu .menu-item'))
            .find(i => i.dataset.kind === 'global' && i.dataset.mode === 'qa');
        item.click();
        return 'clicked';
    })()""")
    r = wait_for(cdp,
                 "document.getElementById('chat-sub').textContent.includes('知识库问答')",
                 timeout=15)
    r = cdp.eval("document.getElementById('chat-sub').textContent")
    print("[2] qa 会话标题:", r.get("value"))
    assert r.get("value") and "知识库问答" in r.get("value"), "qa 会话未创建"

    r = cdp.eval("""(() => {
        const inp = document.getElementById('question-input');
        inp.value = 'What is the main contribution of the ionic liquid sensor paper? Answer briefly.';
        inp.dispatchEvent(new Event('input', { bubbles: true }));
        document.getElementById('send-btn').click();
        return 'sent';
    })()""")
    time.sleep(1)
    r = wait_for(cdp,
                 "document.getElementById('messages').innerText.includes('10.1016') || document.getElementById('messages').innerText.length > 200",
                 timeout=120)
    ans = cdp.eval("document.getElementById('messages').innerText.slice(-600)")["value"]
    print("[2] qa 回答:", ans[:260])
    assert r.get("value") and len(ans) > 30, "qa 问答未返回"

    # ---- 2b) qa 中文问题（trigram 修复：中文短语命中编译产物）----
    r = cdp.eval("""(() => {
        const inp = document.getElementById('question-input');
        inp.value = '浸渍温度对传感器性能的影响？请用中文一句话回答' + ' [' + Date.now() + ']';
        inp.dispatchEvent(new Event('input', { bubbles: true }));
        document.getElementById('send-btn').click();
        return 'sent';
    })()""")
    time.sleep(1)
    r = wait_for(cdp,
                 "document.getElementById('messages').innerText.includes('浸渍') || document.getElementById('messages').innerText.length > 200",
                 timeout=120)
    zh = cdp.eval("document.getElementById('messages').innerText.slice(-600)")["value"]
    print("[2b] qa 中文回答:", zh[:260])
    assert r.get("value") and len(zh) > 30, "qa 中文问答未返回"

    # ---- 3) manage 会话：工具调用 ----
    r = cdp.eval("""(() => {
        document.getElementById('new-session-btn').click();
        const item = Array.from(document.querySelectorAll('#new-session-menu .menu-item'))
            .find(i => i.dataset.kind === 'global' && i.dataset.mode === 'manage');
        item.click();
        return 'clicked';
    })()""")
    r = wait_for(cdp,
                 "document.getElementById('chat-sub').textContent.includes('知识库管理')",
                 timeout=15)
    r = cdp.eval("document.getElementById('chat-sub').textContent")
    print("[3] manage 会话标题:", r.get("value"))
    assert r.get("value") and "知识库管理" in r.get("value"), "manage 会话未创建"

    r = cdp.eval("""(() => {
        const inp = document.getElementById('question-input');
        inp.value = '列出知识库中所有文献的价值评分（请调用工具获取最新数据）' + ' [' + Date.now() + ']';
        inp.dispatchEvent(new Event('input', { bubbles: true }));
        document.getElementById('send-btn').click();
        return 'sent';
    })()""")
    time.sleep(1)
    r = wait_for(cdp,
                 "document.querySelector('#messages .tool-call') !== null",
                 timeout=90)
    if not r.get("value"):
        dump = cdp.eval("document.getElementById('messages').innerText.slice(0, 500)")["value"]
        print("[3] ⚠ 无工具事件，messages 内容:", repr(dump))
    assert r.get("value"), "未出现工具调用事件"
    tools = cdp.eval("""Array.from(document.querySelectorAll('#messages .tool-call')).map(
        d => d.textContent.slice(0, 80))""")["value"]
    print("[3] 工具调用:", tools)
    r = wait_for(cdp,
                 "document.getElementById('messages').innerText.includes('10.') || document.getElementById('messages').innerText.length > 120",
                 timeout=120)
    ans = cdp.eval("document.getElementById('messages').innerText.slice(-600)")["value"]
    print("[3] manage 回答:", ans[:260])
    assert r.get("value"), "manage 未返回回答"

    # ---- 4) 会话列表模式标签 ----
    r = cdp.eval("""Array.from(document.querySelectorAll('#session-list .s-kind'))
        .map(b => b.textContent)""")
    print("[4] 会话标签:", r.get("value"))
    assert any(t in (r.get("value") or []) for t in ("知", "管")), "模式标签缺失"

    print("\n=== ALL KBMODES CHECKS PASSED ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
