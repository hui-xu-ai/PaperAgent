# -*- coding: utf-8 -*-
"""paperkb：PaperAgent 知识库算法包（卡帕西 LLM Wiki 风格）。

独立于 paperparse（纯 PDF 解析）；经 paperkb.api 门面被 backend/agent 调用。
设计要点（KB-DESIGN.md v0.6 第 12 章）：
- 唯一门面 api.py；接口/文件路径/API 调用三条维护线
- 路径不硬编码：所有根路径经 Roots 注入
- 元数据唯一权威 = bib（papers_meta，按 DOI 查询）
"""
__version__ = "0.1.0"
