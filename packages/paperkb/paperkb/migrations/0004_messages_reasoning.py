# -*- coding: utf-8 -*-
"""0004：`chat.messages` 补 `reasoning_content` 列（思考模式思维链落库）。

背景：思考模式改造（AI 检索/思维链回传）把 `reasoning_content` 加进了
`store.py` 的 `CREATE TABLE IF NOT EXISTS chat.messages`，但**存量库**的表
早已存在，`IF NOT EXISTS` 不会补列，也没有对应迁移 —— 结果所有会话模式在
`add_message` 处抛 `sqlite3.OperationalError: table messages has no column
named reasoning_content`（SSE 流在首个事件前中断，前端表现为"（无回答）"）。

幂等：表不存在（全新安装，稍后由 Store._init_db 建全列）或列已存在 → 不动。
"""
from __future__ import annotations

import sqlite3

VERSION = 3
NAME = "0004_messages_reasoning"


def _messages_exists(conn) -> bool:
    row = conn.execute(
        "SELECT 1 FROM chat.sqlite_master WHERE type='table' AND name='messages'"
    ).fetchone()
    return row is not None


def _columns(conn) -> set[str]:
    try:
        return {r[1] for r in conn.execute("PRAGMA chat.table_info(messages)")}
    except sqlite3.Error:
        return set()


def up(conn) -> None:
    if not _messages_exists(conn):
        return
    if "reasoning_content" in _columns(conn):
        return
    conn.execute("ALTER TABLE chat.messages ADD COLUMN reasoning_content TEXT")


def verify(conn) -> list[str]:
    if not _messages_exists(conn):
        return []
    missing = {"reasoning_content"} - _columns(conn)
    if missing:
        return [f"chat.messages 缺列: {sorted(missing)}"]
    return []
