#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: skill/src/paperparse/llm/client.py
功能: 可注入 AI 客户端抽象（用户需求 3/5 底层）：
      - AIProvider 协议：complete(prompt) -> str，返回模型输出
      - DefaultAI / HarnessAI：真实调用由外部 DeepSeek Harness 子代理承载；
        本模块默认提供可替换实现，测试用 FakeAI mock（不真实联网）
      - get_ai() 工厂：支持通过配置/环境注入自定义 provider
对外接口: AIProvider / HarnessAI / FakeAI / get_ai / set_ai
版本: v1.0.0 (2026-08-18)
版本历史:
  v1.0.0 初始版本
"""
from __future__ import annotations

import os

__all__ = ["AIProvider", "HarnessAI", "FakeAI", "get_ai", "set_ai"]

_MODULE_AI = None


class AIProvider:
    """[全局] AI 后端协议：实现 complete(prompt) -> str

    说明: 本 skill 的真实 AI 调用经 DeepSeek Harness Web GUI 子代理承载，
    因此主进程侧不直连任何 API key；任何 provider 只需实现 complete()。

    Usage 记账契约（可选）：provider 可在 complete() 返回前设置实例属性
    `self.last_usage = {"input": int, "output": int, "cache_hit": int}`（token 数），
    TrackingAI 包装器会优先读取真实 usage（含 prompt_cache_hit_tokens），
    未提供时回退字符估算。真实 Harness 后端 provider 应实现该契约。
    """

    def complete(self, prompt: str) -> str:
        raise NotImplementedError


class HarnessAI(AIProvider):
    """[全局] 经 DeepSeek Harness 子代理承载的 AI 后端。

    真实场景中由 Harness 调度子代理执行（上下文隔离）；此实现为占位桩，
    供主进程编排使用——实际调用由上层 harness 层完成，这里不发起网络请求。
    """

    name = "harness"

    def complete(self, prompt: str) -> str:
        # 占位：真实调用在 harness 子代理内完成
        raise NotImplementedError(
            "HarnessAI 需由 DeepSeek Harness 子代理承载；主进程请使用可注入 provider（如 FakeAI/mock）")


class FakeAI(AIProvider):
    """[全局] 测试用假 AI：返回预设输出（不联网）"""

    name = "fake"

    def __init__(self, responses: list[str] | None = None):
        self.responses = list(responses or [])
        self.calls: list[str] = []

    def complete(self, prompt: str) -> str:
        self.calls.append(prompt)
        if self.responses:
            return self.responses.pop(0)
        return "{}"


def get_ai() -> AIProvider:
    """[全局] 获取当前 AI provider（默认 FakeAI；可用 set_ai 注入真后端）"""
    global _MODULE_AI
    if _MODULE_AI is None:
        _MODULE_AI = FakeAI()
    return _MODULE_AI


def set_ai(provider: AIProvider | None) -> None:
    """[全局] 注入/重置 AI provider（None=重置为默认 FakeAI）"""
    global _MODULE_AI
    if provider is None:
        _MODULE_AI = None
    elif isinstance(provider, AIProvider):
        _MODULE_AI = provider
    else:
        # 兼容普通函数式 provider（complete(prompt)->str）
        class _Adapter(AIProvider):
            def complete(self, prompt: str) -> str:
                return provider(prompt) if callable(provider) else str(provider)

        _MODULE_AI = _Adapter()
