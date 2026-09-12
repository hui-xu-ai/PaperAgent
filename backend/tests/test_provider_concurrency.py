# -*- coding: utf-8 -*-
"""供应商并发控制（T）：仅支持的供应商（如 DeepSeek）允许并行；GLM/未知 → 串行。"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.services.settings_service import (
    CONCURRENT_PIPELINE_WORKERS,
    SettingsService,
)
from app.services.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "t.db"))


def test_provider_supports_concurrency_mapping(store):
    """DeepSeek/GLM/智谱 均支持并发（glm-5.3-flash 实测 ~50 并发）；未知 / 空 → 串行。"""
    svc = SettingsService(store)
    assert svc.provider_supports_concurrency("deepseek") is True
    assert svc.provider_supports_concurrency("DeepSeek") is True
    assert svc.provider_supports_concurrency("glm") is True
    assert svc.provider_supports_concurrency("zhipu") is True
    assert svc.provider_supports_concurrency("unknown") is False
    assert svc.provider_supports_concurrency("") is False


def test_get_provider_concurrency_explicit_override(store):
    """provider dict 显式 supports_concurrency 可覆盖；缺省按 id 查表；未知=串行。"""
    svc = SettingsService(store)
    assert svc.get_provider_concurrency({"id": "glm", "supports_concurrency": True}) is True
    assert svc.get_provider_concurrency({"id": "deepseek", "supports_concurrency": False}) is False
    assert svc.get_provider_concurrency({"id": "deepseek"}) is True
    assert svc.get_provider_concurrency({"id": "glm"}) is True
    assert svc.get_provider_concurrency({"id": "unknown"}) is False
    assert svc.get_provider_concurrency(None) is False


def test_get_provider_concurrency_model_hint(store):
    """自定义 id（p_xxx）按模型名提示识别并发（glm/deepseek）；不命中=串行。"""
    svc = SettingsService(store)
    assert svc.get_provider_concurrency({"id": "p_abc", "model": "glm-5.3-flash"}) is True
    assert svc.get_provider_concurrency({"id": "p_abc", "model": "deepseek-chat"}) is True
    assert svc.get_provider_concurrency({"id": "p_abc", "model": "qwen-max"}) is False


def test_pipeline_workers_by_active_provider(store):
    """DeepSeek / GLM（均并发）→ CONCURRENT_PIPELINE_WORKERS；未知 → 1（串行）。"""
    svc = SettingsService(store)
    svc.save_providers([
        {"id": "deepseek", "name": "DS", "base_url": "https://a", "model": "m",
         "api_key": "k", "enabled": True},
        {"id": "glm", "name": "GLM", "base_url": "https://b", "model": "glm-5.3-flash",
         "api_key": "k", "enabled": True},
        {"id": "unknown", "name": "Unk", "base_url": "https://c", "model": "qq",
         "api_key": "k", "enabled": True},
    ])
    svc.set_active_provider("deepseek")
    assert svc.get_pipeline_workers() == CONCURRENT_PIPELINE_WORKERS
    svc.set_active_provider("glm")
    assert svc.get_pipeline_workers() == CONCURRENT_PIPELINE_WORKERS
    svc.set_active_provider("unknown")
    assert svc.get_pipeline_workers() == 1


def test_task_manager_desired_workers_parallel(monkeypatch):
    """TaskManager._desired_workers 按 settings.pipeline_workers 取并发数。"""
    import app.services.container as _cont
    from app.services.task_service import TaskManager

    tm = TaskManager.__new__(TaskManager)

    class _Svc:
        def get_pipeline_workers(self):
            return CONCURRENT_PIPELINE_WORKERS

    monkeypatch.setattr(_cont, "get_settings_service", lambda: _Svc())
    assert tm._desired_workers() == CONCURRENT_PIPELINE_WORKERS


def test_task_manager_desired_workers_fallback_serial(monkeypatch):
    """并发判定失败（容器未初始化）→ 回退串行 1（保守，绝不并发误伤限流）。"""
    import app.services.container as _cont
    from app.services.task_service import TaskManager

    tm = TaskManager.__new__(TaskManager)

    def boom():
        raise RuntimeError("容器未初始化")

    monkeypatch.setattr(_cont, "get_settings_service", boom)
    assert tm._desired_workers() == 1
