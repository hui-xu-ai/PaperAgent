# -*- coding: utf-8 -*-
"""插件注册表（V05）：扫描内置/用户插件目录 + 生命周期 + 启用/禁用。

- 内置：backend/app/plugins/<name>/（plugin.yaml + plugin.py，module 定义 plugin=...）
- 用户：APP_DATA_DIR/plugins/<name>/（可扩展）
- 启用状态存 SQLite（plugins 表）；默认启用
- 事件驱动：订阅 event_bus 的 task_done → 通知启用插件的 on_paper_processed
"""
from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

import yaml

from ..config import APP_DATA_DIR
from .event_bus import EventBus
from .plugin_base import BUILTIN_PLUGINS_DIR, Plugin, PluginMeta
from .store import Store

logger = logging.getLogger(__name__)

USER_PLUGINS_DIR = APP_DATA_DIR / "plugins"


class PluginRegistry:
    def __init__(self, store: Store, event_bus: EventBus | None = None):
        self.store = store
        self.event_bus = event_bus
        self._plugins: dict[str, PluginMeta] = {}
        self._scan()

    # ---------------------------------------------------------- 扫描
    def _scan(self) -> None:
        dirs = [BUILTIN_PLUGINS_DIR, USER_PLUGINS_DIR]
        for base in dirs:
            if not base.exists():
                continue
            for pdir in sorted(base.iterdir()):
                if not pdir.is_dir() or pdir.name.startswith("__"):
                    continue
                try:
                    meta = self._load_plugin_dir(pdir)
                    if meta:
                        self._plugins[meta.id] = meta
                except Exception as e:  # noqa: BLE001 - 坏插件跳过
                    logger.warning("插件加载失败 %s: %s", pdir.name, e)
        logger.info("插件注册表: %d 个插件", len(self._plugins))

    def _load_plugin_dir(self, pdir: Path) -> PluginMeta | None:
        yaml_path = pdir / "plugin.yaml"
        py_path = pdir / "plugin.py"
        if not (yaml_path.exists() and py_path.exists()):
            return None
        info = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        plugin_id = str(info.get("id") or pdir.name)
        # 动态导入 plugin.py（模块名用插件 id 避免冲突）
        mod_name = f"paperagent_plugin_{plugin_id.replace('-', '_')}"
        spec = importlib.util.spec_from_file_location(mod_name, py_path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        instance: Plugin = getattr(module, "plugin", None)
        if instance is None:
            raise ValueError(f"{pdir.name}/plugin.py 未定义 plugin 实例")
        instance.id = instance.id or plugin_id
        instance.name = info.get("name") or instance.name
        instance.version = str(info.get("version") or instance.version)
        instance.description = info.get("description") or instance.description
        enabled = self.store.get_plugin_enabled(plugin_id, default=True)
        return PluginMeta(plugin_id, instance.name, instance.version,
                          instance.description, pdir, instance, enabled)

    # ---------------------------------------------------------- 查询
    def list_plugins(self) -> list[dict]:
        return [m.to_dict() for m in self._plugins.values()]

    def get_plugin(self, plugin_id: str) -> PluginMeta | None:
        return self._plugins.get(plugin_id)

    # ---------------------------------------------------------- 启用/禁用
    def set_enabled(self, plugin_id: str, enabled: bool) -> dict:
        meta = self._plugins.get(plugin_id)
        if not meta:
            raise KeyError(f"插件不存在: {plugin_id}")
        meta.enabled = enabled
        self.store.set_plugin_enabled(plugin_id, enabled)
        if self.event_bus:
            self.event_bus.publish("info", "plugin", "plugin_toggle",
                                   f"插件 {meta.name} {'启用' if enabled else '禁用'}",
                                   {"plugin_id": plugin_id, "enabled": enabled})
        return meta.to_dict()

    # ---------------------------------------------------------- 生命周期
    def rescan(self) -> dict:
        """重新扫描插件目录（U10：安装新 Skill 后调用）。"""
        self._plugins.clear()
        self._scan()
        return {"count": len(self._plugins), "plugins": self.list_plugins()}

    def register_all(self, app) -> None:
        """应用启动：on_register（仅启用插件）。"""
        for meta in self._plugins.values():
            if meta.enabled:
                try:
                    meta.instance.on_register(app)
                except Exception as e:  # noqa: BLE001
                    logger.warning("插件 on_register 失败 %s: %s", meta.id, e)

    def _on_task_done(self, event: dict) -> None:
        """task_done 事件 → 通知启用插件（on_paper_processed）。"""
        paper_id = event.get("data", {}).get("paper_id")
        if not paper_id:
            return
        paper = self.store.get_paper(paper_id)
        doc_json = (paper or {}).get("doc_json", "")
        for meta in self._plugins.values():
            if not meta.enabled:
                continue
            try:
                meta.instance.on_paper_processed(paper_id, doc_json)
            except Exception as e:  # noqa: BLE001 - 插件失败不阻塞
                logger.warning("插件 on_paper_processed 失败 %s: %s", meta.id, e)

    def subscribe(self) -> None:
        if self.event_bus:
            self.event_bus.on("task_done", self._on_task_done)
