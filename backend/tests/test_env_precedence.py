# -*- coding: utf-8 -*-
""".env 与系统环境变量的优先级（2026-09-22 用户拍板：**严格单一来源**）。

背景：dotenv 默认 `override=False` ⇒ Windows 用户环境变量里的旧 Key **永远压住** .env 里的新值。
实测（2026-09-22）：`SILICONFLOW_API_KEY` 系统变量是死值（401 code 30014），.env 里是新值（200），
而程序读到的始终是系统里的死值 ⇒ embedding 通道 + 「硅基流动」供应商预设全坏，且启动自检
`_warn_env_anomalies` 查不出（值非空就不报警）。用户决定：**以 .env 为唯一可查看/编辑的来源**。

本文件锁定：
1. `.env` 里的键**一律覆盖** os.environ（**空值也覆盖**，清空即失效）；
2. `.env` 里没有的键，系统环境变量照旧生效（新装/无该键时不误伤）；
3. 两个 .env 文件之间：APP_DATA_DIR 优先于 backend/；
4. 被覆盖的键要**报告**（只报键名，绝不报值）；
5. 保存供应商后 `_sync_env_file` 写文件**同时**同步 os.environ。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app import config as app_config


def _write_env(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每个用例都在"干净环境变量"上跑：只保留本用例显式设置的键。"""
    for k in ("PAPERAGENT_TEST_KEY", "PAPERAGENT_TEST_EMPTY", "PAPERAGENT_TEST_SYS"):
        monkeypatch.delenv(k, raising=False)
    yield


def test_env_file_overrides_system_env(tmp_path, monkeypatch):
    """.env 里有值 ⇒ 压过系统环境变量（旧 Key 换新 Key 立刻生效的场景）。"""
    monkeypatch.setenv("PAPERAGENT_TEST_KEY", "旧死值")
    p = _write_env(tmp_path / ".env", "PAPERAGENT_TEST_KEY=新值\n")
    app_config._apply_dotenv_files([p])
    assert os.environ["PAPERAGENT_TEST_KEY"] == "新值"


def test_empty_value_also_overrides(tmp_path, monkeypatch):
    """**空值也覆盖**（严格单一来源）：在 .env 里清空 Key ⇒ 它真的失效。"""
    monkeypatch.setenv("PAPERAGENT_TEST_EMPTY", "系统里的旧值")
    p = _write_env(tmp_path / ".env", "PAPERAGENT_TEST_EMPTY=\n")
    app_config._apply_dotenv_files([p])
    assert os.environ["PAPERAGENT_TEST_EMPTY"] == ""


def test_system_env_used_when_key_absent_from_file(tmp_path, monkeypatch):
    """.env 里没写的键 ⇒ 系统环境变量照旧生效（不误伤部署方设的变量）。"""
    monkeypatch.setenv("PAPERAGENT_TEST_SYS", "来自系统")
    p = _write_env(tmp_path / ".env", "PAPERAGENT_TEST_KEY=别的\n")
    app_config._apply_dotenv_files([p])
    assert os.environ["PAPERAGENT_TEST_SYS"] == "来自系统"


def test_later_file_wins(tmp_path, monkeypatch):
    """同一键在两个文件里 ⇒ 越靠后的文件优先（调用方按"低优先级在前"传参）。"""
    backend_env = _write_env(tmp_path / "backend.env", "PAPERAGENT_TEST_KEY=backend\n")
    data_env = _write_env(tmp_path / "data.env", "PAPERAGENT_TEST_KEY=data\n")
    app_config._apply_dotenv_files([backend_env, data_env])   # data 在后 ⇒ 优先
    assert os.environ["PAPERAGENT_TEST_KEY"] == "data"


def test_shadowed_keys_are_reported_without_values(tmp_path, monkeypatch, caplog):
    """被遮蔽的键要报出来（用户才知"我改了没生效"），但**只报键名、不报值**。"""
    monkeypatch.setenv("PAPERAGENT_TEST_KEY", "sk-旧密钥不该出现在日志里")
    p = _write_env(tmp_path / ".env", "PAPERAGENT_TEST_KEY=sk-新密钥也不该出现\n")
    with caplog.at_level("WARNING"):
        app_config._apply_dotenv_files([p])
    text = caplog.text
    assert "PAPERAGENT_TEST_KEY" in text, "被遮蔽的键名必须报出来"
    assert "旧密钥不该出现在日志里" not in text and "新密钥也不该出现" not in text, \
        "绝不能把密钥值打进日志"


def test_missing_file_is_not_an_error(tmp_path):
    """文件不存在（打包版首启可能没有 .env）⇒ 静默跳过，不抛错。"""
    app_config._apply_dotenv_files([tmp_path / "不存在.env"])
    assert True


def test_broken_file_does_not_block_startup(tmp_path, caplog):
    """文件编码坏了 ⇒ 记错误日志但不抛（配置坏了不该拦住启动）。"""
    p = tmp_path / ".env"
    p.write_bytes(b"\xff\xfe\x00 bad bytes")
    with caplog.at_level("ERROR"):
        app_config._apply_dotenv_files([p])
    assert True


# ---------------------------------------------------------------- 写回侧
def test_sync_env_file_updates_os_environ(tmp_path, monkeypatch):
    """保存供应商后：文件与 os.environ 同时更新（否则本次运行仍用旧 Key）。"""
    from app.services.settings_service import SettingsService

    env_path = tmp_path / ".env"
    env_path.write_text("SILICONFLOW_API_KEY=old\nSILICONFLOW_BASE_URL=old\n"
                        "SILICONFLOW_MODEL=old\n", encoding="utf-8")
    monkeypatch.setenv("SILICONFLOW_API_KEY", "old")
    monkeypatch.setenv("SILICONFLOW_BASE_URL", "old")
    monkeypatch.setenv("SILICONFLOW_MODEL", "old")
    svc = SettingsService.__new__(SettingsService)     # 只测这一处纯逻辑，免起 store
    svc.env_path = str(env_path)
    svc._sync_env_file("SILICONFLOW", {"api_key": "new-key", "base_url": "https://new/v1",
                                       "model": "new-model"})
    assert os.environ["SILICONFLOW_API_KEY"] == "new-key"
    assert os.environ["SILICONFLOW_MODEL"] == "new-model"
    assert "SILICONFLOW_API_KEY=new-key" in env_path.read_text(encoding="utf-8")
