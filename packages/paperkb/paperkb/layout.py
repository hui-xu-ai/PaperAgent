# -*- coding: utf-8 -*-
"""存储布局约定（P0-B step3，2026-09-11 用户拍板）。

用户模型（本轮确认）：
- **单一物理根**：不按类型分多个根（类型会变、搬目录=断链；遍历成本也不该翻倍）。
  类型进 **名字**（RID 前缀 `book__`/`thesis__`/`std__`…）+ **数据**（papers_meta.kind），
  在资源管理器里按名排序自然按类型聚在一起，应用里按 kind 筛选。
- **依附资料挂父资源目录**：`library/<父RID>/attachments/{si,review,data}/`。
  这样"一篇文献的全部东西"在一个文件夹里——删除、备份、迁移、手工整理都不会漏。
- **无父资源的资料**才放独立根 `attachments/<RID>/`。

目录示例：
    library/doi-10.1002-adma.202407106/{document.json,en.md,images/,attachments/si/…}
    library/book__isbn-9783527345678/…
    library/thesis__cnki-CDFD2019012345/…
    attachments/note__weekly-2026-09/…        ← 无父资源的零散资料

⚠️ **清理守卫**：`attachments/` 是**用户资料**，任何"重解析/重导出前清空目录"的逻辑
必须先过 `assert_safe_to_clear()`（只清解析产物，绝不删用户附件）。
"""
from __future__ import annotations

from pathlib import Path

# 资源类型（kind）：进 RID 前缀与 DB 字段；paper 无前缀（保持现有目录名）
KINDS = ("paper", "thesis", "book", "chapter", "patent", "standard",
         "report", "note", "si", "review", "data")

# 依附资料类型：挂父资源目录下，**不单独成文献**（不进知识库列表）
ATTACHMENT_KINDS = ("si", "review", "data", "note", "cover", "response")

# 资源目录内受保护的子目录：解析流程清理时必须保留（用户放进去的东西）
PROTECTED_IN_RESOURCE = ("attachments",)

# 无父资源资料的独立根名（项目根下）
STANDALONE_ATTACH_ROOT = "attachments"


def _prefix_to_kind() -> dict[str, str]:
    """前缀（不含尾部 `__`）→ kind。**唯一来源 = doi.KIND_PREFIX**（避免两处定义漂移；
    例如 standard 的前缀是 `std__` 而非 `standard__`）。"""
    from .doi import KIND_PREFIX
    return {p[:-2]: k for k, p in KIND_PREFIX.items() if p}


def kind_from_rid(rid: str) -> str:
    """由 RID 前缀推导资源类型（最长前缀优先）。

    无类型前缀时返回 "paper"（`doi-…` 与无标识的 `nd-…` 都按论文处理）。
    """
    r = (rid or "").strip()
    for prefix, kind in sorted(_prefix_to_kind().items(), key=lambda kv: -len(kv[0])):
        if r.startswith(prefix + "__"):
            return kind
    return "paper"


def kind_for(meta) -> str:
    """元数据的类型：显式 `kind` 优先，否则由 `rid` 前缀推导。"""
    explicit = (getattr(meta, "kind", "") or "").strip().lower()
    if explicit:
        return explicit
    rid = getattr(meta, "rid", "") or ""
    if not rid:
        rid = "doi-" + (getattr(meta, "doi", "") or "")
    return kind_from_rid(rid)


def resource_dir(library_dir: str | Path, rid: str) -> Path:
    """独立资源目录：`library/<RID>`（RID 已含类型前缀，见 doi.make_rid）。"""
    return Path(library_dir) / ((rid or "").strip() or "nd-untitled")


def attachments_dir(resource_root: str | Path, kind: str = "") -> Path:
    """依附资料目录：`<资源目录>/attachments[/<kind>]`（与父资源同生共死）。"""
    base = Path(resource_root) / "attachments"
    k = (kind or "").strip().lower()
    return base / k if k else base


def standalone_attachment_dir(attach_root: str | Path, rid: str,
                              kind: str = "") -> Path:
    """无父资源资料的目录：`attachments/<RID>[/<kind>]`。"""
    base = Path(attach_root) / ((rid or "").strip() or "nd-untitled")
    k = (kind or "").strip().lower()
    return base / k if k else base


def is_attachment_path(path: str | Path, resource_root: str | Path) -> bool:
    """路径是否落在某资源目录的 `attachments/` 内（= 用户资料，不可被清理）。"""
    try:
        rel = Path(path).resolve().relative_to(Path(resource_root).resolve())
    except (ValueError, OSError):
        return False
    return bool(rel.parts) and rel.parts[0] in PROTECTED_IN_RESOURCE


def assert_safe_to_clear(path: str | Path, resource_root: str | Path) -> None:
    """清目录前的守卫：目标是受保护子目录（或位于其内）时拒绝。

    供"重解析/重导出前清空产物"的代码调用——**用户附件不可删**（P0-B step3）。
    """
    target = Path(path).resolve()
    root = Path(resource_root).resolve()
    if is_attachment_path(target, root):
        raise PermissionError(
            f"拒绝清理受保护路径（用户附件）：{target}；只允许清理解析产物")
    if target == root / PROTECTED_IN_RESOURCE[0]:
        raise PermissionError(
            f"拒绝清理受保护路径（用户附件）：{target}；只允许清理解析产物")
