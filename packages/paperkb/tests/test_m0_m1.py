# -*- coding: utf-8 -*-
"""M0+M1 单测：paperkb 包骨架 + bib 导入（真实 savedrecs_1.bib + tmp 库隔离）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from paperkb import api
from paperkb.bib_parser import parse_bib, parse_bib_file
from paperkb.config import Roots
from paperkb.doi import doi_to_dirname, normalize_doi

REAL_BIB = Path(__file__).resolve().parents[3] / "用户提供的文献" / "Bib文件" / "savedrecs_1.bib"
REAL_BIB_EXISTS = REAL_BIB.exists()


@pytest.fixture()
def roots(tmp_path: Path) -> Roots:
    return Roots(data_dir=tmp_path / "data",
                 library_dir=tmp_path / "library",
                 kb_dir=tmp_path / "kb").ensure()


@pytest.fixture()
def store(roots: Roots):
    api.init_kb(roots)
    yield api._store  # noqa: SLF001
    api._store = None
    api._settings = None  # noqa: SLF001


# ---------------------------------------------------------------- 解析器

def test_parse_bib_simple():
    text = """@article{WOS:001, Author = {Xu, Hui and Liu, B},
 Title = {A test {[}with{]}\\& braces}, Year = {2026},
 Abstract = {Multi
 line abstract.}, DOI = {10.1000/abc.1},}
"""
    recs = parse_bib(text)
    assert len(recs) == 1
    f = recs[0].fields
    assert f["author"] == "Xu, Hui and Liu, B"
    assert f["year"] == "2026"
    assert "Multi\n line abstract." in f["abstract"]
    assert "with" in f["title"] and "\\&" in f["title"]


def test_parse_bib_real_file():
    if not REAL_BIB_EXISTS:
        pytest.skip("真实 bib 不存在")
    recs = parse_bib(REAL_BIB.read_text(encoding="utf-8", errors="replace"))
    assert len(recs) == 1
    f = recs[0].fields
    assert f["doi"] == "10.1007/s40843-026-4226-3"
    assert "Cited-References" in f or "cited-references" in f
    assert "Affiliation" in f or "affiliation" in f


def test_parse_bib_file_meta():
    if not REAL_BIB_EXISTS:
        pytest.skip("真实 bib 不存在")
    metas = parse_bib_file(REAL_BIB)
    assert len(metas) == 1
    m = metas[0]
    assert m.doi == "10.1007/s40843-026-4226-3"
    assert m.title.startswith("3D printing of electro-ionic artificial muscles")
    assert len(m.authors) == 8
    assert m.journal == "SCIENCE CHINA-MATERIALS"
    assert m.year == "2026"
    assert m.times_cited == 0
    assert len(m.references) == 54
    # 引用条目解析：应含 DOI 与非 DOI 两种形态
    with_doi = [r for r in m.references if r.doi]
    assert len(with_doi) > 40
    assert m.affiliations, "应解析出机构列表"
    assert any("Jilin Univ" in a for a in m.affiliations)


# ---------------------------------------------------------------- DOI 工具

def test_doi_utils():
    assert normalize_doi("  10.1002/adma.202407106  ") == "10.1002/adma.202407106"
    assert doi_to_dirname("10.1002/adma.202407106") == "10.1002_adma.202407106"
    assert doi_to_dirname("10.1016/j.cej.2025.167798") == "10.1016_j.cej.2025.167798"
    assert normalize_doi("") == ""


def test_is_md5_dir():
    from paperkb.doi import is_md5_dir

    md5 = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
    assert is_md5_dir(md5)
    assert not is_md5_dir("10.1016_j.cej.2025.167798")
    assert not is_md5_dir("paper_abc")
    assert not is_md5_dir("")
    assert not is_md5_dir(md5 + "0")  # 33 位非 32hex


def test_dir_to_key():
    """T6：DOI 目录 → (doi,"doi")；md5 目录经 map 反查 → (DOI 或 md5,"md5")。"""
    from paperkb.doi import dir_to_key

    md5 = "a" * 32
    # DOI 目录直接反推
    assert dir_to_key("10.1016_j.cej.2025.167798") == ("10.1016/j.cej.2025.167798", "doi")
    # md5 目录无 map → 用 md5 作标识
    assert dir_to_key(md5) == (md5, "md5")
    # 其他目录 → 空
    assert dir_to_key("_Mineru网页下载") == ("", "")

    class _Map:
        def __init__(self, doi=""):
            self.doi = doi

        def get_doi_md5_map(self, key):
            return {"doi": self.doi} if key == md5 else None

    # md5 目录 map 命中 → 用真实 DOI 作标识
    assert dir_to_key(md5, _Map("10.1000/md5p.1")) == ("10.1000/md5p.1", "md5")
    # 反查失败（抛异常）按未映射处理
    class _Boom:
        def get_doi_md5_map(self, key):
            raise RuntimeError("db locked")
    assert dir_to_key(md5, _Boom()) == (md5, "md5")


# ---------------------------------------------------------------- 导入链路

def test_bib_import_and_query(store, roots):
    if not REAL_BIB_EXISTS:
        pytest.skip("真实 bib 不存在")
    prev = api.bib_preview(REAL_BIB)
    assert prev["records"] == 1
    assert prev["valid"] == 1
    assert prev["sample"][0]["refs"] == 54

    r = api.bib_import(REAL_BIB)
    assert r["imported"] == 1
    assert r["citations"] == 54
    assert r["updated"] == 0

    # 幂等：重导 = 更新
    r2 = api.bib_import(REAL_BIB)
    assert r2["imported"] == 0 and r2["updated"] == 1

    meta = api.get_paper_meta("10.1007/s40843-026-4226-3")
    assert meta and meta["title"].startswith("3D printing")
    assert len(meta["references"]) == 54

    # 引用关系
    rel = api.citations_for("10.1007/s40843-026-4226-3")
    assert len(rel["cited"]) == 54
    # 出边中应含库里已有/常见 DOI（如 ADMA 10.1002/adma.202302066）
    cited_dois = {c["doi"] for c in rel["cited"]}
    assert "10.1002/adma.202302066" in cited_dois

    # 列表与搜索
    lst = api.list_papers_meta()
    assert len(lst) == 1
    hits = api.search_papers_meta("electro-ionic artificial muscles")
    assert len(hits) == 1 and hits[0]["doi"] == meta["doi"]


def test_missing_dois_and_wos_query(store, roots):
    # 造一个 library 内容（目录名 = DOI 规范化），无 papers_meta
    (roots.library_dir / "10.1002_adma.202407106").mkdir(parents=True)
    (roots.library_dir / "10.1002_adma.202407106" / "document.json").write_text(
        json.dumps({"metadata": {"doi": "10.1002/adma.202407106"}}), encoding="utf-8")
    # kb 里造一个（目录名反推路径）
    (roots.kb_dir / "10.1016_j.cej.2025.167798").mkdir(parents=True)

    missing = api.missing_dois(roots)
    assert "10.1002/adma.202407106" in missing
    assert "10.1016/j.cej.2025.167798" in missing

    queries = api.wos_query(missing, batch=1)
    assert len(queries) == 2
    assert queries[0].startswith("DO=(")


def test_import_requires_init(tmp_path):
    with pytest.raises(RuntimeError):
        api.bib_preview(tmp_path / "x.bib")
