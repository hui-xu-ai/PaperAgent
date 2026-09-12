# -*- coding: utf-8 -*-
"""插件协议（V05 v1）：插件继承 Plugin，实现可选生命周期钩子。

钩子：
- on_register(app)：应用启动时注册路由/资源
- on_paper_processed(paper_id, doc_json)：文献翻译流水线完成后触发（事件驱动）
- on_settings_change(key)：设置变更通知

插件结构（目录内 plugin.yaml + plugin.py）：
    plugins/<name>/plugin.yaml    # id/name/version/description
    plugins/<name>/plugin.py      # 定义 plugin = MyPlugin()
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 内置插件目录（相对本文件：backend/app/plugins/）
BUILTIN_PLUGINS_DIR = Path(__file__).resolve().parent.parent / "plugins"


class Plugin:
    """插件基类：子类覆写钩子方法。"""

    id: str = ""
    name: str = ""
    version: str = "0.1"
    description: str = ""

    def on_register(self, app: Any) -> None:
        """应用启动注册阶段（可挂路由/初始化资源）。"""

    def on_paper_processed(self, paper_id: int, doc_json: str) -> None:
        """文献翻译流水线完成（paper_id 对应 papers 记录）。"""

    def on_settings_change(self, key: str) -> None:
        """设置变更通知。"""


class PluginMeta:
    """插件元数据 + 实例。"""

    def __init__(self, id: str, name: str, version: str, description: str,
                 path: Path, instance: Plugin, enabled: bool = True):
        self.id = id
        self.name = name
        self.version = version
        self.description = description
        self.path = path
        self.instance = instance
        self.enabled = enabled

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "version": self.version,
                "description": self.description, "enabled": self.enabled,
                "path": str(self.path)}
