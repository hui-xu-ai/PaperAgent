# -*- coding: utf-8 -*-
"""扩容 P0 验收：`KbVectorIndex` 的落盘是**行级增量**，不是整份重写。

为什么单独立这个文件：10 万篇 ≈104 万块、向量 ≈4.3GB，旧实现每次编译一篇都整份重写
`kb_vectors.npy` + `kb_index_meta.json`（写放大 ≈2× 体积/篇、全量重建 O(N²)）。这些用例
钉住新契约：**段文件只追加**（既有字节不变）、元数据只 upsert 变更行、失败进死信。

embedding 一律 mock（不依赖 SiliconFlow API）。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from paperkb.config import Roots
from paperkb.index_store import reset_index_store
from paperkb.vector import EMBEDDING_DIM, KbVectorIndex, get_kb_vector_index, \
    reset_kb_vector_index

DOI_A = "10.1234/a"
DOI_B = "10.1234/b"


@pytest.fixture()
def roots(tmp_path: Path) -> Roots:
    r = Roots(data_dir=tmp_path / "data", library_dir=tmp_path / "library",
              kb_dir=tmp_path / "kb").ensure()
    yield r
    reset_index_store(r)
    reset_kb_vector_index(r)


def _fake_embedding(text: str) -> list[float]:
    rng = np.random.RandomState(hash(text) % (2**31))
    vec = rng.randn(EMBEDDING_DIM).astype(np.float32)
    norm = float(np.linalg.norm(vec))
    return (vec / norm if norm > 0 else vec).tolist()


def _mock_encode_texts(texts, **kwargs):
    return [_fake_embedding(t) for t in texts]


def _body(tag: str, paras: int = 30) -> str:
    return f"# {tag}\n\n" + f"## \u5c0f\u8282 {tag}\n".join(
        [f"\u5185\u5bb9 {tag} \u6bb5\u843d\u3002" * 8 + "\n\n" for _ in range(paras)])


def _seg_files(idx: KbVectorIndex) -> list[Path]:
    return sorted(idx._store.index_dir.glob("*.f32"))


@patch("paperlit.vector.encode_texts", _mock_encode_texts)
class TestIncrementalPersistence:
    def test_second_paper_appends_without_rewriting_first(self, roots):
        """追加第二篇：段数不变、文件**只增长**、首篇字节原封不动。"""
        idx = KbVectorIndex(roots, api_key="k")
        assert idx.index_paper(DOI_A, note_text=_body("A")) > 0
        segs = _seg_files(idx)
        assert len(segs) == 1
        head = segs[0].read_bytes()
        assert head, "段文件不应为空"

        assert idx.index_paper(DOI_B, note_text=_body("B")) > 0
        segs2 = _seg_files(idx)
        assert len(segs2) == 1, "追加不得另起新段"
        grown = segs2[0].read_bytes()
        assert len(grown) > len(head)
        assert grown[:len(head)] == head, "既有段的字节被改写了（不是追加）"

    def test_no_legacy_files_written(self, roots):
        """新实现不得再产出旧的整份 JSON / npy。"""
        idx = KbVectorIndex(roots, api_key="k")
        idx.index_paper(DOI_A, note_text=_body("A"))
        d = idx._store.index_dir
        assert not (d / "kb_index_meta.json").exists()
        assert not (d / "kb_vectors.npy").exists()
        assert (d.parent / "kb_index.db").is_file()

    def test_reopen_loads_same_index(self, roots):
        """重开实例（模拟每次检索新建）→ 键与向量一致。"""
        idx = KbVectorIndex(roots, api_key="k")
        idx.index_paper(DOI_A, note_text=_body("A"), title="Title A")
        keys = list(idx._idx_to_key)
        vecs = np.array(idx._vectors)
        reset_index_store(roots)
        idx2 = KbVectorIndex(roots, api_key="k")
        assert list(idx2._idx_to_key) == keys
        assert np.allclose(np.array(idx2._vectors), vecs)
        assert idx2.count_for(DOI_A) == idx.count_for(DOI_A) > 0

    def test_content_change_shadows_then_compacts(self, roots):
        """内容变化 → 旧行成垃圾；压实后垃圾归零且新内容仍在。"""
        idx = KbVectorIndex(roots, api_key="k")
        idx.index_paper(DOI_A, note_text=_body("A"))
        idx.index_paper(DOI_A, note_text=_body("A2"), force=True)
        h = idx._store.health()
        assert h["garbage_rows"] > 0, "刷新应产生垃圾行"
        idx.compact()
        assert idx._store.health()["garbage_rows"] == 0
        reset_index_store(roots)
        idx2 = KbVectorIndex(roots, api_key="k")
        assert idx2.count_for(DOI_A) > 0

    def test_vectors_view_aligned_with_keys(self, roots):
        """`_vectors` 与 `_idx_to_key` 行序严格对齐（错位比"没有向量"更坏）。"""
        idx = KbVectorIndex(roots, api_key="k")
        idx.index_paper(DOI_A, note_text=_body("A"))
        idx.index_paper(DOI_B, note_text=_body("B"))
        assert idx._vectors is not None
        assert len(idx._vectors) == len(idx._idx_to_key) == idx.size


@patch("paperlit.vector.encode_texts", _mock_encode_texts)
class TestFailureHandling:
    def test_embedding_failure_records_dead_letter(self, roots):
        """编码失败：不留幽灵元数据，且登记死信（否则该篇语义检索永久缺失且无人重试）。"""
        idx = KbVectorIndex(roots, api_key="k")
        with patch("paperlit.vector.encode_texts", lambda texts, **kw: [None] * len(texts)):
            idx.index_paper(DOI_A, note_text=_body("A"))
        assert idx.count_for(DOI_A) == 0
        assert idx.size == 0
        dl = idx._store.dead_letters()
        assert len(dl) == 1 and dl[0]["op"] == "vector_index" and DOI_A in dl[0]["doi"]

    def test_success_resolves_dead_letter(self, roots):
        """重试成功后死信结案（不会永远挂在健康面板上）。"""
        idx = KbVectorIndex(roots, api_key="k")
        idx._store.add_dead_letter(DOI_A, "vector_index", "boom", delay_sec=0)
        assert len(idx._store.dead_letters()) == 1
        idx.index_paper(DOI_A, note_text=_body("A"))
        assert idx._store.dead_letters() == []


class TestProcessSingleton:
    """进程级单例：旧路径每次检索/每篇编译都新建实例全量读盘（10 万篇不可接受）。"""

    def test_same_roots_reuse_instance(self, roots):
        a = get_kb_vector_index(roots, api_key="k")
        b = get_kb_vector_index(roots, api_key="k")
        assert a is b

    def test_different_key_isolated(self, roots):
        assert get_kb_vector_index(roots, api_key="k1") is not \
            get_kb_vector_index(roots, api_key="k2")

    def test_reset_gives_fresh_instance(self, roots):
        a = get_kb_vector_index(roots, api_key="k")
        reset_kb_vector_index(roots)
        assert get_kb_vector_index(roots, api_key="k") is not a

    def test_writes_visible_through_singleton(self, roots):
        """写路径走同一实例：编译写入后，检索侧实例立刻看得到（不读旧盘）。"""
        with patch("paperlit.vector.encode_texts", _mock_encode_texts):
            writer = get_kb_vector_index(roots, api_key="k")
            writer.index_paper(DOI_A, note_text=_body("A"))
        reader = get_kb_vector_index(roots, api_key="k")
        assert reader is writer
        assert reader.count_for(DOI_A) > 0
        assert reader.size > 0


class TestRebuildPath:
    def test_drop_papers_from_index_removes_rows(self, roots):
        """移入回收站 → 向量摘除（不调 embedding），SQLite 里也不留行。"""
        from paperkb.vector import drop_papers_from_index

        with patch("paperlit.vector.encode_texts", _mock_encode_texts):
            idx = KbVectorIndex(roots, api_key="k")
            idx.index_paper(DOI_A, note_text=_body("A"))
            idx.index_paper(DOI_B, note_text=_body("B"))
            assert idx.count_for(DOI_A) > 0
        n = drop_papers_from_index(roots, [DOI_A])
        assert n > 0
        reset_index_store(roots)
        idx2 = KbVectorIndex(roots, api_key="k")
        assert idx2.count_for(DOI_A) == 0
        assert idx2.count_for(DOI_B) > 0


class TestIndexHealthApi:
    """索引健康四接口的**逻辑**（HTTP 层只是透传）。"""

    @pytest.fixture()
    def kbsetup(self, roots):
        from paperkb import api

        api.init_kb(roots)
        yield api
        api._store = None          # noqa: SLF001
        api._settings = None       # noqa: SLF001

    def test_status_reports_schema_and_health(self, kbsetup):
        st = kbsetup.kb_index_status()
        assert st["schema_version"] >= 1
        assert set(("segments", "live_passages", "garbage_rows", "papers",
                    "dead_letters", "legacy_files_present")) <= set(st)
        assert kbsetup.kb_index_status()["dead_letters_detail"] == []

    def test_scan_then_compact_idempotent_on_empty(self, kbsetup):
        assert kbsetup.kb_index_scan()["pruned"] == 0
        assert kbsetup.kb_index_compact()["rows_after"] == 0

    def test_retry_no_dead_letters_is_noop(self, kbsetup):
        assert kbsetup.kb_index_retry(limit=5) == {"retried": 0, "resolved": 0,
                                                   "skipped": 0}

    def test_retry_resolves_when_product_present(self, kbsetup, roots):
        """死信 → 产物在磁盘 → 重试成功并结案。"""
        from paperkb.index_store import get_index_store

        ist = get_index_store(roots)
        doi = "10.1234/retry"
        folder = roots.kb_dir / "10.1234_retry"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "_note.md").write_text(
            f"---\ndoi: {doi}\n---\n# T\n\n" + "x" * 400, encoding="utf-8")
        ist.add_dead_letter(doi, "vector_index", "boom", delay_sec=0)

        with patch("paperlit.vector.encode_texts", _mock_encode_texts), \
                patch.dict("os.environ", {"SILICONFLOW_API_KEY": "test-key"}):
            out = kbsetup.kb_index_retry(limit=5)
        assert out["retried"] == 1 and out["resolved"] == 1
        assert ist.dead_letters() == []

    def test_retry_closes_dead_letter_without_dir(self, kbsetup, roots):
        """目录已不在（篇目被移除）→ 无从重试，结案而不是永远挂着。"""
        from paperkb.index_store import get_index_store

        ist = get_index_store(roots)
        ist.add_dead_letter("10.1234/gone", "vector_index", "boom", delay_sec=0)
        out = kbsetup.kb_index_retry(limit=5)
        assert out["skipped"] == 1 and ist.dead_letters() == []

