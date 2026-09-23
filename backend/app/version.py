# -*- coding: utf-8 -*-
"""版本契约单一来源（见 `docs/VERSIONING.md`）。

- `APP_VERSION`：应用发布版本（backend + frontend + paperkb 一起发）。
  `pyproject.toml` 里有一份给打包用的副本，`backend/tests/test_version_contract.py`
  断言两者一致（防漂移）。
- `FRONTEND_VERSION`（`frontend/version.js`）由**前端资源自报**，后端经
  `read_frontend_version()` 读**实际随包发出的那份文件**——因此能发现"前后端版本打歪"
  （发布事故防线，见 `docs/RELEASE-CHECKLIST.md`）。
- `DATA_FORMAT` / `LAYOUT_VERSION`：**数据层概念，权威在 `paperkb.version`**
  （迁移 runner 与 KBStore 都要用，paperkb 不能反向依赖 backend）→ 此处 re-export。
- `MIN_READABLE_DATA_FORMAT`：代码能**自动迁移**的最旧数据格式。
  低于它的数据必须走离线工具，不做在线兼容。
"""
from __future__ import annotations

import re
from pathlib import Path

from paperkb.version import (DATA_FORMAT, LAYOUT_VERSION,  # noqa: F401
                             MIN_READABLE_DATA_FORMAT)

APP_VERSION = "1.4.0"

# 前端版本文件（相对 frontend/）：单一来源，禁止在别处再写一份前端版本号常量。
FRONTEND_VERSION_FILE = "version.js"
_FRONTEND_VERSION_RE = re.compile(
    r"""PAPERAGENT_FRONTEND_VERSION\s*=\s*["']([^"']+)["']""")


def read_frontend_version(frontend_dir: Path | str) -> str:
    """读随包前端的版本号；文件缺失/不可解析返回 ""（不抛，别让版本探测拖垮启动）。"""
    try:
        text = (Path(frontend_dir) / FRONTEND_VERSION_FILE).read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return ""
    m = _FRONTEND_VERSION_RE.search(text)
    return m.group(1) if m else ""


__all__ = ["APP_VERSION", "DATA_FORMAT", "MIN_READABLE_DATA_FORMAT", "LAYOUT_VERSION",
           "FRONTEND_VERSION_FILE", "read_frontend_version"]
