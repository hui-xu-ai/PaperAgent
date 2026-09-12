# -*- coding: utf-8 -*-
"""obsidian-notes 内置插件：文献处理完成后自动生成三级笔记与索引。

验证插件系统扩展性的示范插件（V04 的 kb_service 在此插件化）。
注意：插件内使用绝对导入（from app.xxx import ...），动态加载无包上下文。
"""
from __future__ import annotations

from app.services.plugin_base import Plugin


class ObsidianNotesPlugin(Plugin):
    id = "obsidian-notes"
    name = "Obsidian 笔记生成"
    version = "1.0"
    description = "文献翻译完成后自动生成一级核心笔记/二级详细笔记与知识库索引"

    def __init__(self):
        # R4：不再于 on_register 内 import container 摸全局单例；改为可构造注入。
        # 当前 A1 收敛后本插件为惰性（on_paper_processed 已禁；知识库编译统一走
        # paperkb compile worker），_kb 仅供未来扩展（构造时注入）。
        self._kb = None

    def on_register(self, app) -> None:
        """注册时预留：依赖经构造注入，不指向 container 全局单例。"""
        return

    def on_paper_processed(self, paper_id: int, doc_json: str) -> None:
        """（A1 收敛：旧 kb_service.generate 写旧格式 _note.md，与新 paperkb 编译
        L1 冲突——旧 note 先写则新 compile 见文件存在即 skipped_existing。已禁用；
        知识库编译统一走 paperkb compile worker。保留钩子防插件系统行为变更。）"""
        return


plugin = ObsidianNotesPlugin()
