# -*- coding: utf-8 -*-
"""身份原语单测（P0-B 统一身份，2026-09-11）。

锁定四条性质：
1. `doi_to_dirname` **单射**——旧实现让 10.1002/a_b == 10.1002/a/b == 10.1002/a:b
   （不同文献同一目录 = 串档）；
2. 向后兼容——常见 DOI 的目录名与旧实现完全一致（现有 library/kb 不迁移）；
3. `dirname_to_doi` 只认真实 DOI 形态——旧实现把 JMADE-D-26-04277_R1_reviewer
   这类文件名反推成"DOI"，制造幽灵文献；
4. `make_rid` —— 无 DOI 资料（中文文献/学位论文/书/章节/SI/审稿意见）共用一套身份。
"""
from __future__ import annotations

from paperkb.doi import (dir_to_key, dirname_to_doi, doi_to_dirname, is_doi,
                         make_rid)

REAL_DOIS = [
    "10.1002/adma.202407106",
    "10.1016/j.cej.2025.167798",
    "10.1038/ncomms8258",
    "10.1063/1.5004573",
]


class _MapStore:
    """最小 doi_md5_map 桩：key(目录名) → {"doi": ...}。"""

    def __init__(self, rows: dict):
        self.rows = rows

    def get_doi_md5_map(self, key):
        return self.rows.get(key)


# ---------------------------------------------------------------- is_doi

def test_is_doi_accepts_real_forms():
    assert is_doi("10.1002/adma.202407106")
    assert is_doi("10.1038/s41586-021-03819-2")
    assert is_doi("10.1002/a/b")          # 子路径型 DOI 合法


def test_is_doi_rejects_ghosts():
    """旧实现把审稿版文件名当 DOI（幽灵文献根因）。"""
    assert not is_doi("JMADE-D-26-04277/R1_reviewer")
    assert not is_doi("paper")
    assert not is_doi("")
    assert not is_doi("11.1002/x")        # 必须 10. 前缀


# ---------------------------------------------------------------- 单射 / 兼容

def test_dirname_is_injective_for_ambiguous_chars():
    a, b, c = (doi_to_dirname("10.1002/a_b"), doi_to_dirname("10.1002/a/b"),
               doi_to_dirname("10.1002/a:b"))
    assert len({a, b, c}) == 3, f"三个不同 DOI 必须得到三个不同目录名，实际 {a} {b} {c}"


def test_dirname_backward_compatible_for_existing_papers():
    """现有 4 篇的目录名必须不变（否则要迁移数据）。"""
    assert doi_to_dirname("10.1002/adma.202407106") == "10.1002_adma.202407106"
    assert doi_to_dirname("10.1016/j.cej.2025.167798") == "10.1016_j.cej.2025.167798"
    assert doi_to_dirname("10.1038/ncomms8258") == "10.1038_ncomms8258"
    assert doi_to_dirname("10.1063/1.5004573") == "10.1063_1.5004573"


def test_dirname_roundtrip_for_reversible_dois():
    for d in REAL_DOIS + ["10.1002/a_b", "10.1002/a/b", "10.1002/a:b"]:
        assert dirname_to_doi(doi_to_dirname(d)) == d, d


def test_dirname_with_unsafe_chars_is_unique_but_not_reversible():
    """含空格/括号的 DOI：目录名唯一（带消歧后缀），但反推交给 doi_md5_map。"""
    weird = "10.1002/(SICI)1099-0844(199912)17:4<241::AID-CBF845>3.0.CO;2-8"
    name = doi_to_dirname(weird)
    assert "--" in name, "含不安全字符时应追加消歧后缀"
    assert dirname_to_doi(name) == "", "后缀版不可逆 → 必须交给映射表解析"
    # 确定性 + 唯一性：同名 DOI 恒定；不同 DOI 即使净化后 base 相同也不碰撞
    assert doi_to_dirname(weird) == name
    assert doi_to_dirname(weird.replace("2-8", "2-9")) != name


# ---------------------------------------------------------------- dir_to_key

def test_dir_to_key_keeps_legacy_dirs_visible():
    """旧实现返回 ("","") 会让历史目录在列表里消失；现在保留为 dir 类型。"""
    key, kind = dir_to_key("JMADE-D-26-04277_R1_reviewer")
    assert (key, kind) == ("JMADE-D-26-04277_R1_reviewer", "dir")


def test_dir_to_key_resolves_unreversible_dir_via_map():
    weird = "10.1002/(SICI)1099-0844(199912)17:4<241::AID-CBF845>3.0.CO;2-8"
    name = doi_to_dirname(weird)
    key, kind = dir_to_key(name, _MapStore({name: {"doi": weird}}))
    assert (key, kind) == (weird, "doi")


def test_dir_to_key_md5_dir():
    h = "a" * 32
    assert dir_to_key(h) == (h, "md5")
    assert dir_to_key(h, _MapStore({h: {"doi": "10.1/x"}})) == ("10.1/x", "md5")
    assert dir_to_key("_qa") == ("", "")          # 下划线前缀：应用产物，非文献
    assert dir_to_key(".system") == ("", "")      # 点前缀：系统目录


# ---------------------------------------------------------------- RID

def test_make_rid_prefers_doi():
    rid = make_rid("paper", doi="10.1002/adma.202407106")
    assert rid == "doi-10.1002_adma.202407106"
    assert "/" not in rid and ":" not in rid, "RID 必须可直接做目录名"


def test_make_rid_for_non_doi_sources():
    assert make_rid("paper", isbn="978-3-527-34567-8") == "isbn-978-3-527-34567-8"
    assert make_rid("paper", cnki="CDFD2019012345") == "cnki-CDFD2019012345"
    assert make_rid("paper", arxiv="2401.12345") == "arxiv-2401.12345"
    assert make_rid("paper", report_no="NREL/TP-5000-12345") == "rep-NREL-TP-5000-12345"


def test_make_rid_fingerprint_is_stable_and_dedupes():
    fp = "3f9a1c22b7d4ee91"
    assert make_rid("paper", fingerprint=fp) == "nd-3f9a1c22b7d4"
    # 同一文件重复导入 → 同一 RID（天然去重）
    assert make_rid("paper", fingerprint=fp) == make_rid("paper", fingerprint=fp)
    assert make_rid("paper", fingerprint=fp) != make_rid("paper", fingerprint="aa" + fp[2:])


def test_make_rid_child_resources_attach_to_parent():
    parent = make_rid("paper", doi="10.1002/adma.202407106")
    si = make_rid("si", parent_rid=parent)
    review = make_rid("review", parent_rid=parent)
    chapter = make_rid("chapter", isbn="9783527345678", parent_rid="isbn-9783527345678")
    assert si == "si__doi-10.1002_adma.202407106"
    assert review == "review__doi-10.1002_adma.202407106"
    assert chapter == "chapter__isbn-9783527345678"
    # 子资源与正文物理隔离：三个 RID 互不相同
    assert len({parent, si, review}) == 3


def test_make_rid_empty_falls_back():
    assert make_rid() == "nd-untitled"
