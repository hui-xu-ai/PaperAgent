# -*- coding: utf-8 -*-
"""引擎资源路径适配（应用侧兼容，不改引擎代码）。

背景：skill 仓库目录结构多次调整（2026-08-20：templates/tools/prompts 曾在 src/ 下，
引擎 asset_root() 只认 skill/ 根），导致开发模式资源定位失效。
同时按用户决策做**项目内快照**（engine_assets/），发布自包含、不依赖外部目录。

资源优先级：
1. 项目内快照 backend/engine_assets/（SKILL.md + templates/tools/prompts/memory 扁平）
2. skill 根结构（skill/templates 存在 → 引擎原生定位即可，无需 patch）
3. skill/src 结构（src/templates 存在 → patch asset_root 到 src）

实现：PAPER_TEMPLATES_DIR 环境变量（引擎 templates_dir() 原生支持覆盖）
+ patch paperparse.config.asset_root（必须在任何引擎模块 import 之前执行，
  保证各模块 `from ..config import asset_root` 绑定适配后的函数）。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# backend/engine_assets/（项目内快照）
SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "engine_assets"


def _use(base: Path) -> None:
    os.environ.setdefault("PAPER_TEMPLATES_DIR", str(base / "templates"))
    import paperparse.config as pc
    pc.asset_root = lambda: base
    logger.info("引擎资源适配: asset_root -> %s", base)


def apply_engine_asset_patch() -> None:
    """开发模式资源适配（幂等；打包模式自动跳过）。

    2026-08 架构统一后优先级：原生资产（packages/paperparse 内）→ 项目内快照
    engine_assets/（打包自包含）→ 历史 skill 根/src 兼容。
    """
    try:
        import paperparse.config as pc
        # 1) 原生资产（dev/editable 安装，packages/paperparse 内）：无需 patch
        try:
            native = pc.asset_root()
            if (native / "rules").exists():
                logger.info("引擎资源原生可用: %s（无需 patch）", native)
                return
        except Exception:
            pass
        # 2) 项目内快照（自包含，打包模式）
        if (SNAPSHOT_DIR / "SKILL.md").exists() and (SNAPSHOT_DIR / "templates").exists():
            _use(SNAPSHOT_DIR)
            return
        # 3) 历史 skill 根结构（兼容旧布局）
        root = pc.get_project_root()
        if (root / "templates").exists():
            return
        src = root / "src"
        if (src / "templates").exists():
            _use(src)
    except Exception as e:  # noqa: BLE001 - patch 失败不阻塞
        logger.warning("引擎资源适配失败（将尝试默认路径）: %s", e)
