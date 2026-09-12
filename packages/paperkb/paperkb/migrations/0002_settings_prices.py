# -*- coding: utf-8 -*-
"""0002：`settings.prices` 规范化为当前结构（消灭"读时兼容旧扁平格式"）。

背景（`docs/COMPAT-REGISTER.md` A1）：旧版把价格存成**扁平**三项
`{"input_per_m":…, "cached_input_per_m":…, "output_per_m":…}`；新版结构是
`{"by_provider_model": {"<provider>::<model>": {三项}}, "default": {三项}}`
且 M5 起 `default` 恒为 0（只按 per-model 计价）。旧实现让 `settings_service.get_prices`
在**读取时**兼容旧格式 —— 那是业务层的兼容分支（禁止）。

本迁移把它一次性规整：旧扁平值按 M5 语义丢弃（default 归 0），只保留规范形状；
这样业务层只需读当前格式。幂等：已是规范形状则不动；无 `prices` 行也跳过。
"""
from __future__ import annotations

import json
import sqlite3

VERSION = 2
NAME = "0002_settings_prices"
KEY = "prices"
PRICE_KEYS = ("input_per_m", "cached_input_per_m", "output_per_m")
CANONICAL = {"by_provider_model": {},
             "default": {k: 0.0 for k in PRICE_KEYS}}


def _schema_with_settings(conn) -> str | None:
    for s in [r[1] for r in conn.execute("PRAGMA database_list")] or ["main"]:
        try:
            row = conn.execute(f"SELECT 1 FROM {s}.sqlite_master WHERE type='table' "
                               f"AND name='settings'").fetchone()
        except sqlite3.Error:
            continue
        if row is not None:
            return s
    return None


def up(conn) -> None:
    schema = _schema_with_settings(conn)
    if schema is None:
        return
    row = conn.execute(f"SELECT value FROM {schema}.settings WHERE key=?", (KEY,)).fetchone()
    if row is None:
        return
    raw = row[0] if not isinstance(row, sqlite3.Row) else row["value"]
    try:
        data = json.loads(raw or "{}")
    except (ValueError, TypeError):
        data = {}
    if isinstance(data, dict) and ("by_provider_model" in data or "default" in data):
        return                              # 已是规范形状
    conn.execute(f"INSERT OR REPLACE INTO {schema}.settings(key, value) VALUES(?,?)",
                 (KEY, json.dumps(CANONICAL, ensure_ascii=False)))


def verify(conn) -> list[str]:
    schema = _schema_with_settings(conn)
    if schema is None:
        return []
    row = conn.execute(f"SELECT value FROM {schema}.settings WHERE key=?", (KEY,)).fetchone()
    if row is None:
        return []
    raw = row[0] if not isinstance(row, sqlite3.Row) else row["value"]
    try:
        data = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return [f"{KEY} 不是合法 JSON"]
    if not isinstance(data, dict) or not ({"by_provider_model", "default"} & set(data)):
        return [f"{KEY} 仍是旧扁平格式（读时兼容将被删除）"]
    return []
