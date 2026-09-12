# -*- coding: utf-8 -*-
"""Token 记账服务测试（V02）。"""
from __future__ import annotations

from app.services.usage_service import UsageService


def test_record_and_cost(settings, store):
    us = UsageService(store)
    r = us.record("engine", "deepseek", "deepseek-chat", 1000, 500, 0)
    # M5：默认单价为 0（未录入单价按 0 计价）→ 无 resolver 时费用 0
    assert abs(r["cost"] - 0.0) < 1e-9
    assert r["prompt_tokens"] == 1000 and r["completion_tokens"] == 500


def test_cache_hit_discount(settings, store):
    us = UsageService(store)
    # M5：默认单价为 0 → 缓存命中/未命中均按 0 计
    r = us.record("engine", "deepseek", "deepseek-chat", 1000, 0, 800)
    assert abs(r["cost"] - 0.0) < 1e-9


def test_summary_global_and_prefix(settings, store):
    us = UsageService(store)
    us.record("session:1", "deepseek", "m", 100, 50, 0)
    us.record("session:1", "deepseek", "m", 200, 100, 0)
    us.record("engine", "deepseek", "m", 1000, 500, 0)
    g = us.summary()
    assert g["calls"] == 3 and g["total_tokens"] == 1950
    s = us.summary("session:1")
    assert s["calls"] == 2 and s["prompt_tokens"] == 300
    assert s["completion_tokens"] == 150


# ---------------------------------------------------------------- T3：按供应商-模型组合计价
def test_record_with_price_resolver(settings, store):
    """resolver 按 (provider, model) 返回单价 → 按组合计价。"""
    us = UsageService(store, price_resolver=lambda pid, m: {
        "input_per_m": 3.0, "cached_input_per_m": 0.1, "output_per_m": 9.0})
    r = us.record("engine", "siliconflow", "deepseek-ai/DeepSeek-V4-Flash", 1000, 500, 0)
    expected = (1000 * 3.0 + 500 * 9.0) / 1e6
    assert abs(r["cost"] - expected) < 1e-9


def test_record_resolver_cache_hit(settings, store):
    """resolver 单价下缓存命中仍按 cached 价计。"""
    us = UsageService(store, price_resolver=lambda pid, m: {
        "input_per_m": 2.0, "cached_input_per_m": 0.2, "output_per_m": 8.0})
    r = us.record("engine", "x", "y", 1000, 0, 800)
    expected = (200 * 2.0 + 800 * 0.2) / 1e6
    assert abs(r["cost"] - expected) < 1e-9


def test_record_resolver_fallback(settings, store):
    """resolver 抛异常/缺字段 → 回落注入默认价(0)，记账不中断。"""
    def boom(pid, m):
        raise RuntimeError("resolver boom")
    us = UsageService(store, prices={"output_per_m": 2.0}, price_resolver=boom)
    r = us.record("engine", "deepseek", "deepseek-chat", 1000, 500, 0)
    expected = (1000 * 0.0 + 500 * 2.0) / 1e6   # input/cached 默认 0，output 用注入 2.0
    assert abs(r["cost"] - expected) < 1e-9
