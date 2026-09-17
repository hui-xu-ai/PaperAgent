# -*- coding: utf-8 -*-
"""编译模块（查询缓存 + 主题编译）。"""
from .query_cache import cache_get, cache_set, cache_clear, cache_stats
from .topic_compiler import compile_topics, list_topics, get_topic

__all__ = [
    "cache_get", "cache_set", "cache_clear", "cache_stats",
    "compile_topics", "list_topics", "get_topic",
]
