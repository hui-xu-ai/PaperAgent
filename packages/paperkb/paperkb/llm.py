# -*- coding: utf-8 -*-
"""LLM 调用抽象（编译/翻译统一入口，按 KB-DESIGN 第 12 章）。

- paperkb 不直连供应商：调用方（backend llm_service）注入实现
- context 分组：translate/compile 一组（共用上下文前缀，缓存命中），chat 一组（隔离）
"""
from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class LLMClient(Protocol):
    """LLM 调用协议（compile/translate 上下文分组）。"""

    def complete(self, prompt: str, context: str = "compile") -> str:
        """单次补全。context 用于限流/预算分组；translate+compile 应共享前缀。"""
        ...


class FakeLLM:
    """测试用：按顺序返回预设响应。"""

    def __init__(self, responses: list[str] | None = None):
        self._responses = list(responses or [])
        self._calls: list[tuple[str, str]] = []

    def complete(self, prompt: str, context: str = "compile") -> str:
        self._calls.append((context, prompt))
        if not self._responses:
            return "{}"
        return self._responses.pop(0)


_client: LLMClient | None = None


def configure_llm(client: LLMClient | None) -> None:
    """注入 LLM 客户端（backend 启动时经 kbmeta_service 调用）。"""
    global _client
    _client = client
    logger.info("paperkb LLM 客户端已注入: %s", type(client).__name__ if client else "None")


def get_llm() -> LLMClient:
    if _client is None:
        raise RuntimeError("paperkb LLM 未注入：请先 configure_llm(client)")
    return _client


def llm_ready() -> bool:
    return _client is not None
