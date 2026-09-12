#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: tests/test_config.py
功能: T0 config 模块单元测试
对外接口: 无（测试）
版本: v1.0.0 (2025-xx-xx)
版本历史:
  v1.0.0 初始版本
"""
from paperparse.config import AppConfig, asset_root, get_project_root, load_config, templates_dir


def test_defaults_without_env(monkeypatch, tmp_work):
    monkeypatch.chdir(tmp_work)  # 隔离项目根 .env
    cfg = load_config(env_file=None)
    assert cfg.mineru_base_url == "https://mineru.net/api/v4"
    assert cfg.mineru_model_version == "vlm"
    assert cfg.pdf_render_dpi == 300
    assert cfg.mineru_daily_page_limit == 1000
    assert cfg.llm_enabled is False


def test_env_override(monkeypatch, tmp_work):
    monkeypatch.chdir(tmp_work)
    monkeypatch.setenv("PDF_RENDER_DPI", "200")
    monkeypatch.setenv("MINERU_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_ENABLED", "true")
    cfg = load_config(env_file=None)
    assert cfg.pdf_render_dpi == 200
    assert cfg.mineru_api_key == "sk-test"
    assert cfg.llm_enabled is True


def test_invalid_int_falls_back(monkeypatch, tmp_work):
    monkeypatch.chdir(tmp_work)
    monkeypatch.setenv("PDF_RENDER_DPI", "abc")
    cfg = load_config(env_file=None)
    assert cfg.pdf_render_dpi == 300


def test_dotenv_autodiscovery(tmp_work, monkeypatch):
    """.env 自动查找链（修复：从 skill/ 运行也能读到仓库根 .env）：
    cwd → 项目根(skill/) → 项目根父目录(仓库根) 依次查找同名 env_file"""
    from paperparse import config as cfgmod
    repo_root = tmp_work / "repo"
    skill_dir = repo_root / "skill"
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / ".env").write_text("MINERU_API_KEY=sk-root\n", encoding="utf-8")
    monkeypatch.setattr(cfgmod, "get_project_root", lambda: skill_dir)
    monkeypatch.chdir(tmp_work)
    # env_file=None 绝不加载 .env（先清进程环境，避免 load_dotenv 残留）
    monkeypatch.delenv("MINERU_API_KEY", raising=False)
    assert cfgmod.load_config(env_file=None).mineru_api_key == ""
    # 默认自动查找：cwd 与 skill/ 均无 .env，应回退到仓库根
    cfg = cfgmod.load_config()
    assert cfg.mineru_api_key == "sk-root"


def test_asset_root_resolves_skill_source():
    """asset_root() 在源码态应指向 skill/（含 SKILL.md），且 templates_dir 可用（wheel 回退预留）"""
    root = asset_root()
    assert (root / "SKILL.md").exists()
    assert root == get_project_root()
    t = templates_dir()
    assert (t / "obsidian_bilingual.md.j2").exists()


def test_asset_root_falls_back_to_installed_share(tmp_work, monkeypatch):
    """wheel 态：源码目录无 SKILL.md 时，回退到 <sys.prefix>/share/paperparse_skill"""
    import sys
    from paperparse import config as cfgmod
    fake_share = tmp_work / "share" / "paperparse_skill"
    fake_share.mkdir(parents=True, exist_ok=True)
    (fake_share / "SKILL.md").write_text("# x", encoding="utf-8")
    # 模拟：让源码根判定 SKILL.md 不存在（用 monkeypatch 把判断用的目录换成临时的）
    monkeypatch.setattr(cfgmod, "get_project_root", lambda: tmp_work / "no_skill")
    monkeypatch.setattr(sys, "prefix", str(tmp_work))
    assert cfgmod.asset_root() == fake_share
