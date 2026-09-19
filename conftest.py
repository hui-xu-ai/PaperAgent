# -*- coding: utf-8 -*-
"""pytest 根配置：把临时目录指向项目本地 .tmp_test/，绕过 Windows 系统 TEMP 权限问题。"""
from __future__ import annotations

import os
from pathlib import Path

_LOCAL_TMP = Path(__file__).resolve().parent / ".tmp_test"
_LOCAL_TMP.mkdir(exist_ok=True)
os.environ["TMPDIR"] = str(_LOCAL_TMP)
os.environ["TEMP"] = str(_LOCAL_TMP)
os.environ["TMP"] = str(_LOCAL_TMP)
