# -*- coding: utf-8 -*-
"""扩容 P0：`index_store` 数据层（SQLite 元数据 + 段式向量 + 单例 + 死信）单测。

覆盖的是**10 万篇量级的存储契约**，不是"能跑就行"：
- 追加写与索引体积无关（不再整份重写 `.npy` / `meta.json`）；
- 刷新/删除只产生垃圾行，`compact()` 回收且不丢 live 行；
- 段名单调（compact 后不得复用旧名，否则新段会被"删旧段"连带删掉）；
- 崩溃安全：元数据事务提交后才删物理段文件。
"""
from __future__ import annotations

import numpy as np
import pytest

from paperkb.config import Roots
from paperkb.index_store import (SCHEMA_VERSION, IndexStore, get_index_store,
                                 import_legacy, reset_index_store)

DIM = 8


@pytest.fixture()
def roots(tmp_path):
    r = Roots(data_dir=tmp_path / "data", library_dir=tmp_path / "library",
              kb_dir=tmp_path / "kb").ensure()
    yield r
    reset_index_store(r)


def _row(key: str, doi: str, ptype: str = "note", chunk: int = 0) -> dict:
    return {"key": key, "doi": doi, "ptype": ptype, "chunk": chunk, "section": "S",
            "file": doi, "start": chunk * 100, "end": chunk * 100 + 99,
            "hash": "h" + key, "chunk_md5": "m" + key}


def _vecs(n: int, seed: int = 0) -> np.ndarray:
    rng = np.random.RandomState(seed)
    return rng.randn(n, DIM).astype(np.float32)


class TestSingleton:
    def test_same_roots_share_instance(self, roots):
        assert get_index_store(roots) is get_index_store(roots)

    def test_reset_drops_instance(self, roots):
        a = get_index_store(roots)
        reset_index_store(roots)
        assert get_index_store(roots) is not a

    def test_two_roots_isolated(self, tmp_path):
        r1 = Roots(data_dir=tmp_path / "a", library_dir=tmp_path / "la",
                   kb_dir=tmp_path / "ka").ensure()
        r2 = Roots(data_dir=tmp_path / "b", library_dir=tmp_path / "lb",
                   kb_dir=tmp_path / "kb2").ensure()
        s1, s2 = get_index_store(r1), get_index_store(r2)
        assert s1 is not s2 and s1.db_path != s2.db_path
        reset_index_store(r1)
        reset_index_store(r2)


class TestSchema:
    def test_schema_version_stamped(self, roots):
        st = get_index_store(roots)
        assert int(st.get_meta("schema_version")) == SCHEMA_VERSION

    def test_newer_schema_refused(self, roots, tmp_path):
        """数据由更新版本写过 → 必须拒绝读取（不许猜测式部分读取）。"""
        import sqlite3

        st = get_index_store(roots)
        con = sqlite3.connect(str(st.db_path))
        con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('schema_version', '999')")
        con.commit()
        con.close()
        reset_index_store(roots)
        with pytest.raises(RuntimeError, match="新于本代码"):
            IndexStore(roots)


class TestAppendAndLoad:
    def test_append_then_load_aligned(self, roots):
        st = get_index_store(roots)
        assert st.append([_row("a__note", "10.1/a"), _row("a__note#1", "10.1/a", chunk=1)],
                         _vecs(2)) == 2
        keys, meta, vec = st.load()
        assert keys == ["a__note", "a__note#1"]
        assert vec is not None and vec.shape == (2, DIM)
        assert meta["a__note"]["doi"] == "10.1/a"

    def test_second_paper_appends_to_same_segment(self, roots):
        """追加写**不**重写既有段：段数保持 1，行数累加。"""
        st = get_index_store(roots)
        st.append([_row("a__note", "10.1/a")], _vecs(1))
        st.append([_row("b__note", "10.1/b")], _vecs(1, seed=1))
        h = st.health()
        assert h["segments"] == 1 and h["segment_rows"] == 2 and h["live_passages"] == 2

    def test_refresh_shadows_old_row(self, roots):
        """同 key 重写 → 指向新行，旧行变垃圾（不重写全量）。"""
        st = get_index_store(roots)
        st.append([_row("a__note", "10.1/a")], _vecs(1))
        st.append([_row("a__note", "10.1/a", chunk=5)], _vecs(1, seed=2))
        h = st.health()
        assert h["live_passages"] == 1 and h["garbage_rows"] == 1
        keys, meta, vec = st.load()
        assert keys == ["a__note"] and int(meta["a__note"]["chunk"]) == 5

    def test_delete_then_load(self, roots):
        st = get_index_store(roots)
        st.append([_row("a__note", "10.1/a"), _row("b__note", "10.1/b")], _vecs(2))
        assert st.delete(["b__note"]) == 1
        keys, _, vec = st.load()
        assert keys == ["a__note"] and vec.shape == (1, DIM)

    def test_dim_change_seals_previous_segment(self, roots):
        """换维度（换模型）→ 封旧段另起新段，绝不把不同向量空间混进一张矩阵。"""
        st = get_index_store(roots)
        st.append([_row("a__note", "10.1/a")], _vecs(1))
        st.append([_row("b__note", "10.1/b")],
                  np.random.randn(1, DIM * 2).astype(np.float32))
        assert st.health()["segments"] == 2


class TestCompact:
    def test_compact_reclaims_and_keeps_live(self, roots):
        st = get_index_store(roots)
        st.append([_row("a__note", "10.1/a"), _row("b__note", "10.1/b")], _vecs(2))
        st.append([_row("a__note", "10.1/a")], _vecs(1, seed=3))
        st.delete(["b__note"])
        before = st.health()
        assert before["garbage_rows"] == 2
        stats = st.compact()
        assert stats["reclaimed_rows"] == 2
        h = st.health()
        assert h["garbage_rows"] == 0 and h["live_passages"] == 1 and h["segments"] == 1
        keys, _, vec = st.load()
        assert keys == ["a__note"] and vec.shape == (1, DIM)

    def test_segment_names_never_reused(self, roots):
        """回归钉：compact 曾按 COUNT(*) 取名 → 新段与刚删的旧段同名 → 新段被删、索引全空。"""
        st = get_index_store(roots)
        st.append([_row("a__note", "10.1/a")], _vecs(1))
        st.compact()
        st.append([_row("b__note", "10.1/b")], _vecs(1, seed=4))
        st.compact()
        keys, _, vec = st.load()
        assert set(keys) == {"a__note", "b__note"} and vec is not None \
            and vec.shape == (2, DIM), "compact 后索引不得为空（段名曾经被复用而连带删除）"

    def test_compact_then_reopen_persists(self, roots):
        st = get_index_store(roots)
        st.append([_row("a__note", "10.1/a")], _vecs(1))
        st.compact()
        reset_index_store(roots)
        keys, _, vec = get_index_store(roots).load()
        assert keys == ["a__note"] and vec is not None


class TestPassageMetaOnly:
    def test_save_meta_keeps_vector_pointer(self, roots):
        """只刷元数据列（内容 hash 未变）时，seg/row 指针不得被清掉。"""
        st = get_index_store(roots)
        st.append([_row("a__note", "10.1/a")], _vecs(1))
        m = _row("a__note", "10.1/a", chunk=7)
        assert st.save_passage_meta([m]) == 1
        keys, meta, vec = st.load()
        assert keys == ["a__note"] and vec is not None
        assert int(meta["a__note"]["chunk"]) == 7

    def test_save_meta_ignores_orphan(self, roots):
        st = get_index_store(roots)
        assert st.save_passage_meta([_row("ghost__note", "10.1/x")]) == 0
        assert st.load()[0] == []


class TestPapersState:
    def test_papers_counted(self, roots):
        st = get_index_store(roots)
        st.append([_row("a__note", "10.1/a"), _row("a__note#1", "10.1/a", chunk=1),
                   _row("b__note", "10.1/b")], _vecs(3))
        assert st.health()["papers"] == 2
        st.delete(["b__note"])
        st.refresh_papers_state()
        assert st.health()["papers"] == 1


class TestDeadLetter:
    def test_backoff_accumulates_tries(self, roots):
        st = get_index_store(roots)
        i1 = st.add_dead_letter("10.1/a", "vector_index", "boom", delay_sec=0)
        i2 = st.add_dead_letter("10.1/a", "vector_index", "boom2", delay_sec=0)
        assert i1 == i2                       # 同 (doi,op) 不重复堆积
        rows = st.dead_letters()
        assert len(rows) == 1 and rows[0]["tries"] == 2

    def test_due_and_resolve(self, roots):
        st = get_index_store(roots)
        st.add_dead_letter("10.1/a", "vector_index", "boom", delay_sec=0)
        st.add_dead_letter("10.1/b", "vector_index", "later", delay_sec=9999)
        assert [r["doi"] for r in st.due_dead_letters()] == ["10.1/a"]
        dl_id = st.dead_letters(limit=1)[0]["id"]
        st.resolve_dead_letter(dl_id)
        assert len(st.dead_letters()) == 1    # 只剩未到期的 b

    def test_resolve_for_clears_paper_op(self, roots):
        st = get_index_store(roots)
        st.add_dead_letter("10.1/a", "vector_index", "boom", delay_sec=0)
        st.add_dead_letter("10.1/a", "l3", "other", delay_sec=0)
        assert st.resolve_for("10.1/a", "vector_index") == 1
        assert [r["op"] for r in st.dead_letters()] == ["l3"]


class TestLegacyImport:
    def test_import_legacy_roundtrip(self, roots):
        """旧格式（整份 JSON + npy）可一次性导入（不是读时兼容）。"""
        import json

        st = get_index_store(roots)
        d = st.index_dir
        order = ["a__note", "b__note"]
        np.save(d / "kb_vectors.npy", _vecs(2, seed=9))
        (d / "kb_index_meta.json").write_text(json.dumps({
            "model": "BAAI/bge-m3", "idx_to_key": order,
            "key_meta": {k: _row(k, "10.1/" + k[:1]) for k in order},
        }), encoding="utf-8")
        reset_index_store(roots)
        st2 = get_index_store(roots)
        assert st2.legacy_files_present() is True
        out = import_legacy(st2)
        assert out == {"imported": 2}
        keys, _, vec = st2.load()
        assert keys == order and vec.shape == (2, DIM)
        assert st2.get_meta("model") == "BAAI/bge-m3"
