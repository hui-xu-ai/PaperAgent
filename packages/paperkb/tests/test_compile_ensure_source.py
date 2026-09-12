# -*- coding: utf-8 -*-
"""编译入口"按需纳入 kb"单测（2026-09-11 用户模型）。

模型：PDF 解析后文献只留在 library（未编译不算知识库内容）；**只有用户选择编译时**
才把原文层四件同步进 kb。本测试锁定 Compiler._ensure_source 的两条性质：
1) kb 缺 document.json → 从 library 复制进来（编译能拿到正文）；
2) kb 已有 → 不覆盖（保持 D10 冻结原则，不因重复编译回退用户编辑）。
"""
from __future__ import annotations

import json

from paperkb.compile import Compiler
from paperkb.config import Roots


def _roots(tmp_path) -> Roots:
    return Roots(data_dir=tmp_path / "data",
                 library_dir=tmp_path / "library",
                 kb_dir=tmp_path / "kb")


def _mk_library(root: Roots, dirname: str = "10.1000_a.1") -> None:
    d = root.library_dir / dirname
    d.mkdir(parents=True, exist_ok=True)
    (d / "en.md").write_text("# Title\n\nbody\n", encoding="utf-8")
    (d / "document.json").write_text(
        json.dumps({"metadata": {"doi": "10.1000/a.1"}, "paragraphs": []}),
        encoding="utf-8")


def _compiler(roots: Roots) -> Compiler:
    """只测 _ensure_source（不建 store/LLM）：绕过 __init__ 注入 roots。"""
    c = object.__new__(Compiler)
    c.roots = roots
    return c


def test_ensure_source_copies_from_library(tmp_path):
    roots = _roots(tmp_path)
    _mk_library(roots)
    assert not (roots.kb_dir / "10.1000_a.1").exists(), "前置：kb 里还没有该文献"

    _compiler(roots)._ensure_source("10.1000/a.1")

    assert (roots.kb_dir / "10.1000_a.1" / "document.json").exists(), "编译前应纳入 kb"
    assert (roots.kb_dir / "10.1000_a.1" / "en.md").exists(), "原文层四件应一并纳入"


def test_ensure_source_is_frozen_when_present(tmp_path):
    roots = _roots(tmp_path)
    _mk_library(roots)
    dst = roots.kb_dir / "10.1000_a.1"
    dst.mkdir(parents=True, exist_ok=True)
    (dst / "en.md").write_text("USER-EDITED", encoding="utf-8")
    (dst / "document.json").write_text("{}", encoding="utf-8")

    _compiler(roots)._ensure_source("10.1000/a.1")

    assert (dst / "en.md").read_text(encoding="utf-8") == "USER-EDITED", \
        "kb 已存在时不得覆盖（冻结原则）"


def test_ensure_source_missing_library_does_not_raise(tmp_path):
    """library 无该文献时只告警不抛（错误交由后续读取阶段报出更准确的信息）。"""
    roots = _roots(tmp_path)
    _compiler(roots)._ensure_source("10.1000/not-there")  # 不抛异常
    assert not (roots.kb_dir / "10.1000_not-there").exists()
