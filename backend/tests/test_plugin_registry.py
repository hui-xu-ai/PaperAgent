# -*- coding: utf-8 -*-
"""插件系统测试（V05/V08）：扫描加载 / 启用禁用 / 事件驱动触发。"""
from __future__ import annotations

from app.services.plugin_registry import PluginRegistry


def test_plugin_registry_loads_obsidian_notes(store):
    reg = PluginRegistry(store)
    ids = {p["id"] for p in reg.list_plugins()}
    assert "obsidian-notes" in ids
    meta = reg.get_plugin("obsidian-notes")
    assert meta.instance.name == "Obsidian 笔记生成"
    assert meta.instance.version == "1.0"


def test_plugin_toggle_persists(store):
    reg = PluginRegistry(store)
    reg.set_enabled("obsidian-notes", False)
    # 重新实例化（从 SQLite 读状态）
    reg2 = PluginRegistry(store)
    assert reg2.get_plugin("obsidian-notes").enabled is False
    reg2.set_enabled("obsidian-notes", True)
    assert PluginRegistry(store).get_plugin("obsidian-notes").enabled is True


def test_task_done_event_triggers_plugin(store, monkeypatch):
    """task_done 事件 → 启用插件 on_paper_processed 被调用（事件驱动链路）。"""
    from app.services.event_bus import EventBus

    bus = EventBus()
    reg = PluginRegistry(store, event_bus=bus)
    reg.subscribe()  # 订阅 task_done（container 启动时自动调用）
    meta = reg.get_plugin("obsidian-notes")
    calls = []
    monkeypatch.setattr(meta.instance, "on_paper_processed",
                        lambda pid, doc: calls.append((pid, doc)))
    bus.publish("info", "task", "task_done", "done", {"paper_id": 7})
    assert calls == [(7, "")]  # paper 7 不存在 → doc_json 空字符串
