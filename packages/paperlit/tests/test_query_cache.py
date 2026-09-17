# -*- coding: utf-8 -*-
"""paperlit P7 测试：查询缓存。"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from paperlit.config import Roots
from paperlit.db import LitStore
from paperlit.compile.query_cache import (
    cache_get, cache_set, cache_clear, cache_stats, _hash_query,
)


@pytest.fixture
def lit_store(tmp_path):
    roots = Roots(data_dir=tmp_path / "data", lit_dir=tmp_path / "lit")
    roots.ensure()
    store = LitStore(roots)
    store.init_schema()
    return store


class TestHashQuery:
    def test_deterministic(self):
        assert _hash_query("battery") == _hash_query("battery")

    def test_case_insensitive(self):
        assert _hash_query("Battery") == _hash_query("battery")

    def test_strips_whitespace(self):
        assert _hash_query("  battery  ") == _hash_query("battery")

    def test_different_queries(self):
        assert _hash_query("battery") != _hash_query("electrolyte")


class TestCacheSetGet:
    def test_set_and_get(self, lit_store):
        cache_set(lit_store, "solid electrolyte", ["10.1/a", "10.1/b"])
        result = cache_get(lit_store, "solid electrolyte")
        assert result == ["10.1/a", "10.1/b"]

    def test_get_missing(self, lit_store):
        result = cache_get(lit_store, "nonexistent")
        assert result is None

    def test_overwrite(self, lit_store):
        cache_set(lit_store, "battery", ["10.1/a"])
        cache_set(lit_store, "battery", ["10.1/b", "10.1/c"])
        result = cache_get(lit_store, "battery")
        assert result == ["10.1/b", "10.1/c"]

    def test_case_insensitive_get(self, lit_store):
        cache_set(lit_store, "Battery", ["10.1/a"])
        result = cache_get(lit_store, "battery")
        assert result == ["10.1/a"]


class TestCacheTTL:
    def test_expired_returns_none(self, lit_store):
        cache_set(lit_store, "battery", ["10.1/a"])
        old_time = (datetime.now() - timedelta(hours=25)).isoformat()
        with lit_store._conn() as conn:
            conn.execute(
                "UPDATE query_cache SET created_at=? WHERE query_hash=?",
                (old_time, _hash_query("battery"))
            )
        result = cache_get(lit_store, "battery", ttl_hours=24)
        assert result is None

    def test_not_expired(self, lit_store):
        cache_set(lit_store, "battery", ["10.1/a"])
        result = cache_get(lit_store, "battery", ttl_hours=24)
        assert result == ["10.1/a"]

    def test_custom_ttl(self, lit_store):
        cache_set(lit_store, "battery", ["10.1/a"])
        old_time = (datetime.now() - timedelta(hours=2)).isoformat()
        with lit_store._conn() as conn:
            conn.execute(
                "UPDATE query_cache SET created_at=? WHERE query_hash=?",
                (old_time, _hash_query("battery"))
            )
        assert cache_get(lit_store, "battery", ttl_hours=1) is None
        assert cache_get(lit_store, "battery", ttl_hours=5) == ["10.1/a"]


class TestCacheClear:
    def test_clear(self, lit_store):
        cache_set(lit_store, "battery", ["10.1/a"])
        cache_set(lit_store, "electrolyte", ["10.1/b"])
        count = cache_clear(lit_store)
        assert count == 2
        assert cache_get(lit_store, "battery") is None

    def test_clear_empty(self, lit_store):
        count = cache_clear(lit_store)
        assert count == 0


class TestCacheStats:
    def test_stats(self, lit_store):
        assert cache_stats(lit_store)["total_entries"] == 0
        cache_set(lit_store, "battery", ["10.1/a"])
        assert cache_stats(lit_store)["total_entries"] == 1
        cache_set(lit_store, "electrolyte", ["10.1/b"])
        assert cache_stats(lit_store)["total_entries"] == 2
