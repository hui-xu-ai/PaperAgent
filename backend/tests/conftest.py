# -*- coding: utf-8 -*-
"""pytest 共享 fixtures。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# backend/ 入 path（以 conftest.py 所在目录为基准）
BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# 测试 fixture：解析产物（git 入库，稳定；不依赖引擎输出目录/外部文件）
ENGINE_DOC = Path(__file__).resolve().parent / "fixtures" / "document.json"


class FakeChat:
    """测试用对话通道：固定回答，记录调用（兼容 context/effort 签名）。

    - `stream_events`：真实通道形态（`{"type":"delta"|"reasoning","text":...}`）；
    - `stream`：向后兼容的纯文本形态（委托给 stream_events）；
    - `calls`：[(context, messages)]（既有断言依赖该形状，不改成三元组）；
    - `efforts`：每次调用透传的 effort（None = 不发 reasoning_effort）。
    """

    def __init__(self, responses: list[str] | None = None):
        self.responses = list(responses or ["这是基于论文片段的测试回答。"])
        self.calls: list[tuple[str, list[dict]]] = []
        self.efforts: list[str | None] = []

    def stream_events(self, context: str, messages: list[dict[str, str]],
                      effort: str | None = None):
        self.calls.append((context, messages))
        self.efforts.append(effort)
        yield {"type": "delta", "text": self.responses[0]}

    def stream(self, context: str, messages: list[dict[str, str]],
               effort: str | None = None):
        for ev in self.stream_events(context, messages, effort=effort):
            if ev["type"] == "delta":
                yield ev["text"]

    def complete(self, context: str, messages: list[dict[str, str]]):
        self.calls.append((context, messages))
        return self.responses[0]


@pytest.fixture
def settings(tmp_path):
    """最小 Settings（临时 db 与输出目录）。

    2026-09-12 批2：带 `mineru_api_key` —— 解析已有**硬门禁**（无 Key 直接拒绝），
    测试必须与宿主机 .env 无关地保持"Key 已配置"状态（`config.mineru_ready` 在
    os.environ 无该键时回退本快照值），否则同一套测试在有/无 .env 的机器上结果不同。
    """
    from app.config import Settings
    return Settings(
        db_path=str(tmp_path / "test.db"),
        engine_work_root=str(tmp_path / "output"),
        engine_out_root=str(tmp_path / "backup"),
        engine_input_root=str(tmp_path / "input"),
        chat_history_limit=30,
        answer_cache_size=256,
        query_limit_chars=1500,
        mineru_api_key="sk-test-mineru",
    )


@pytest.fixture
def store(settings):
    from app.services.store import Store
    return Store(settings.db_path)


@pytest.fixture
def engine(settings):
    from app.services.engine_service import EngineService
    return EngineService(settings)


@pytest.fixture
def fake_chat():
    return FakeChat()
