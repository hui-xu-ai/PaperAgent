# -*- coding: utf-8 -*-
"""知识库服务（R1 收敛）：文件树/文件读写/图片/目录，取代旧 V04 生成职能。

目录结构（knowledge_base/ 路径可配置，可指向 Obsidian vault）：
    _index.md                 总索引（★由 paperkb regenerate_index 统一重建）
    <DOI>/                    按 DOI 建文件夹（无 DOI → run_<runid>）
      _note.md                一级：核心知识卡（★paperkb Compiler L1 生成）
      _wiki.md                二级：深度编译（★paperkb Compiler L2 生成）
      _relations.md           三级：概念关系（★paperkb Compiler L3 生成）
      en.md/en_zh.md/zh.md/summary.md   变体/原文层（paperkb/engine 直写 kb，单一来源）
      document.json / images/ / source.pdf   原文层副本（paperkb 同步）

R1（2026-08-26 架构整改）：本服务不再生成 _note.md/_wiki.md/_index.md——
一级/二级笔记与索引一律由 paperkb（Compiler + regenerate_index）产出，
旧 V04 的 _build_note/_build_details/generate/backfill/_index_row/_update_index 已删除。
本服务只保留：文件树、安全读写、图片路径、目录管理、原文层同步（_link_originals）。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from ..config import APP_DATA_DIR, Settings
from .engine_service import EngineService
from .store import Store

logger = logging.getLogger(__name__)

# 翻译变体（P0-B 2026-09-12：真相源在 library/<资源目录>/，kb 侧只读回退）
_KB_VARIANTS = ("zh.md", "en_zh.md", "summary.md")


class KnowledgeBaseService:
    def __init__(self, settings: Settings, store: Store, engine: EngineService):
        self.settings = settings
        self.store = store
        # engine 保留为构造签名兼容（文件管理路径不再依赖引擎生成）。
        self.engine = engine

    # ---------------------------------------------------------- 路径
    def root(self) -> Path:
        from .settings_service import KEY_KB_PATH
        from ..services import container
        kb = container.get_settings_service().get_kb_path()
        base = Path(kb) if kb else APP_DATA_DIR / "knowledge_base"
        return base

    def paper_dir(self, doi: str, run_id: str = "", fallback: str = "") -> str:
        """DOI 目录名（"/"→"_"，空 DOI 用 run_id，再回退 PDF 文件名净化）。G13。"""
        if doi:
            name = doi.strip().replace("/", "_").replace(":", "_")
            name = re.sub(r"[^\w.\-]", "_", name)
            return name[:120] or f"run_{run_id}" or "paper"
        if run_id:
            return f"run_{run_id}"
        if fallback:
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", fallback.strip()).strip(" .")
            name = re.sub(r"[^\w.\-]", "_", name)
            return name[:120] or "paper"
        return "paper"

    # ---------------------------------------------------------- 原文层同步
    def _kb_copy_mode(self) -> str:
        from ..services import container
        try:
            return container.get_settings_service().get_kb_copy_mode()
        except Exception:  # noqa: BLE001
            return "copy"

    @staticmethod
    def _copy_or_link(src: Path, dst: Path, mode: str) -> None:
        """按模式把 src 复制/硬链接到 dst（link 失败回退 copy）。"""
        import os
        if mode == "link":
            try:
                os.link(src, dst)
                return
            except OSError:
                pass
        import shutil
        shutil.copy2(src, dst)

    def _link_originals(self, paper: dict, folder: Path) -> None:
        """把规范库 library/<DOI>/ 的原文层副本带进知识库文件夹。

        T4：变体（zh.md/en_zh.md/summary.md）由 combined_translate **直写 kb**（单一来源），
        这里不再从 library 复制；只同步原文层 en.md / document.json / images/
        （固定纳入，用户已备份；images/ 为 kb 内 md 引用所需）。
        """
        copy_mode = self._kb_copy_mode()
        doc_path = Path(paper["doc_json"]).resolve()
        base = doc_path.parent
        if base.name == "intermediate":
            base = base.parent  # 兼容 parse-only 中间层 library/<DOI>/intermediate/
        # en.md / document.json：源在 library 篇目录（document.json 可能位于
        # intermediate/ 中间层，直接用 doc_path 原路径）
        for src in (base / "en.md", doc_path):
            if src.is_file():
                try:
                    # G13：始终同步 library 最新产物（覆盖旧拷贝，如含 <!-- image--> 的旧 en.md）
                    self._copy_or_link(src, folder / src.name, copy_mode)
                except OSError:
                    pass
        img_src = base / "images"
        if img_src.exists():
            try:
                import shutil
                shutil.copytree(img_src, folder / "images", dirs_exist_ok=True)
            except OSError:
                pass

    # ---------------------------------------------------------- 读取/浏览
    def tree(self) -> dict:
        """知识库目录树（前端浏览器用）。

        P0-B（2026-09-12）：**变体（zh.md/en_zh.md）真相源在 library**——kb 里没有就
        从 library 补列（老数据 kb 里仍有变体时优先显示 kb，读到时按 mtime 择新）。
        """
        root = self.root()
        folders = []
        if root.exists():
            for d in sorted(root.iterdir()):
                if not d.is_dir() or d.name.startswith("."):
                    continue
                names = {f.name: f.stat().st_size
                         for f in sorted(d.iterdir()) if f.is_file()}
                for v in _KB_VARIANTS:
                    if v in names:
                        continue
                    src = self._variant_in_library(d.name, v)
                    if src is not None:
                        names[v] = src.stat().st_size
                files = [{"name": n, "size": s} for n, s in sorted(names.items())]
                folders.append({"doi_dir": d.name, "files": files})
        return {"root": str(root), "folders": folders,
                "has_index": (root / "_index.md").exists()}

    def _library_dir_for(self, dirname: str) -> Path | None:
        """kb 目录名 → library 对应目录（变体回退读取用；不存在返回 None）。

        ⚠️ APP_DATA_DIR 必须**运行时**取（`app.config` 属性），否则测试/换根时
        monkeypatch 不生效（模块级 `from ... import APP_DATA_DIR` 会固化旧值）。
        """
        import app.config as cfg

        lib = Path(getattr(cfg, "APP_DATA_DIR", APP_DATA_DIR)) / "library"
        cand = lib / dirname
        return cand if cand.is_dir() else None

    def _variant_in_library(self, dirname: str, name: str) -> Path | None:
        lib = self._library_dir_for(dirname)
        if lib is None:
            return None
        p = lib / name
        return p if p.is_file() else None

    def read_file(self, rel_path: str) -> dict:
        """安全读取知识库内文件（防目录穿越）。

        P0-B（2026-09-12）：变体（zh.md/en_zh.md）曾"真相源在 library"。
        2026-09-16（方案 A，用户决策）：**kb 是唯一定版**，因此改为——
          · kb 有该文件 ⇒ **直接读 kb**（不再与 library 比 mtime：mtime 择新会让"读到的内容"
            取决于文件时间戳，与编译/翻译的读写基准不一致；审计 C12）；
          · kb 缺该文件 ⇒ 回退 library（兼容迁移前的旧文献：它们的中文变体只存在于 library）。
        """
        root = self.root().resolve()
        target = (root / rel_path).resolve()
        if not target.is_relative_to(root):
            raise FileNotFoundError(f"文件不存在: {rel_path}")
        picked = target
        if target.name in _KB_VARIANTS and not target.is_file():
            alt = self._variant_in_library(Path(rel_path).parts[0], target.name)
            if alt is not None:
                picked = alt
        if not picked.is_file():
            raise FileNotFoundError(f"文件不存在: {rel_path}")
        return {"path": rel_path, "content": picked.read_text(encoding="utf-8")}

    def list_images(self, rel_dir: str) -> list[dict]:
        """列出知识库目录下 images/ 子目录的图片（阅读器图片 tab）。"""
        root = self.root().resolve()
        base = (root / rel_dir).resolve()
        if not base.is_relative_to(root):
            raise ValueError("非法路径")
        img_dir = base / "images"
        if not img_dir.is_dir():
            return []
        exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
        return [{"name": f.name, "size": f.stat().st_size}
                for f in sorted(img_dir.iterdir())
                if f.is_file() and f.suffix.lower() in exts]

    def image_path(self, rel_path: str) -> Path | None:
        """安全解析知识库内图片路径（返回绝对路径或 None）。"""
        root = self.root().resolve()
        target = (root / rel_path).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            return None
        if target.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}:
            return None
        return target

    def write_file(self, rel_path: str, content: str) -> dict:
        """保存知识库文件（U5 阅读器编辑；防穿越；记录编辑标记防插件覆盖）。"""
        root = self.root().resolve()
        target = (root / rel_path).resolve()
        if not target.is_relative_to(root):
            raise ValueError("非法路径")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        from ..services import container
        container.get_store().mark_kb_edited(rel_path)
        logger.info("知识库文件已保存: %s", rel_path)
        return {"path": rel_path, "saved": True}

    # ---------------------------------------------------------- 文件操作（V11）
    def _safe_target(self, rel_path: str) -> Path:
        root = self.root().resolve()
        target = (root / rel_path).resolve()
        if not target.is_relative_to(root):
            raise ValueError("非法路径")
        return target

    def create_file(self, rel_path: str, content: str = "") -> dict:
        target = self._safe_target(rel_path)
        if target.exists():
            raise FileExistsError(f"已存在: {rel_path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"path": rel_path, "created": True}

    def create_dir(self, rel_path: str) -> dict:
        target = self._safe_target(rel_path)
        target.mkdir(parents=True, exist_ok=True)
        return {"path": rel_path, "created": True}

    def rename(self, rel_path: str, new_name: str) -> dict:
        """重命名（new_name 为同目录下的新文件名/文件夹名）。"""
        if not new_name or "/" in new_name or "\\" in new_name or new_name in (".", ".."):
            raise ValueError("非法名称")
        target = self._safe_target(rel_path)
        if not target.exists():
            raise FileNotFoundError(f"不存在: {rel_path}")
        new_path = target.parent / new_name
        if new_path.exists():
            raise FileExistsError(f"目标已存在: {new_name}")
        target.rename(new_path)
        return {"path": str(new_path.relative_to(self.root())), "renamed": True}

    def delete(self, rel_path: str) -> dict:
        target = self._safe_target(rel_path)
        if not target.exists():
            raise FileNotFoundError(f"不存在: {rel_path}")
        if target.is_dir():
            target.rmdir() if not any(target.iterdir()) else None
            if target.exists():
                raise ValueError("目录非空，请先清空")
        else:
            target.unlink()
        return {"path": rel_path, "deleted": True}
