# -*- coding: utf-8 -*-
"""多路召回融合（RRF）与二阶段重排单测（2026-09-21 检索质量增强）。

覆盖：
- RRF：同一 (doi,file) 多路命中**累加**、通道记账、片段取更长者、向量片段优先作注入文本
- 排序：多路都召回的 > 单路排第一（RRF 的用意；旧实现是硬编码权重 1.0/0.9/0.6）
- 重排：写 `rerank_score` 并按分数重排；抛错/关开关/无 key → 退回 RRF 顺序

重排一律 mock（不依赖 SiliconFlow API）。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from paperkb import api
from paperkb.config import Roots
from paperkb.models import PaperMeta
from paperkb.retrieve import _rrf_fuse, _rrf_k, _rerank_items


class TestRrfFusion:
    def test_same_doc_across_channels_accumulates(self):
        """多路命中同一 (doi,file) → 分数累加（这正是 RRF 的用意）。"""
        out = _rrf_fuse([
            ("notes", 1.0, [{"doi": "10.1/a", "file": "_note.md", "snippet": "短"}]),
            ("vector", 1.0, [{"doi": "10.1/a", "file": "_note.md",
                              "snippet": "向量命中块原文" * 10}]),
        ], k=60)
        assert len(out) == 1
        assert out[0]["rrf"] == pytest.approx(1 / 61 + 1 / 61)
        assert out[0]["channels"] == ["notes", "vector"]
        assert out[0]["source"] == "vector", "向量片段即命中段，应优先作注入文本"
        assert len(out[0]["snippet"]) > 10, "片段应取信息量更大的那个"

    def test_snippet_keeps_longer_one(self):
        """FTS 窗口比向量块更长时，保留更长片段（别把证据缩短）。"""
        long_snip = "很长的 FTS 窗口片段。" * 20
        out = _rrf_fuse([
            ("vector", 1.0, [{"doi": "10.1/a", "file": "_note.md",
                              "snippet": "短块"}]),
            ("notes", 1.0, [{"doi": "10.1/a", "file": "_note.md",
                             "snippet": long_snip}]),
        ], k=60)
        assert out[0]["snippet"] == long_snip

    def test_multi_channel_beats_single_top_rank(self):
        """两路都排第一的条目应压过单路排第一的条目。"""
        out = _rrf_fuse([
            ("notes", 1.0, [{"doi": "top", "file": "_note.md", "snippet": "s"}]),
            ("meta", 0.5, [{"doi": "x", "file": "_meta", "snippet": "m"}]),
            ("vector", 1.0, [{"doi": "x", "file": "_meta", "snippet": "m"},
                             {"doi": "other", "file": "_note.md", "snippet": "o"}]),
        ], k=60)
        assert [it["doi"] for it in out] == ["x", "top", "other"]

    def test_same_doi_different_file_not_merged(self):
        """(doi,file) 才是条目身份：同一篇的笔记与元数据片段不得合并。"""
        out = _rrf_fuse([
            ("notes", 1.0, [{"doi": "10.1/a", "file": "_note.md", "snippet": "n"}]),
            ("meta", 0.5, [{"doi": "10.1/a", "file": "_meta", "snippet": "m"}]),
        ], k=60)
        assert len(out) == 2
        assert {it["file"] for it in out} == {"_note.md", "_meta"}

    def test_rrf_k_from_settings(self):
        assert _rrf_k() == 60


@pytest.fixture()
def env(tmp_path: Path):
    roots = Roots(data_dir=tmp_path / "data",
                  library_dir=tmp_path / "library",
                  kb_dir=tmp_path / "kb").ensure()
    api.init_kb(roots)
    yield roots
    api._store = None
    api._journals = None
    api._compiler = None
    api._settings = None


def _mk_paper(roots: Roots, doi: str, title: str) -> None:
    api._need_store().upsert_meta(  # noqa: SLF001
        PaperMeta(doi=doi, title=title, journal="J", year="2024",
                  abstract="abs", source_file="t.bib"))


def _vector_hits(*dois: str) -> list[dict]:
    return [{"key": f"{d}__note", "doi": d, "passage_type": "note", "chunk": 0,
             "section": "方法", "score": 0.9 - i * 0.1,
             "snippet": f"{d} 的方法段落原文。" * 3}
            for i, d in enumerate(dois)]


class TestRerank:
    @patch("paperlit.vector.reranker.rerank")
    @patch("paperkb.api.kb_vector_search")
    def test_rerank_reorders_pool(self, mock_search, mock_rr, env, monkeypatch):
        monkeypatch.setenv("SILICONFLOW_RERANK_API_KEY", "test-key")
        _mk_paper(env, "10.1000/a", "Alpha")
        _mk_paper(env, "10.1000/b", "Beta")
        mock_search.return_value = _vector_hits("10.1000/a", "10.1000/b")
        mock_rr.return_value = [(1, 0.9), (0, 0.1)]      # 把 B 排到前面

        items = api.recall("zzz-不存在的问句", top_k=2, rerank=True)

        assert [it["doi"] for it in items] == ["10.1000/b", "10.1000/a"]
        assert items[0]["rerank_score"] == pytest.approx(0.9)
        assert mock_rr.call_count == 1
        assert len(mock_rr.call_args.kwargs["documents"]) == 2

    @patch("paperlit.vector.reranker.rerank", side_effect=RuntimeError("boom"))
    @patch("paperkb.api.kb_vector_search")
    def test_rerank_failure_keeps_rrf_order(self, mock_search, mock_rr, env,
                                            monkeypatch):
        monkeypatch.setenv("SILICONFLOW_RERANK_API_KEY", "test-key")
        _mk_paper(env, "10.1000/a", "Alpha")
        _mk_paper(env, "10.1000/b", "Beta")
        mock_search.return_value = _vector_hits("10.1000/a", "10.1000/b")

        items = api.recall("zzz-不存在的问句", top_k=2, rerank=True)

        assert [it["doi"] for it in items] == ["10.1000/a", "10.1000/b"]
        assert all("rerank_score" not in it for it in items)

    @patch("paperlit.vector.reranker.rerank")
    @patch("paperkb.api.kb_vector_search")
    def test_no_key_skips_rerank(self, mock_search, mock_rr, env, monkeypatch):
        """无 rerank key → 静默跳过（重排是加分项，不该影响召回）。"""
        monkeypatch.delenv("SILICONFLOW_RERANK_API_KEY", raising=False)
        monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
        _mk_paper(env, "10.1000/a", "Alpha")
        mock_search.return_value = _vector_hits("10.1000/a")
        api.recall("zzz", rerank=True)
        assert mock_rr.call_count == 0

    @patch("paperlit.vector.reranker.rerank")
    @patch("paperkb.api.kb_vector_search")
    def test_explicit_false_overrides_setting(self, mock_search, mock_rr, env,
                                              monkeypatch):
        monkeypatch.setenv("SILICONFLOW_RERANK_API_KEY", "test-key")
        _mk_paper(env, "10.1000/a", "Alpha")
        mock_search.return_value = _vector_hits("10.1000/a")
        api.recall("zzz", rerank=False)
        assert mock_rr.call_count == 0

    @patch("paperlit.vector.reranker.rerank", side_effect=ImportError("no paperlit"))
    def test_rerank_items_silent_on_import_error(self, mock_rr, env, monkeypatch):
        monkeypatch.setenv("SILICONFLOW_RERANK_API_KEY", "test-key")
        items = [{"doi": "a", "snippet": "x"}, {"doi": "b", "snippet": "y"}]
        _rerank_items("q", items)
        assert all("rerank_score" not in it for it in items)

    @patch("paperlit.vector.reranker.rerank")
    def test_rerank_items_writes_by_original_index(self, mock_rr, env, monkeypatch):
        monkeypatch.setenv("SILICONFLOW_RERANK_API_KEY", "test-key")
        mock_rr.return_value = [(1, 0.8), (0, 0.2), (99, 0.5)]
        items = [{"doi": "a", "snippet": "x"}, {"doi": "b", "snippet": "y"}]
        _rerank_items("q", items)
        assert items[0]["rerank_score"] == pytest.approx(0.2)
        assert items[1]["rerank_score"] == pytest.approx(0.8)

    @patch("paperlit.vector.reranker.rerank")
    def test_rerank_items_skips_when_no_text(self, mock_rr, env, monkeypatch):
        monkeypatch.setenv("SILICONFLOW_RERANK_API_KEY", "test-key")
        _rerank_items("q", [{"doi": "a", "snippet": ""}, {"doi": "b"}])
        assert mock_rr.call_count == 0


class TestRecallShape:
    @patch("paperkb.api.kb_vector_search")
    def test_items_carry_rrf_and_channels(self, mock_search, env):
        _mk_paper(env, "10.1000/a", "Alpha")
        mock_search.return_value = _vector_hits("10.1000/a")
        items = api.recall("zzz")
        assert items
        for it in items:
            assert it["rrf"] > 0
            assert it["channels"]
            assert it["rid"] and it["kind"], "api 门面应补 rid/kind"
