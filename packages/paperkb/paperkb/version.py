# -*- coding: utf-8 -*-
"""数据层版本契约（**数据的版本归数据层**，见 `docs/VERSIONING.md` §1）。

放在 paperkb 而不是 backend 的原因：`DATA_FORMAT` 描述的是**库结构**，
迁移 runner（`paperkb.migrations`）与 `KBStore.init_schema` 都要用它，
而 paperkb 不能反向依赖 backend。backend 的 `app/version.py` 再 re-export。
"""
from __future__ import annotations

# 数据格式版本：**只有写了迁移才 +1**
#   v1 → v2（2026-09-12）：settings.prices 规范化为 {by_provider_model, default}；
#                          papers_meta 主键 doi→rid 收编进迁移层（0003）
DATA_FORMAT = 2
MIN_READABLE_DATA_FORMAT = 1
LAYOUT_VERSION = "v1"

__all__ = ["DATA_FORMAT", "MIN_READABLE_DATA_FORMAT", "LAYOUT_VERSION"]
