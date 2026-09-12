#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: skill/src/paperparse/__init__.py
功能: 包入口：版本号
对外接口: __version__
版本: v2.3.0 (2026-08-20)
版本历史:
  v2.3.0 mineru-v4 高精度链路修复（.env 自动查找/parse_zip 解压 full.md/variants 路径）
         + 摘要去重（OCR 缺陷数字噪声）+ 乱码修复（KNOWN_FIXES/本地重提取）
         + 翻译 token 优化（前缀缓存/模板外置/字符切批/护栏/usage 契约）
"""
__version__ = "2.3.0"
