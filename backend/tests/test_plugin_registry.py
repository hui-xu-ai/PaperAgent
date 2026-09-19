# -*- coding: utf-8 -*-
"""插件系统测试（V05/V08）：扫描加载 / 启用禁用 / 事件驱动触发。"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from app.services.plugin_registry import PluginRegistry


@pytest.fixture()
def _tmp_plugin(tmp_path, monkeypatch):
    """创建一个临时测试插件，注入 USER_PLUGINS_DIR。"""
    import app.services.plugin_registry as mod
    pdir = tmp_path / "test_plugin"
    pdir.mkdir()
    (pdir / "plugin.yaml").write_text(
        "id: test-plugin\nname: Test Plugin\nversion: \"1.0\"\n"
        "description: Temporary test plugin\n", encoding="utf-8")
    (pdir / "plugin.py").write_text(textwrap.dedent("""\
        from app.services.plugin_base import Plugin

        class TestPlugin(Plugin):
            id = "test-plugin"
            name = "Test Plugin"
            version = "1.0"
            description = "Temporary test plugin"

            def on_register(self, app): return
            def on_paper_processed(self, paper_id, doc_json): return

        plugin = TestPlugin()
    """), encoding="utf-8")
    monkeypatch.setattr(mod, "USER_PLUGINS_DIR", tmp_path)
    return pdir


def test_plugin_registry_loads_test_plugin(store, _tmp_plugin):
    reg = PluginRegistry(store)
    ids = {p["id"] for p in reg.list_plugins()}
    assert "test-plugin" in ids
    meta = reg.get_plugin("test-plugin")
    assert meta.instance.name == "Test Plugin"
    assert meta.instance.version == "1.0"


def test_plugin_toggle_persists(store, _tmp_plugin):
    reg = PluginRegistry(store)
    reg.set_enabled("test-plugin", False)
    reg2 = PluginRegistry(store)
    assert reg2.get_plugin("test-plugin").enabled is False
    reg2.set_enabled("test-plugin", True)
    assert PluginRegistry(store).get_plugin("test-plugin").enabled is True


def test_task_done_event_triggers_plugin(store, _tmp_plugin, monkeypatch):
    """task_done 事件 → 启用插件 on_paper_processed 被调用（事件驱动链路）。"""
    from app.services.event_bus import EventBus

    bus = EventBus()
    reg = PluginRegistry(store, event_bus=bus)
    reg.subscribe()
    meta = reg.get_plugin("test-plugin")
    calls = []
    monkeypatch.setattr(meta.instance, "on_paper_processed",
                        lambda pid, doc: calls.append((pid, doc)))
    bus.publish("info", "task", "task_done", "done", {"paper_id": 7})
    assert calls == [(7, "")]
