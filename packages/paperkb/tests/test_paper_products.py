# -*- coding: utf-8 -*-
"""`kb_paper_products`：单篇编译产物**整份正文**出口（按份边界对齐截断 + 截断量上报）。

为什么单独立测：这是"召回只给片段"的补全出口——agent 需要完整公式/小节时显式取整份。
必须保证：① 真的给到整份（不被 3000 字砍成片段级）；② 被截时必须**如实上报**
（`chars.truncated`），否则调用方会"以为拿到全文"。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from paperkb import api
from paperkb.config import Roots
from paperkb.doi import doi_to_dirname

DOI = "10.1234/prod"


@pytest.fixture()
def kbsetup(tmp_path: Path):
    roots = Roots(data_dir=tmp_path / "data", library_dir=tmp_path / "library",
                  kb_dir=tmp_path / "kb").ensure()
    api.init_kb(roots)
    folder = roots.kb_dir / doi_to_dirname(DOI)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "_note.md").write_text(
        f"---\ndoi: {DOI}\ntype: paper-note\n---\n"
        "# \u6807\u9898\n\n## \u4e00\u53e5\u8bdd\u8d21\u732e\n> \u4e00\u53e5\u8bdd\u3002\n\n"
        + "## \u5c0f\u8282 A\n" + "\u6b63\u6587\u5185\u5bb9\u3002" * 200 + "\n",
        encoding="utf-8")
    (folder / "_wiki.md").write_text(
        f"---\ndoi: {DOI}\n---\n# Wiki\n" + "W" * 500, encoding="utf-8")
    yield api, roots
    api._store = None          # noqa: SLF001
    api._settings = None       # noqa: SLF001


def test_returns_whole_products_without_frontmatter(kbsetup):
    got = kbsetup[0].kb_paper_products(DOI)
    assert got["found"] is True
    assert set(got["products"]) == {"note", "wiki"}
    # frontmatter 不进正文（与嵌入/片段同一口径）
    assert "type: paper-note" not in got["products"]["note"]
    assert got["products"]["note"].startswith("# \u6807\u9898")
    assert got["chars"]["wiki"]["truncated"] is False
    # 未超上限 → 原样返回（# Wiki\n + 500 个 W）
    assert got["chars"]["wiki"]["returned"] == got["chars"]["wiki"]["full"] == 507


def test_truncation_is_reported_not_silent(kbsetup):
    """超上限时如实标记 truncated 并给出原始长度（避免"以为拿到全文"）。"""
    got = kbsetup[0].kb_paper_products(DOI, max_chars=600)
    c = got["chars"]["note"]
    assert c["truncated"] is True
    assert c["returned"] < c["full"]
    assert c["returned"] <= 600
    assert c["full"] > 600


def test_files_selection(kbsetup):
    got = kbsetup[0].kb_paper_products(DOI, files="wiki")
    assert set(got["products"]) == {"wiki"}
    assert got["missing"] == []           # 没要的产物不算 missing


def test_missing_and_unknown_are_reported(kbsetup):
    got = kbsetup[0].kb_paper_products(DOI, files="note,wiki,relations")
    assert got["missing"] == ["relations"]        # 该篇没编译 L3
    assert "relations" not in got["products"]

    gone = kbsetup[0].kb_paper_products("10.9999/none")
    assert gone["found"] is False and gone["products"] == {}
    assert "hint" in gone


def test_max_chars_is_clamped(kbsetup):
    """单份上限有硬顶（防一次取回把上下文撑爆）。"""
    got = kbsetup[0].kb_paper_products(DOI, files="wiki", max_chars=10 ** 9)
    assert got["chars"]["wiki"]["returned"] == got["chars"]["wiki"]["full"]
    tiny = kbsetup[0].kb_paper_products(DOI, files="wiki", max_chars=1)
    assert tiny["chars"]["wiki"]["returned"] >= 1
