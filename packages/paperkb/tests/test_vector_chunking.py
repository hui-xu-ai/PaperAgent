# -*- coding: utf-8 -*-
"""向量索引分块/增量/命中块回取单测（2026-09-21 RAG 审计修复）。

覆盖：
- 分块键（首块 `{doi}__{ptype}`，附加块 `#n`）与"命中段 = 注入段"
- 内容 hash 增量：内容变才重嵌；分块数减少要清理旧键（旧实现永不刷新 → 索引与磁盘脱节）
- frontmatter 不进嵌入/片段
- `shared_concepts` 是真实交集（旧实现谎报全部查询概念）
- 落盘/重载后仍能按偏移切回原文

embedding 一律 mock（不依赖 SiliconFlow API）。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from paperkb.config import Roots
from paperkb.vector import EMBEDDING_DIM, KbVectorIndex


@pytest.fixture()
def roots(tmp_path: Path) -> Roots:
    return Roots(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        kb_dir=tmp_path / "kb",
    ).ensure()


def _fake_embedding(text: str) -> list[float]:
    rng = np.random.RandomState(hash(text) % (2**31))
    vec = rng.randn(EMBEDDING_DIM).astype(np.float32)
    norm = np.linalg.norm(vec)
    return (vec / norm if norm > 0 else vec).tolist()


def _mock_encode_texts(texts, **kwargs):
    return [_fake_embedding(t) for t in texts]


def _mock_encode_query(query, **kwargs):
    return _fake_embedding(query)


def _paper_dir(roots: Roots, doi: str, name: str, body: str) -> Path:
    d = roots.kb_dir / doi.replace("/", "_")
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(body, encoding="utf-8")
    return d


LONG_NOTE = (
    "---\ntype: paper-note\ndoi: 10.1234/chunk\n---\n"
    "# 样例论文标题\n\n"
    "## 一句话贡献\n> 一句话。\n\n"
    + "## 研究背景\n" + "背景内容。" * 60 + "\n\n"
    + "## 研究方法\n" + "方法内容。" * 60 + "\n\n"
    + "## 研究结果\n" + "结果内容。" * 60 + "\n"
)


class TestChunking:
    @patch("paperlit.vector.encode_texts", side_effect=_mock_encode_texts)
    def test_long_note_split_into_chunk_keys(self, mock_enc, roots):
        idx = KbVectorIndex(roots, api_key="k")
        idx.index_paper("10.1234/chunk", note_text=LONG_NOTE)
        assert "10.1234/chunk__note" in idx._key_to_idx
        assert any(k.startswith("10.1234/chunk__note#") for k in idx._key_to_idx), \
            "长笔记应切成多块"

    @patch("paperlit.vector.encode_texts", side_effect=_mock_encode_texts)
    def test_short_note_single_chunk(self, mock_enc, roots):
        idx = KbVectorIndex(roots, api_key="k")
        idx.index_paper("10.1234/s", note_text="# T\n" + "x" * 200)
        assert list(idx._key_to_idx) == ["10.1234/s__note"]

    @patch("paperlit.vector.encode_texts", side_effect=_mock_encode_texts)
    def test_frontmatter_not_indexed(self, mock_enc, roots):
        idx = KbVectorIndex(roots, api_key="k")
        idx.index_paper("10.1234/chunk", note_text=LONG_NOTE)
        for key, meta in idx._key_meta.items():
            if meta["ptype"] != "note":
                continue
            assert "type: paper-note" not in idx.passage_text(key)


class TestIncrementalRefresh:
    @patch("paperlit.vector.encode_texts", side_effect=_mock_encode_texts)
    def test_unchanged_content_not_reembedded(self, mock_enc, roots):
        idx = KbVectorIndex(roots, api_key="k")
        idx.index_paper("10.1234/a", note_text="# T\n" + "x" * 300)
        assert mock_enc.call_count == 1
        added = idx.index_paper("10.1234/a", note_text="# T\n" + "x" * 300)
        assert added == 0 and mock_enc.call_count == 1, "内容未变不应重嵌"

    @patch("paperlit.vector.encode_texts", side_effect=_mock_encode_texts)
    def test_changed_content_refreshes_vector(self, mock_enc, roots):
        idx = KbVectorIndex(roots, api_key="k")
        idx.index_paper("10.1234/a", note_text="# T\n" + "x" * 300)
        before = idx._vectors.copy()
        idx.index_paper("10.1234/a", note_text="# T\n" + "y" * 300)
        assert mock_enc.call_count == 2, "内容变了必须重嵌（旧实现永不刷新）"
        assert not np.allclose(before, idx._vectors)
        assert idx.size == 1, "同一键应原地刷新，不新增"

    @patch("paperlit.vector.encode_texts", side_effect=_mock_encode_texts)
    def test_shrunk_text_removes_stale_chunk_keys(self, mock_enc, roots):
        idx = KbVectorIndex(roots, api_key="k")
        idx.index_paper("10.1234/a", note_text=LONG_NOTE)
        long_keys = {k for k in idx._key_to_idx if k.startswith("10.1234/a__note")}
        assert len(long_keys) > 1
        idx.index_paper("10.1234/a", note_text="# T\n" + "短的正文。" * 10)
        now = {k for k in idx._key_to_idx if k.startswith("10.1234/a__note")}
        assert now == {"10.1234/a__note"}, f"旧块键未清理: {now}"

    @patch("paperlit.vector.encode_texts", side_effect=_mock_encode_texts)
    def test_force_reembeds_same_content(self, mock_enc, roots):
        idx = KbVectorIndex(roots, api_key="k")
        idx.index_paper("10.1234/a", note_text="# T\n" + "x" * 300)
        idx.index_paper("10.1234/a", note_text="# T\n" + "x" * 300, force=True)
        assert mock_enc.call_count == 2


class TestPassageText:
    @patch("paperlit.vector.encode_texts", side_effect=_mock_encode_texts)
    def test_snippet_equals_indexed_chunk(self, mock_enc, roots):
        from paperkb.textseg import strip_frontmatter

        d = _paper_dir(roots, "10.1234/a", "_note.md", LONG_NOTE)
        idx = KbVectorIndex(roots, api_key="k")
        idx.index_paper("10.1234/a", note_text=LONG_NOTE, folder=d)
        key = sorted(k for k in idx._key_to_idx if k.startswith("10.1234/a__note"))[0]
        text = idx.passage_text(key)
        body = strip_frontmatter(LONG_NOTE)
        assert text and "type: paper-note" not in text
        assert text in body, "命中块必须是原文的连续切片"

    @patch("paperlit.vector.encode_texts", side_effect=_mock_encode_texts)
    def test_file_changed_returns_empty(self, mock_enc, roots):
        d = _paper_dir(roots, "10.1234/a", "_note.md", LONG_NOTE)
        idx = KbVectorIndex(roots, api_key="k")
        idx.index_paper("10.1234/a", note_text=LONG_NOTE, folder=d)
        (d / "_note.md").write_text("# 换了一篇完全不同的内容\n" + "z" * 200,
                                    encoding="utf-8")
        key = f"10.1234/a__note"
        assert idx.passage_text(key) == "", "偏移失准时应返回空，不给错位证据"

    @patch("paperlit.vector.encode_texts", side_effect=_mock_encode_texts)
    def test_survives_reload(self, mock_enc, roots):
        d = _paper_dir(roots, "10.1234/a", "_note.md", LONG_NOTE)
        idx = KbVectorIndex(roots, api_key="k")
        idx.index_paper("10.1234/a", note_text=LONG_NOTE, folder=d)
        idx2 = KbVectorIndex(roots, api_key="k")
        assert idx2.size == idx.size
        assert idx2.passage_text("10.1234/a__note")


class TestSearchSnippet:
    @patch("paperlit.vector.encode_query", side_effect=_mock_encode_query)
    @patch("paperlit.vector.encode_texts", side_effect=_mock_encode_texts)
    def test_search_with_snippet_returns_chunk(self, mock_enc, mock_q, roots):
        d = _paper_dir(roots, "10.1234/a", "_note.md", LONG_NOTE)
        idx = KbVectorIndex(roots, api_key="k")
        idx.index_paper("10.1234/a", note_text=LONG_NOTE, folder=d)
        hits = idx.search("研究结果", top_k=3, with_snippet=True)
        assert hits and any(h.get("snippet") for h in hits)
        assert all("section" in h and "chunk" in h for h in hits)

    @patch("paperlit.vector.encode_query", side_effect=_mock_encode_query)
    @patch("paperlit.vector.encode_texts", side_effect=_mock_encode_texts)
    def test_search_by_concepts_shared_intersection(self, mock_enc, mock_q, roots):
        from paperkb.db import KBStore

        store = KBStore(roots)
        store.init_schema()
        store.upsert_concept("10.1234/a", "gnn", "图神经网络")
        store.upsert_concept("10.1234/b", "gnn", "图神经网络变体")
        store.upsert_concept("10.1234/b", "imc", "离子迁移")
        idx = KbVectorIndex(roots, api_key="k")
        idx.index_paper("10.1234/a", note_text="# A\n" + "x" * 200)
        idx.index_paper("10.1234/b", note_text="# B\n" + "y" * 200)

        res = idx.search_by_concepts(["gnn", "imc"], top_k=5, store=store)
        by_doi = {r["doi"]: r["shared_concepts"] for r in res}
        assert by_doi["10.1234/a"] == ["gnn"], "只共有一个概念时不得谎报两个"
        assert set(by_doi["10.1234/b"]) == {"gnn", "imc"}


# 一个 2000 字的单小节笔记：同小节会被切成 ≥2 块（small-to-big 的用武之地）
LONG_SECTION_NOTE = ("---\ntype: paper-note\ndoi: 10.1234/sec\n---\n"
                     "# 长节样例\n\n## 研究背景\n" + "背景句子内容。" * 400)


class TestSectionExpand:
    """small-to-big：命中块 → 所属小节整段（小块匹配、大块注入）。"""

    def _index(self, roots, body=LONG_SECTION_NOTE, doi="10.1234/sec"):
        d = _paper_dir(roots, doi, "_note.md", body)
        idx = KbVectorIndex(roots, api_key="k")
        idx.index_paper(doi, note_text=body, folder=d)
        keys = sorted((k for k in idx._key_to_idx if k.startswith(f"{doi}__note")),
                      key=lambda k: int(idx._key_meta[k]["start"]))
        return idx, keys, d

    @patch("paperlit.vector.encode_texts", side_effect=_mock_encode_texts)
    def test_expands_to_whole_section(self, mock_enc, roots):
        idx, keys, _d = self._index(roots)
        assert len(keys) >= 2, "样例小节应切成多块"
        first = keys[0]
        single = idx.passage_text(first)
        merged = idx.section_text(first)          # expand_section 默认走这条
        assert len(single) > 500
        assert len(merged) > len(single), "应扩展到小节整段（不止命中那一块）"
        assert merged in LONG_SECTION_NOTE, "扩展片段必须是原文的连续切片"
        second_start = int(idx._key_meta[keys[1]]["start"])
        first_start = int(idx._key_meta[first]["start"])
        assert len(merged) > second_start - first_start, "应覆盖到下一块的起点"

    @patch("paperlit.vector.encode_texts", side_effect=_mock_encode_texts)
    def test_respects_limit(self, mock_enc, roots):
        idx, keys, _d = self._index(roots)
        assert len(idx.section_text(keys[0], limit=600)) <= 600

    @patch("paperlit.vector.encode_texts", side_effect=_mock_encode_texts)
    def test_file_changed_falls_back_to_single_chunk(self, mock_enc, roots):
        idx, keys, d = self._index(roots)
        (d / "_note.md").write_text("# 完全换了内容\n" + "z" * 300, encoding="utf-8")
        assert idx.section_text(keys[0]) == "", "偏移失准宁可返回空，不给错位证据"

    @patch("paperlit.vector.encode_texts", side_effect=_mock_encode_texts)
    def test_single_chunk_section_not_expanded(self, mock_enc, roots):
        idx, keys, _d = self._index(
            roots, body="# T\n\n## 小节\n" + "只有一段短内容。" * 10,
            doi="10.1234/one")
        assert keys == ["10.1234/one__note"]
        assert idx.section_text(keys[0]) == idx.passage_text(keys[0])


class TestQueryVecCache:
    """查询向量缓存：同一问句第二次不再调 embedding API。"""

    @patch("paperlit.vector.encode_query", side_effect=_mock_encode_query)
    @patch("paperlit.vector.encode_texts", side_effect=_mock_encode_texts)
    def test_second_identical_query_hits_cache(self, mock_enc, mock_q, roots):
        body = "# T\n" + "正文内容。" * 100
        d = _paper_dir(roots, "10.1234/a", "_note.md", body)
        idx = KbVectorIndex(roots, api_key="k")
        idx.index_paper("10.1234/a", note_text=body, folder=d)
        idx.search("同一个问句", top_k=2)
        idx.search("同一个问句", top_k=2)
        assert mock_q.call_count == 1, "重复查询应命中 SQLite 缓存"
        assert (roots.vector_dir / "query_cache.db").exists()

    @patch("paperlit.vector.encode_query", side_effect=_mock_encode_query)
    @patch("paperlit.vector.encode_texts", side_effect=_mock_encode_texts)
    def test_disabled_by_setting(self, mock_enc, mock_q, roots, monkeypatch):
        from paperkb import api

        class _S:
            query_vec_cache = False

        monkeypatch.setattr(api, "_settings", _S(), raising=False)
        body = "# T\n" + "正文内容。" * 100
        d = _paper_dir(roots, "10.1234/a", "_note.md", body)
        idx = KbVectorIndex(roots, api_key="k")
        idx.index_paper("10.1234/a", note_text=body, folder=d)
        idx.search("同一个问句", top_k=2)
        idx.search("同一个问句", top_k=2)
        assert mock_q.call_count == 2, "关掉缓存就该每次真调"

    def test_roundtrip_and_miss(self, roots):
        from paperkb.db import QueryVecCache

        c = QueryVecCache(roots)
        c.put("m", "问句", [1.0, 2.0, 3.0])
        assert c.get("m", "问句") == [1.0, 2.0, 3.0]
        assert c.get("m", "别的问句") == []
        assert c.get("other-model", "问句") == []
        # 派生缓存落在数据层（vector_dir），不进 biblio 主库
        assert c.path == roots.vector_dir / "query_cache.db"
