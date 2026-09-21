# -*- coding: utf-8 -*-
"""L3 `_relations.md` 渲染单测：引用项 → Obsidian wikilink 目标归一化。

2026-09-21 用户报障两点（均导致 Obsidian 死链）：
  1) `[[本文/_note]]`——模型用「本文」（含「本文(DIYM)」变体）指代当前篇；
  2) `[[10.1016/j.snb.../_note]]`——目标须是**目录名**（DOI 的 "/" 已转 "_"）。

本测锁定修复：自指标记 → 自身目录；任意引用项 → `doi_to_dirname` 目录名。
"""
from __future__ import annotations

import re

from paperkb.compile import _render_relations
from paperkb.models import PaperMeta

META = PaperMeta(doi="10.1234/self.1", title="Demo Paper")
SELF_DIR = "10.1234_self.1"          # doi_to_dirname("10.1234/self.1")


def _targets(out: str) -> list[str]:
    return re.findall(r"\[\[([^]|]+)", out)


def _assert_all_dirnames(out: str) -> None:
    """所有链接目标必须是无 "/" 的目录名（唯一允许的 "/" 在 /_note 后缀里）。"""
    for t in _targets(out):
        head = t.split("/_note")[0]
        assert "/" not in head, f"链接目标含 '/'（Obsidian 死链）: [[{t}]]"


def test_self_marker_and_bare_doi_in_concept_map():
    """concept_map.papers：本文/本文(DIYM) → 自身目录；裸 DOI → 目录名。"""
    data = {"summary": "s",
            "concept_map": [{"concept": "c",
                             "papers": ["本文", "本文(DIYM)",
                                        "10.1038/ncomms8258"],
                             "evolution": "e"}]}
    out = _render_relations(META, data, [])
    assert f"[[{SELF_DIR}/_note]]" in out
    assert "[[10.1038_ncomms8258/_note]]" in out
    assert "[[本文" not in out
    _assert_all_dirnames(out)


def test_inline_doi_ref_and_slash_doi_normalized():
    """方法论连接：文献N（DOI）→ 目录名；带 "/" 的 DOI → "_"。"""
    data = {"summary": "s",
            "methodology_connections": [
                {"from": "本文", "to": "文献2（10.1016/j.snb.2022.132616）",
                 "relation": "r"}]}
    out = _render_relations(META, data, [])
    assert f"[[{SELF_DIR}/_note]]" in out
    assert "[[10.1016_j.snb.2022.132616/_note]]" in out
    assert "[[文献" not in out
    _assert_all_dirnames(out)


def test_related_list_numbered_and_dirname():
    """文末列表有序编号，且目标为目录名。"""
    ctxs = [{"doi": "10.1038/ncomms8258", "connection": "note"},
            {"doi": "10.1016/j.snb.2022.132616", "connection": "wiki"}]
    out = _render_relations(META, {"summary": "s"}, ctxs)
    assert "1. [[10.1038_ncomms8258/_note]]" in out
    assert "2. [[10.1016_j.snb.2022.132616/_note]]" in out
    _assert_all_dirnames(out)
