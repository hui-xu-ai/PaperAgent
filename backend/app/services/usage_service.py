# -*- coding: utf-8 -*-
"""Token 记账服务（V02）：真实 usage 落库 + 费用估算 + 异常模式审计。

- 单价表可配置（V03 设置中心接入；默认 DeepSeek 官方价）
- 审计：单 context 高频调用（>上限）→ 事件告警（配合 TokenGuard 拦截）
- 全局/会话统计 → 前端标签栏
"""
from __future__ import annotations

import logging

from .event_bus import EventBus
from .store import Store

logger = logging.getLogger(__name__)

# 默认单价（元 / 百万 token）。M5：取消全局默认单价——未录入单价的模型按 0 计价。
DEFAULT_PRICES = {
    "input_per_m": 0.0,         # 输入（缓存未命中）
    "cached_input_per_m": 0.0,  # 输入（前缀缓存命中）
    "output_per_m": 0.0,        # 输出（completion，不含缓存）
}


class UsageService:
    def __init__(self, store: Store, event_bus: EventBus | None = None,
                 prices: dict | None = None, price_resolver=None):
        self.store = store
        self.event_bus = event_bus
        self.prices = {**DEFAULT_PRICES, **(prices or {})}
        # T3：按 (provider_id, model) 查三项单价的 resolver（容器注入 settings_service.get_prices_for）
        self._price_resolver = price_resolver

    def set_price_resolver(self, resolver) -> None:
        """T3：绑定按 (provider, model) 查单价的 resolver（settings_service.get_prices_for）。"""
        self._price_resolver = resolver

    def _price_lookup(self, provider: str, model: str) -> dict:
        """取计价单价：resolver 命中（provider+model）→ 注入的全局价 → DEFAULT_PRICES。
        resolver 异常不阻塞记账（回落注入价）。"""
        if self._price_resolver:
            try:
                p = self._price_resolver(provider or "", model or "")
                if isinstance(p, dict):
                    return {**DEFAULT_PRICES, **{k: p[k] for k in DEFAULT_PRICES if k in p}}
            except Exception:  # noqa: BLE001 - resolver 失败不影响记账
                logger.warning("price_resolver 查询失败（%s/%s），回落注入单价",
                               provider, model, exc_info=True)
        return self.prices

    # ---------------------------------------------------------- 记录
    def record(self, context: str, provider: str, model: str,
               prompt_tokens: int, completion_tokens: int,
               cache_hit_tokens: int = 0) -> dict:
        """记录一次 LLM 调用（含费用估算），返回记账明细。"""
        cost = self._estimate_cost(prompt_tokens, completion_tokens, cache_hit_tokens,
                                   provider, model)
        self.store.record_llm_usage(context, provider, model,
                                    int(prompt_tokens or 0),
                                    int(completion_tokens or 0),
                                    int(cache_hit_tokens or 0), cost)
        if self.event_bus:
            self.event_bus.publish(
                "info", "usage", "llm_call",
                f"{context} [{model}]: 输入 {prompt_tokens}（缓存命中 {cache_hit_tokens}）"
                f" 输出 {completion_tokens} 约 ¥{cost:.4f}",
                {"context": context, "prompt": prompt_tokens,
                 "completion": completion_tokens, "cache_hit": cache_hit_tokens,
                 "cost": cost})
        return {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                "cache_hit_tokens": cache_hit_tokens, "cost": cost}

    def _estimate_cost(self, prompt_tokens: int, completion_tokens: int,
                       cache_hit_tokens: int, provider: str = "",
                       model: str = "") -> float:
        """按 provider+model 查对应单价计价（T3：未匹配回落全局默认）。"""
        p = self._price_lookup(provider, model)
        miss = max(0, prompt_tokens - cache_hit_tokens)
        cost = (miss * p["input_per_m"] + cache_hit_tokens * p["cached_input_per_m"]
                + completion_tokens * p["output_per_m"]) / 1_000_000
        return round(cost, 6)

    # ---------------------------------------------------------- 统计
    def summary(self, context_prefix: str | None = None) -> dict:
        """全局或某 context 前缀的 token/费用汇总（前端标签栏数据源）。"""
        return self.store.usage_summary(context_prefix)

    def recent(self, limit: int = 50) -> list[dict]:
        return self.store.recent_usage(limit)

    def reset(self, context_prefix: str | None = None) -> dict:
        """P5 点2：清零 token 用量（可只清某会话，缺省清全局），返回清零后汇总。"""
        deleted = self.store.clear_usage(context_prefix)
        summary = self.summary(context_prefix)
        summary["deleted"] = deleted
        return summary

    # ---------------------------------------------------------- 审计（红线）
    def audit_call(self, context: str, call_index: int,
                   max_calls: int = 5) -> None:
        """调用次数审计：超过阈值发 warning 事件（配合 TokenGuard 硬拦截）。"""
        if call_index > max_calls and self.event_bus:
            self.event_bus.publish(
                "warning", "usage", "audit",
                f"{context} 调用次数异常（第 {call_index} 次 > {max_calls}），"
                f"请检查是否有死循环/重复调用", {"context": context})
