#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: tests/test_errors.py
功能: T2 错误体系单元测试（注册表完整性 / 信封 / 未知异常包装 / 动态注册）
对外接口: 无（测试）
版本: v1.0.0 (2025-xx-xx)
版本历史:
  v1.0.0 初始版本
"""
import pytest

from paperparse.middleware.errors import (
    ERROR_REGISTRY,
    PaperError,
    register_code,
    to_envelope,
    wrap_unknown,
)


def test_registry_codes_unique_and_complete():
    seen = set()
    for code, spec in ERROR_REGISTRY.items():
        assert code not in seen, f"错误码重复: {code}"
        seen.add(code)
        for key in ("severity", "retryable", "user_message", "recovery", "ai_hint"):
            assert key in spec, f"{code} 缺少 {key}"


def test_envelope_fields_and_template():
    err = PaperError("PAPER-0011", stage="S1", detail={"limit": 1000})
    env = to_envelope(err)
    assert env.code == "PAPER-0011"
    assert env.severity == "error"
    assert env.retryable is True
    assert env.stage == "S1"
    assert "1000" in env.user_message  # 模板渲染
    assert env.ai_detail["hint"]


def test_path_template():
    err = PaperError("PAPER-0001", path=r"D:\missing.pdf")
    env = to_envelope(err)
    assert "missing.pdf" in env.user_message
    assert env.ai_detail["path"] == r"D:\missing.pdf"


def test_unknown_code_raises():
    with pytest.raises(ValueError):
        PaperError("PAPER-XXXX")


def test_wrap_unknown():
    err = wrap_unknown(RuntimeError("boom"), stage="S2")
    assert err.code == "PAPER-9999"
    env = to_envelope(err)
    assert env.ai_detail["exc_type"] == "RuntimeError"
    assert env.ai_detail["exc_message"] == "boom"


def test_register_code():
    register_code("PAPER-0700", severity="warning", retryable=False,
                  user_message="测试消息", recovery="无", ai_hint="h")
    assert "PAPER-0700" in ERROR_REGISTRY
    with pytest.raises(ValueError):
        register_code("BAD", severity="error", retryable=False,
                      user_message="", recovery="")
