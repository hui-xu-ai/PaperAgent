#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: skill/src/paperparse/config.py
功能: 应用配置：.env 加载 + 环境变量覆盖（12-Factor）
      + 资源定位（源码/可编辑态在 skill/，wheel 装在 <prefix>/share/paperparse_skill）
对外接口: load_config / templates_dir / get_project_root / asset_root
版本: v1.1.0 (2026-08-19)
版本历史:
  v1.1.0 新增 asset_root()（资源解析回退：源码目录 → 安装 share 目录），支持 wheel 自包含
  v1.0.0 初始版本
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_ENV_FILE = ".env"

_DATA_SUBDIR = "share/paperparse_skill"


def get_project_root() -> Path:
    """[全局] 返回 skill 包源码根目录（skill/，源码/可编辑安装时资源所在）"""
    return Path(__file__).resolve().parents[1]


def _locate_env_file(env_file: str) -> Path | None:
    """[局部] 定位 .env 文件（修复：原按 cwd 相对路径查找，从 skill/ 运行读不到仓库根 .env
    → 密钥从未加载 → mineru-v4 永远报"未配置密钥"）：
    依次查找：cwd/<env_file> → 项目根(skill/)/<env_file> → 仓库根(skill 父目录)/<env_file>
    """
    p = Path(env_file)
    if p.exists():
        return p
    for base in (get_project_root(), get_project_root().parent):
        cand = base / env_file
        if cand.exists():
            return cand
    return None


def asset_root() -> Path:
    """[全局] 返回 skill 资源（SKILL.md/templates/tools/prompts/memory）所在根目录：
    优先源码目录 skill/（可编辑安装），否则回退 wheel 安装的 <prefix>/share/paperparse_skill。
    """
    src_root = get_project_root()
    if (src_root / "SKILL.md").exists():
        return src_root
    # wheel：data-files 装入 sys.prefix/share/paperparse_skill/
    for base in (Path(sys.prefix), Path(sys.base_prefix), Path.home() / ".local"):
        cand = base / _DATA_SUBDIR
        if (cand / "SKILL.md").exists():
            return cand
    return src_root


def templates_dir() -> Path:
    """[全局] 返回模板目录（asset_root()/templates），可用环境变量 PAPER_TEMPLATES_DIR 覆盖"""
    override = os.getenv("PAPER_TEMPLATES_DIR")
    return Path(override) if override else (asset_root() / "templates")


@dataclass
class AppConfig:
    """[全局] 运行时配置（不可变约定：加载后只读）"""
    mineru_api_key: str = ""
    mineru_base_url: str = "https://mineru.net/api/v4"
    mineru_model_version: str = "vlm"
    mineru_daily_page_limit: int = 1000
    mineru_timeout_sec: int = 120
    mineru_max_upload_bytes: int = 750 * 1024
    # 2026-09-12 批2：解析**质量参数**集中声明（此前 language 硬编码 "en"，enable_table/is_ocr
    # 根本不下发 ⇒ 中文文献走英文 OCR、扫描件近乎无输出）。取值与默认值：
    #   mineru_language: auto（按首页字符集判定 ch/en）| en | ch
    #   mineru_is_ocr:   auto（按首页可抽字符数判定：扫描件 → 开）| on | off
    mineru_language: str = "auto"
    mineru_is_ocr: str = "auto"
    mineru_enable_table: bool = True
    mineru_enable_formula: bool = True
    # PaddleOCR-VL optionalPayload（JSON 字符串；空 → 用 DEFAULT_PADDLEOCR_OPTIONS）
    paddleocr_options: str = ""
    # P11：PaddleOCR-VL 官方云 API（AI Studio token；模型名 PaddleOCR-VL-1.6）
    paddleocr_access_token: str = ""
    paddleocr_base_url: str = "https://paddleocr.aistudio-app.com"
    paddleocr_model_version: str = "PaddleOCR-VL-1.6"
    paddleocr_timeout_sec: int = 120
    pdf_render_dpi: int = 300
    stitch_batch_pages: int = 4
    output_dir: str = "output"
    input_dir: str = "input"
    md_template: str = "obsidian_bilingual"
    # P-ENHANCE R04：规则库根（RULES_DIR 环境变量；空 → rule_library.default_rules_dir()）
    rules_dir: str = ""
    llm_enabled: bool = False
    debug: bool = False


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _as_int(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value.strip())
    except ValueError:
        return default


def load_config(env_file: str | None = DEFAULT_ENV_FILE) -> AppConfig:
    """[全局] 加载配置：先读 .env，再读进程环境变量（后者优先）

    参数:
        env_file: .env 文件名/路径；None 表示跳过 .env（用于测试/纯环境变量场景）；
                  相对路径按 cwd → skill/ → 仓库根 自动查找
    返回:
        AppConfig 实例
    报错:
        不抛错；缺项用默认值，selftest 负责提示
    """
    if env_file:
        env_path = _locate_env_file(env_file)
        if env_path is not None:
            load_dotenv(env_path)

    cfg = AppConfig(
        mineru_api_key=os.getenv("MINERU_API_KEY", "").strip(),
        mineru_base_url=os.getenv("MINERU_BASE_URL", AppConfig.mineru_base_url).strip(),
        mineru_model_version=os.getenv("MINERU_MODEL_VERSION", AppConfig.mineru_model_version).strip(),
        mineru_daily_page_limit=_as_int(os.getenv("MINERU_DAILY_PAGE_LIMIT"), AppConfig.mineru_daily_page_limit),
        mineru_timeout_sec=_as_int(os.getenv("MINERU_TIMEOUT_SEC"), AppConfig.mineru_timeout_sec),
        mineru_max_upload_bytes=_as_int(os.getenv("MINERU_MAX_UPLOAD_BYTES"),
                                        AppConfig.mineru_max_upload_bytes),
        mineru_language=os.getenv("MINERU_LANGUAGE", AppConfig.mineru_language).strip().lower(),
        mineru_is_ocr=os.getenv("MINERU_IS_OCR", AppConfig.mineru_is_ocr).strip().lower(),
        mineru_enable_table=_as_bool(os.getenv("MINERU_ENABLE_TABLE"), AppConfig.mineru_enable_table),
        mineru_enable_formula=_as_bool(os.getenv("MINERU_ENABLE_FORMULA"),
                                       AppConfig.mineru_enable_formula),
        paddleocr_options=os.getenv("PADDLEOCR_OPTIONS", "").strip(),
        paddleocr_access_token=os.getenv("PADDLEOCR_ACCESS_TOKEN", "").strip(),
        paddleocr_base_url=os.getenv("PADDLEOCR_BASE_URL", AppConfig.paddleocr_base_url).strip(),
        paddleocr_model_version=os.getenv("PADDLEOCR_MODEL_VERSION",
                                          AppConfig.paddleocr_model_version).strip(),
        paddleocr_timeout_sec=_as_int(os.getenv("PADDLEOCR_TIMEOUT_SEC"),
                                      AppConfig.paddleocr_timeout_sec),
        pdf_render_dpi=_as_int(os.getenv("PDF_RENDER_DPI"), AppConfig.pdf_render_dpi),
        stitch_batch_pages=_as_int(os.getenv("STITCH_BATCH_PAGES"), AppConfig.stitch_batch_pages),
        output_dir=os.getenv("OUTPUT_DIR", AppConfig.output_dir).strip(),
        input_dir=os.getenv("INPUT_DIR", AppConfig.input_dir).strip(),
        md_template=os.getenv("MD_TEMPLATE", AppConfig.md_template).strip(),
        rules_dir=os.getenv("RULES_DIR", "").strip(),
        llm_enabled=_as_bool(os.getenv("LLM_ENABLED"), False),
        debug=_as_bool(os.getenv("PAPER_DEBUG"), False),
    )
    return cfg
