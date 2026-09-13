# -*- coding: utf-8 -*-
"""前缀缓存命中的**多供应商**解析守卫（2026-09-13 用户实测报障回归）。

**报障**：「DeepSeek 能有缓存命中，智谱 glm-flash 和硅基流动的 DeepSeek 就没有缓存命中」。
**实测（直打三家 API，同前缀调两次）**：
  · DeepSeek 官方：`prompt_cache_hit_tokens=3840` + `prompt_tokens_details.cached_tokens=3840`；
  · 智谱 GLM：**只有** `prompt_tokens_details.cached_tokens=4224`，**没有** `prompt_cache_hit_tokens`
    ⇒ 旧实现只读后者 ⇒ **永远记 0**（本应用解析 bug，本文件锁死）；
  · 硅基流动：字段同 DeepSeek，但**有最小缓存块**：≈1000 token 前缀命中 0、≈4000 token 前缀命中 3840
    ⇒ 短前缀不命中是正常的（不是解析问题）。
"""
from __future__ import annotations

from app.services.llm_service import cache_hit_tokens


def test_deepseek_shape_prefers_dedicated_field():
    usage = {"prompt_tokens": 4013, "prompt_cache_hit_tokens": 3840,
             "prompt_tokens_details": {"cached_tokens": 3840}}
    assert cache_hit_tokens(usage) == 3840


def test_zhipu_shape_only_details_cached_tokens():
    """智谱（OpenAI 兼容）形状：没有 prompt_cache_hit_tokens，命中数在 details 里。"""
    usage = {"prompt_tokens": 4236, "completion_tokens": 8,
             "prompt_tokens_details": {"cached_tokens": 4224}}
    assert cache_hit_tokens(usage) == 4224, "智谱的缓存命中必须能读到（旧实现恒 0）"


def test_siliconflow_shape_zero_means_zero():
    """硅基流动短前缀确实不命中 ⇒ 如实返回 0（不能把"没命中"伪装成"读不到"）。"""
    usage = {"prompt_tokens": 1014, "prompt_cache_hit_tokens": 0,
             "prompt_cache_miss_tokens": 1014, "prompt_tokens_details": {"cached_tokens": 0}}
    assert cache_hit_tokens(usage) == 0


def test_sdk_object_shape():
    """OpenAI SDK 返回的是对象（属性访问）而非 dict。"""
    class _Det:
        cached_tokens = 1024

    class _Usage:
        prompt_tokens = 1082
        prompt_tokens_details = _Det()

    assert cache_hit_tokens(_Usage()) == 1024

    class _Usage2:
        prompt_tokens = 100
        prompt_cache_hit_tokens = 64

    assert cache_hit_tokens(_Usage2()) == 64


def test_anthropic_style_and_robustness():
    assert cache_hit_tokens({"cache_read_input_tokens": 512}) == 512
    assert cache_hit_tokens({"cached_tokens": 128}) == 128
    # 缺字段 / 脏值 / None：一律 0，绝不抛
    assert cache_hit_tokens({}) == 0
    assert cache_hit_tokens(None) == 0
    assert cache_hit_tokens({"prompt_cache_hit_tokens": "bad"}) == 0
    assert cache_hit_tokens({"prompt_tokens_details": {"cached_tokens": None}}) == 0
