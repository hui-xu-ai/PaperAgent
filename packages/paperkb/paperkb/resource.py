# -*- coding: utf-8 -*-
"""资源键解析（P0-B step4，2026-09-12）：**无 DOI 文献也能编译**。

背景（用户模型）：编译只需要「有正文 + 一份元数据」，DOI 只是可选标识。
旧实现把 DOI 当主键、目录名由 DOI 派生，于是"没有 DOI"= 进不了编译链
（`_assemble_kb` 直接 return、`Compiler.queue` 报 skipped_no_doc、`_doc` 抛
`kb 中无 document.json`）。P0-B step1/step2 已把主键换成 rid，这里把最后一环补上：
**同一套键解析同时覆盖 DOI / RID / 目录名 / md5 目录**。

约定：
- 目录名候选（`candidate_dirnames`）：RID `doi-x/y` → 目录名 `x_y`；裸 DOI → 目录名；
  md5 目录名 / 历史目录名原样保留。
- store 键（`resource_key`）：优先 papers_meta 里已登记的 rid；否则 DOI → `doi-…`；
  只有 md5 → `nd-<md5前12>`（与 `make_rid` 的内容指纹规则一致）。
"""
from __future__ import annotations

from pathlib import Path

from .doi import doi_to_dirname, is_doi, is_md5_dir

# RID 类型前缀（与 doi.KIND_PREFIX / db.resolve_rid 的白名单一致）
RID_PREFIXES = ("doi-", "isbn-", "cnki-", "cstr-", "arxiv-", "rep-", "nd-",
                "si__", "review__", "book__", "chapter__", "thesis__",
                "note__", "patent__", "std__")


def candidate_dirnames(key: str, store=None) -> list[str]:
    """键 → 候选目录名（顺序即优先级；不猜不存在的目录）。"""
    k = (key or "").strip()
    out: list[str] = []

    def _add(name: str) -> None:
        if name and name not in out:
            out.append(name)

    _add(k)
    if k.startswith("doi-"):
        rest = k[4:]
        _add(rest)
        _add(doi_to_dirname(rest))
    elif is_doi(k):
        _add(doi_to_dirname(k))
    if store is not None and k:
        try:
            row = store.get_doi_md5_map(k)
        except Exception:  # noqa: BLE001 - 映射表缺失按无映射处理
            row = None
        if row:
            doi = (row.get("doi") or "").strip()
            if doi:
                _add(doi_to_dirname(doi))
    return out


def resource_key(store, key: str, *, dirname: str = "") -> str:
    """键 → 统一资源键 rid（编译产物 / 编译任务 / 元数据都用它）。

    覆盖四种写法（任一都必须得到**同一个**键，否则同一篇会被当成两篇）：
    RID `doi-10.1002_adma…` / 裸 DOI `10.1002/adma…` / library 目录名 `10.1002_adma…`
    / md5 目录名（无 DOI 文献）。绝不返回空串（返回空会让该篇从列表里消失）。
    """
    from .doi import dirname_to_doi

    k = (key or "").strip()
    d = (dirname or "").strip() or (k if not is_doi(k) and not k.startswith("doi-") else "")
    if not k and not d:
        return ""
    if store is not None and k:
        try:
            rid = store.resolve_rid(k)
        except Exception:  # noqa: BLE001
            rid = ""
        # ⚠️ resolve_rid 对**认不出的键**会原样返回（宽松设计）：只有当它确实解析出
        # 了另一种形态（rid != 键）或本身已是 RID 前缀时才算"解析成功"。
        # 目录名（`10.1000_abc.1`）与 md5 目录名都必须继续往下归一化——否则同一篇
        # 会拿到两个不同的键（实测踩过）。md5 目录名**不算** RID 前缀。
        if rid and rid != k and (rid.startswith("doi-") or is_md5_dir(rid)):
            return rid
        if rid == k and k.startswith(RID_PREFIXES) and not is_md5_dir(k):
            return k
    # 目录名写法 → 还原成 DOI 形态的资源键（存在才认，避免把历史目录名当 DOI）
    for name in (d, k):
        if not name or is_md5_dir(name) or name.startswith("doi-"):
            continue
        back = dirname_to_doi(name)
        if back and is_doi(back):
            cand = "doi-" + doi_to_dirname(back)
            if store is None or store.has_meta(cand):
                return cand
    if is_doi(k):
        return "doi-" + doi_to_dirname(k)
    if k.startswith("doi-"):
        return k
    if is_md5_dir(d or k):
        return "nd-" + (d or k).lower()[:12]
    return k


def resources_dir(root_dir, key: str, store=None) -> Path | None:
    """`<root>/<资源目录>`（root=library_dir 或 kb_dir）；不存在返回 None。

    只做**存在性命中**，不创建目录（调用方决定是否落盘）。
    """
    root = Path(root_dir)
    for name in candidate_dirnames(key, store):
        cand = root / name
        if cand.is_dir():
            return cand
    # 兜底：映射表里目录名与键不同形（历史数据）
    if store is not None and key:
        try:
            row = store.get_doi_md5_map(key)
        except Exception:  # noqa: BLE001
            row = None
        if row:
            cand = root / (row.get("key") or "")
            if cand.is_dir():
                return cand
    return None


def find_doc(root_dir, key: str, store=None, *, search_root=None,
             paper_id: int | None = None, kb: bool = False) -> Path | None:
    """在 `<root_dir>/<资源目录>/<核心数据文件>` 里定位文档。

    `kb`：在哪一侧找（文件名由 `layout.doc_basename` 唯一给定；两侧当前同名，
    见 `layout.py` 的说明——**不要在别处裸写文件名**）。

    两级：
    1. 候选目录名精确命中；
    2. **兜底模糊扫描** `search_root`（通常是 library_dir）下各目录的核心数据文件，
       按 `metadata.doi`（同一 DOI 换目录名）、`metadata.pdf_md5`（无 DOI 文献的
       `nd-<指纹12>` 键）或 `paper_id` 反查——覆盖"目录名与键无任何字面关系"的历史数据。
       仅在传入 search_root 时启用（每条目 O(目录数)，几十篇规模可接受）。
    """
    import json

    from .layout import LIB_DOC_NAME, doc_basename

    hit = resources_dir(root_dir, key, store)
    doc_name = doc_basename(kb=kb)
    if hit is not None and (hit / doc_name).exists():
        return hit / doc_name
    if search_root is None or not key:
        return None
    root = Path(search_root)
    if not root.is_dir():
        return None
    fp = key[3:].lower() if key.startswith("nd-") else ""
    for d in sorted(root.iterdir()):
        # 模糊扫描的目标始终是解析库（search_root 语义，见 docstring）
        doc = d / LIB_DOC_NAME
        if not d.is_dir() or not doc.exists():
            continue
        try:
            meta = (json.loads(doc.read_text(encoding="utf-8", errors="replace"))
                    .get("metadata") or {})
        except Exception:  # noqa: BLE001 - 坏文档跳过
            continue
        if is_doi(key) and (meta.get("doi") or "").strip() == key:
            return doc
        if paper_id and str(meta.get("paper_id") or "") == str(paper_id):
            return doc
        if fp:
            md5 = str(meta.get("pdf_md5") or "").strip().lower()
            if md5 and md5.startswith(fp):
                return doc
    return None


def meta_for(store, key: str, doc=None):
    """取元数据（papers_meta 权威）；无 bib 时用 document.json 兜底 title。"""
    from .models import PaperMeta

    if store is not None:
        m = store.get_meta(key)
        if m is not None:
            return m
    title = getattr(doc, "title", "") if doc is not None else ""
    return PaperMeta(rid=resource_key(store, key), doi=key if is_doi(key) else "", title=title)
