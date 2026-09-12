# -*- coding: utf-8 -*-
"""存储布局约定单测（P0-B step3，2026-09-11）。

锁定三条约定：
1. 类型由 RID 前缀表达（`book__`/`thesis__`/…），显式 kind 优先；
2. 依附资料挂父资源目录 `attachments/<kind>/`（删/备份成组）；
3. **`attachments/` 是用户资料，解析清理必须被守卫拦住**（防误删）。
"""
from __future__ import annotations

import pytest

from paperkb.layout import (assert_safe_to_clear, attachments_dir, is_attachment_path,
                            kind_for, kind_from_rid, resource_dir,
                            standalone_attachment_dir)

from paperkb.doi import make_rid
from paperkb.models import PaperMeta


# ---------------------------------------------------------------- 类型

def test_kind_from_rid_prefixes():
    assert kind_from_rid("doi-10.1002_adma.202407106") == "paper"
    assert kind_from_rid("nd-3f9a1c22b7d4") == "paper"
    assert kind_from_rid("book__isbn-9783527345678") == "book"
    assert kind_from_rid("thesis__cnki-CDFD2019012345") == "thesis"
    assert kind_from_rid("std__GB-T-12345-2020") == "standard"
    assert kind_from_rid("patent__CN202310123456") == "patent"
    assert kind_from_rid("si__doi-10.1002_adma.202407106") == "si"
    assert kind_from_rid("review__doi-10.1002_adma.202407106") == "review"
    assert kind_from_rid("") == "paper"


def test_make_rid_encodes_kind_in_name():
    """单一根 + 名字前缀：资源管理器里按名排序即按类型聚在一起。"""
    assert make_rid("book", isbn="978-3-527-34567-8").startswith("book__")
    assert make_rid("thesis", cnki="CDFD2019012345").startswith("thesis__")
    assert make_rid("standard", report_no="GB/T 12345-2020").startswith("std__")
    # 论文保持短名（现有目录不受影响）
    assert make_rid("paper", doi="10.1002/adma.202407106") == "doi-10.1002_adma.202407106"


def test_kind_for_prefers_explicit_field():
    m = PaperMeta(rid="doi-10.1002_x.1", doi="10.1002/x.1", kind="book")
    assert kind_for(m) == "book", "显式 kind 优先（例如同 DOI 的书籍版）"
    m2 = PaperMeta(rid="book__isbn-9783527345678")
    assert kind_for(m2) == "book"
    m3 = PaperMeta(doi="10.1002/x.1")           # 无 rid 时按 DOI 兜底
    assert kind_for(m3) == "paper"


# ---------------------------------------------------------------- 路径

def test_attachments_live_under_parent(tmp_path):
    lib = tmp_path / "library"
    root = resource_dir(lib, "doi-10.1002_adma.202407106")
    assert root == lib / "doi-10.1002_adma.202407106"
    assert attachments_dir(root, "si") == root / "attachments" / "si"
    assert attachments_dir(root, "review") == root / "attachments" / "review"
    assert attachments_dir(root) == root / "attachments"
    # 同一篇文献的东西都在同一个目录树下（删/备份成组）
    assert str(attachments_dir(root, "si")).startswith(str(root))


def test_standalone_attachment_root_for_parentless(tmp_path):
    root = tmp_path / "attachments"
    assert standalone_attachment_dir(root, "note__weekly-2026-09") == \
        root / "note__weekly-2026-09"
    assert standalone_attachment_dir(root, "nd-abc", "data") == \
        root / "nd-abc" / "data"


# ---------------------------------------------------------------- 清理守卫

def test_cleanup_guard_protects_attachments(tmp_path):
    lib = tmp_path / "library"
    root = resource_dir(lib, "doi-10.1002_adma.202407106")
    (root / "attachments" / "si").mkdir(parents=True)
    (root / "images").mkdir(parents=True)

    assert is_attachment_path(root / "attachments" / "si", root)
    assert not is_attachment_path(root / "images", root)

    # 解析产物可清（不抛）
    assert_safe_to_clear(root / "images", root)
    assert_safe_to_clear(root / "document.json", root)
    # 用户附件必须被拦住
    with pytest.raises(PermissionError):
        assert_safe_to_clear(root / "attachments", root)
    with pytest.raises(PermissionError):
        assert_safe_to_clear(root / "attachments" / "si", root)
    with pytest.raises(PermissionError):
        assert_safe_to_clear(root / "attachments" / "si" / "file.pdf", root)
    # 资源目录本身不能整目录清（否则连同附件一起没了）
    with pytest.raises(PermissionError):
        assert_safe_to_clear(root / "attachments", root)
