# -*- coding: utf-8 -*-
"""配置逻辑测试：解析通道自动选择。"""
from __future__ import annotations

import pytest

from app.config import Settings


def test_resolve_parser_auto_without_key():
    """批2 政策（用户拍板）：无 Key ⇒ 解析不可用（返回空串），**不再回落** v1 免费通道。"""
    s = Settings(mineru_api_key="", mineru_parser="auto")
    assert s.resolve_mineru_parser() == ""


def test_resolve_parser_auto_with_key():
    s = Settings(mineru_api_key="sk-mineru", mineru_parser="auto")
    assert s.resolve_mineru_parser() == "mineru-v4"  # 精准


def test_resolve_parser_explicit_override():
    """显式配置也要有 Key 才成立（批2：没有 Key 就没有可用解析通道）。"""
    s = Settings(mineru_api_key="", mineru_parser="mineru-v4")
    assert s.resolve_mineru_parser() == ""
    s2 = Settings(mineru_api_key="sk-x", mineru_parser="mineru-v4")
    assert s2.resolve_mineru_parser() == "mineru-v4"


def test_default_port():
    import os
    s = Settings()
    assert s.port == 8900
    # deepseek_model 为类属性（import 时绑定）：应与 .env/环境值一致（本机 .env 为
    # deepseek-ai/DeepSeek-V4-Flash），无 .env 时回落默认 deepseek-chat
    assert s.deepseek_model == os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")


def test_env_mineru_key_not_swallowed():
    """P2-A 回归：.env 中 MINERU_API_KEY 必须真实可加载（曾被换行损坏吞进注释行）。

    该 bug 曾导致精准解析（mineru-v4）永不触发，只能走 v1 免费低精度。
    """
    from pathlib import Path
    from dotenv import dotenv_values

    env_path = Path(__file__).resolve().parents[2] / ".env"  # 项目根 .env
    if not env_path.exists():
        pytest.skip("无 .env（CI/裸仓库），跳过真实文件回归")
    d = dotenv_values(env_path)
    key = (d.get("MINERU_API_KEY") or "").strip()
    assert key.startswith("sk-"), (
        "MINERU_API_KEY 未从 .env 加载——检查是否被注释吞掉或编码损坏（P2-A）")
    assert len(key) > 30, "MINERU_API_KEY 疑似被截断"


# ------------------------------------------------------- 批1：MinerU 运行期实时取值
def test_live_mineru_key_prefers_environ(monkeypatch):
    """设置中心保存 MinerU 配置后写 os.environ ⇒ 实时取值必须优先 os.environ（而非启动快照）。"""
    from app.config import live_mineru_key
    monkeypatch.setenv("MINERU_API_KEY", "sk-live")
    assert live_mineru_key("sk-snapshot") == "sk-live"


def test_live_mineru_key_empty_does_not_fall_back(monkeypatch):
    """显式清空（键存在但为空）不得回退快照——否则"删掉 Key 回落免费通道"永不生效。"""
    from app.config import live_mineru_key
    monkeypatch.setenv("MINERU_API_KEY", "")
    assert live_mineru_key("sk-snapshot") == ""


def test_live_mineru_key_absent_falls_back(monkeypatch):
    from app.config import live_mineru_key
    monkeypatch.delenv("MINERU_API_KEY", raising=False)
    assert live_mineru_key("sk-snapshot") == "sk-snapshot"


def test_resolve_live_mineru_parser(monkeypatch):
    """批2 政策：有 Key → mineru-v4；无 Key → ""（解析不可用，不再回落免费/本地通道）。"""
    from app.config import resolve_live_mineru_parser
    monkeypatch.setenv("MINERU_PARSER", "auto")
    monkeypatch.setenv("MINERU_API_KEY", "sk-x")
    assert resolve_live_mineru_parser() == "mineru-v4"       # 有 key → 精准
    monkeypatch.setenv("MINERU_API_KEY", "")
    assert resolve_live_mineru_parser() == ""                # 无 key → 不可用
    monkeypatch.setenv("MINERU_PARSER", "mineru-v4")
    assert resolve_live_mineru_parser() == ""                # 显式指定也需 Key
    monkeypatch.setenv("MINERU_API_KEY", "sk-x")
    assert resolve_live_mineru_parser() == "mineru-v4"


def test_mineru_ready_follows_live_key(monkeypatch):
    """硬门禁判据：实时 Key 非空 = 就绪；显式清空 = 不就绪（不回退启动快照）。"""
    from app.config import mineru_ready
    monkeypatch.setenv("MINERU_API_KEY", "sk-live")
    assert mineru_ready("sk-snapshot") is True
    monkeypatch.setenv("MINERU_API_KEY", "")
    assert mineru_ready("sk-snapshot") is False
    monkeypatch.delenv("MINERU_API_KEY", raising=False)
    assert mineru_ready("sk-snapshot") is True               # 无该键 → 用快照
    assert mineru_ready("") is False
