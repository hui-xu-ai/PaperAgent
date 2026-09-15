# -*- coding: utf-8 -*-
"""头部元数据契约守卫（2026-09-16）。

两件必须长期成立的事，各自都没有天然的守卫：

1. **字段顺序单一事实源跨包一致**：`paperparse`（渲染包）与 `paperkb`（知识库包）
   **互不 import**（各自 pyproject 声明，CI 有 `test_declared_deps.py` 把关），
   于是"作者、通讯作者、研究单位、年份、期刊、影响因子、JCR分区、中科院分区、DOI、被引、关键词"
   这个顺序在两处各写了一遍。backend 同时依赖两者 ⇒ 在这里做跨包断言。

2. **渲染前必须已富化元数据**：`zh.md`/`en_zh.md` 的头部读 `papers_meta`，而富化
   （`_ensure_meta_for_doi` → Crossref）原本只在 `_assemble_kb`（翻译之后）里跑 ⇒
   实测"头部期刊/年份空、被引 0，而 `_note.md` 有真值"（时间线：zh.md 00:30:21 <
   identifiers 00:30:23 < _note.md 00:30:59）。顺序是**代码结构**而非行为，用 AST 守。
"""
from __future__ import annotations

import ast
from pathlib import Path

TASK_SERVICE = Path(__file__).resolve().parents[1] / "app" / "services" / "task_service.py"


def test_field_order_identical_in_both_packages():
    """paperkb.headmeta.FIELD_ORDER == paperparse.markdown_render.FIELD_ORDER。"""
    from paperkb.headmeta import FIELD_ORDER as KB_ORDER
    from paperparse.core.markdown_render import FIELD_ORDER as PP_ORDER

    assert tuple(KB_ORDER) == tuple(PP_ORDER), (
        "两个包各写了一份头部字段顺序（互不依赖），必须逐字一致；"
        f"paperkb={KB_ORDER} paperparse={PP_ORDER}")


def _find_func(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"未找到函数 {name}")


def _lineno_of_call(func: ast.FunctionDef, attr: str) -> int:
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == attr:
            return node.lineno
    raise AssertionError(f"{func.name} 里没有调用 {attr}()")


def test_enrich_runs_before_variant_render():
    """`_translate_and_export` 里 `_enrich_meta_before_render` 必须早于 `combined_translate`。"""
    tree = ast.parse(TASK_SERVICE.read_text(encoding="utf-8"))
    func = _find_func(tree, "_translate_and_export")
    enrich = _lineno_of_call(func, "_enrich_meta_before_render")
    render = _lineno_of_call(func, "combined_translate")
    assert enrich < render, (
        f"元数据富化（L{enrich}）必须在变体渲染（L{render}）之前——否则头部期刊/年份/被引"
        "那一瞬永远是空的（papers_meta 还没写）")


def test_enrich_helper_reads_doi_from_document_json():
    """`_enrich_meta_before_render` 从 document.json 取 DOI 调富化（无 DOI 则跳过）。"""
    tree = ast.parse(TASK_SERVICE.read_text(encoding="utf-8"))
    func = _find_func(tree, "_enrich_meta_before_render")
    src = ast.unparse(func)
    assert "_ensure_meta_for_doi" in src
    assert "metadata" in src and "doi" in src
