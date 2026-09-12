#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: skill/src/paperparse/__main__.py
功能: 支持 `python -m paperparse` 直接运行
对外接口: 无（入口转发）
版本: v1.0.0 (2026-08-19)
版本历史:
  v1.0.0 初始版本
"""
from paperparse.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
