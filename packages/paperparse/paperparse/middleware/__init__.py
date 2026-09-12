#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: skill/src/paperparse/middleware/__init__.py
功能: 中间调度层包入口：统一导出契约/错误/审计
对外接口: schema / errors / audit 模块
版本: v1.0.0 (2026-08-19)
版本历史:
  v1.0.0 初始版本
"""
from paperparse.middleware import audit, errors, schema

__all__ = ["schema", "errors", "audit"]
