# -*- coding: utf-8 -*-
"""KB 向量索引的**数据层**：SQLite 元数据 + 段式向量文件 + 进程级单例。

为什么需要它（2026-09-21 扩容 P0）：

- 旧存储 = 整份 `kb_index_meta.json` + 整份 `kb_vectors.npy`。每次编译**一篇**都要
  全量重写两份文件 ⇒ 写放大 ≈ 2×索引体积/篇，全量重建 O(N²)。按 10 万篇 ≈104 万块估：
  JSON ≈250MB、向量 ≈4.3GB —— 每编译一篇重写 4.5GB，不可接受。
- 现在：元数据按行 upsert 进 SQLite（`data/vector/kb_index.db`）；向量**只追加**到段文件
  （`kb_vectors/<seg>.f32`，裸 float32 行主序，`open(..., "ab")` 即 O(1)）；空间回收由
  `compact()` 显式触发（live 行重写为新段 → 单事务原子切换 → 删旧段 + 删旧文件）。
- 进程级单例：旧路径每次检索（`api.py`）与每篇编译（`compile.py`）都 `KbVectorIndex(roots)`
  新建实例并全量读盘；单例后同一 roots 只加载一次。

归属：`data/vector/` 是**派生索引**（可由 knowledge_base 编译产物重建），不是权威数据，
因此不进 `data/manifest.json` 的五库版本契约，自带 `meta.schema_version`。

`sqlite3.connect` 只允许出现在数据层（`backend/tests/test_version_contract.py` 守卫）——
本模块即数据层，已登记进 `CONNECT_ALLOW`。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

import numpy as np

from .config import Roots

logger = logging.getLogger(__name__)

DB_NAME = "kb_index.db"
SCHEMA_VERSION = 2
SEG_DTYPE = np.float32

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
    k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS segments(
    name TEXT PRIMARY KEY, rows INTEGER NOT NULL, dim INTEGER NOT NULL,
    created REAL NOT NULL, sealed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS passages(
    key TEXT PRIMARY KEY, doi TEXT NOT NULL DEFAULT '', ptype TEXT NOT NULL DEFAULT '',
    chunk INTEGER NOT NULL DEFAULT 0, section TEXT NOT NULL DEFAULT '',
    file TEXT NOT NULL DEFAULT '', start INTEGER NOT NULL DEFAULT -1,
    end INTEGER NOT NULL DEFAULT -1, hash TEXT NOT NULL DEFAULT '',
    chunk_md5 TEXT NOT NULL DEFAULT '', seg TEXT NOT NULL DEFAULT '',
    row INTEGER NOT NULL DEFAULT -1);
CREATE INDEX IF NOT EXISTS ix_passages_doi ON passages(doi);
CREATE INDEX IF NOT EXISTS ix_passages_seg ON passages(seg, row);
CREATE TABLE IF NOT EXISTS papers_state(
    doi TEXT PRIMARY KEY, passages INTEGER NOT NULL DEFAULT 0,
    updated REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS dead_letter(
    id INTEGER PRIMARY KEY AUTOINCREMENT, doi TEXT NOT NULL DEFAULT '',
    op TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
    tries INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0,
    created REAL NOT NULL DEFAULT 0, resolved INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_dead_due ON dead_letter(resolved, next_try);
"""

_PASSAGE_COLS = ("key", "doi", "ptype", "chunk", "section", "file", "start", "end",
                 "hash", "chunk_md5", "seg", "row")


class IndexStore:
    """`data/vector/` 的读写入口（元数据走 SQLite，向量走段文件）。

    线程内单写者：每个方法各自开一次短连接（WAL），无长事务、无内存等价物驻留。
    """

    def __init__(self, roots: Roots):
        self.roots = roots
        self.index_dir = Path(roots.vector_dir) / "kb_vectors"
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = Path(roots.vector_dir) / DB_NAME
        self._ensure_schema()

    # ---------------------------------------------------------------- 连接 / 建表
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            cur = conn.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
            if cur is None:
                conn.execute("INSERT INTO meta(k, v) VALUES('schema_version', ?)",
                             (str(SCHEMA_VERSION),))
            elif int(cur["v"]) > SCHEMA_VERSION:
                raise RuntimeError(
                    f"kb_index.db schema 版本 {cur['v']} 新于本代码支持的 {SCHEMA_VERSION}"
                    "（数据由更新版本写入，拒绝部分读取）")
            conn.commit()

    # ---------------------------------------------------------------- meta 键值
    def get_meta(self, key: str, default: str = "") -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return row["v"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", (key, str(value)))
            conn.commit()

    # ---------------------------------------------------------------- 段管理
    def _seg_path(self, name: str) -> Path:
        return self.index_dir / f"{name}.f32"

    def _active_segment(self, conn: sqlite3.Connection) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM segments WHERE sealed=0 ORDER BY created DESC LIMIT 1").fetchone()

    def _next_seg_name(self, conn: sqlite3.Connection) -> str:
        """取下一个段名（**单调计数器**，存在 meta 里）。

        不能用 `COUNT(*)`：compact() 会先清空 segments 再建新段，按行数取名会与刚删掉的
        旧段重名，随后"删旧段文件"就把新段删了（实测踩过：compact 后索引全空）。
        """
        row = conn.execute("SELECT v FROM meta WHERE k='next_seg'").fetchone()
        n = int(row["v"]) if row else 1
        conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('next_seg', ?)", (str(n + 1),))
        return f"seg-{n:06d}"

    def _ensure_active(self, conn: sqlite3.Connection, dim: int) -> sqlite3.Row:
        row = self._active_segment(conn)
        if row is not None:
            if int(row["dim"]) != dim:
                # 维度变了（换 embedding 模型）：封旧段，另起新段，绝不混用向量空间
                conn.execute("UPDATE segments SET sealed=1 WHERE name=?", (row["name"],))
            else:
                return row
        name = self._next_seg_name(conn)
        conn.execute("INSERT INTO segments(name, rows, dim, created, sealed) VALUES(?,?,?,?,0)",
                     (name, 0, dim, time.time()))
        return conn.execute("SELECT * FROM segments WHERE name=?", (name,)).fetchone()

    # ---------------------------------------------------------------- 写
    def append(self, rows: list[dict], vectors: np.ndarray) -> int:
        """追加一批 passage（元数据行 + 向量行）。返回写入向量数。

        `rows[i]` 与 `vectors[i]` 一一对应；已存在的 key 会被重新指向新行
        （旧行成为垃圾，由 `compact()` 回收）——追加语义保证写放大与索引体积无关。
        """
        if not rows:
            return 0
        vecs = np.asarray(vectors, dtype=SEG_DTYPE)
        if vecs.ndim != 2 or vecs.shape[0] != len(rows):
            raise ValueError(f"rows/vectors 数量不符: {len(rows)} vs {vecs.shape}")
        dim = int(vecs.shape[1])
        seen: set[str] = set()
        with self._connect() as conn:
            seg = self._ensure_active(conn, dim)
            base = int(seg["rows"])
            path = self._seg_path(seg["name"])
            with open(path, "ab") as fh:     # O(1) 追加（段文件是裸 float32 行主序）
                fh.write(vecs.tobytes(order="C"))
            conn.execute("UPDATE segments SET rows=rows+? WHERE name=?",
                         (len(rows), seg["name"]))
            for i, meta in enumerate(rows):
                conn.execute(
                    """INSERT OR REPLACE INTO passages(
                           key,doi,ptype,chunk,section,file,start,end,hash,chunk_md5,seg,row)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (meta.get("key", ""), meta.get("doi", ""), meta.get("ptype", ""),
                     int(meta.get("chunk") or 0), meta.get("section", ""),
                     meta.get("file", ""), int(meta.get("start", -1) or -1),
                     int(meta.get("end", -1) or -1), meta.get("hash", ""),
                     meta.get("chunk_md5", ""), seg["name"], base + i))
                seen.add(meta.get("doi", ""))
            now = time.time()
            for doi in seen:
                if not doi:
                    continue
                conn.execute(
                    """INSERT INTO papers_state(doi, passages, updated) VALUES(?,?,?)
                       ON CONFLICT(doi) DO UPDATE SET
                           passages=(SELECT COUNT(*) FROM passages WHERE doi=excluded.doi),
                           updated=excluded.updated""", (doi, 0, now))
            conn.commit()
        return len(rows)

    def keys_for(self, doi: str, ptype: str) -> list[str]:
        """某篇某产物已有的全部 key（走 `ix_passages_doi`）。

        用于"清理该篇该产物的陈旧块"：旧实现每次编译都**扫描整个内存 `_key_meta`**
        找同 DOI 的键 ⇒ O(N)/篇 ⇒ 全量重建 O(N²)（10 万篇实测尾段每篇慢 3.5 倍）。
        """
        with self._connect() as conn:
            rows = conn.execute("SELECT key FROM passages WHERE doi=? AND ptype=?",
                                (doi, ptype)).fetchall()
        return [r["key"] for r in rows]

    def save_passage_meta(self, rows: list[dict]) -> int:
        """只更新 passage 的**元数据列**（不动 seg/row）：内容 hash 未变、仅偏移/小节刷新的块。

        若 key 不存在（无向量的孤儿）则忽略——元数据必须指向真实向量行，否则
        命中块会出现"有指针没数据"。
        """
        if not rows:
            return 0
        n = 0
        with self._connect() as conn:
            for m in rows:
                cur = conn.execute(
                    """UPDATE passages SET doi=?, ptype=?, chunk=?, section=?, file=?,
                           start=?, end=?, hash=?, chunk_md5=? WHERE key=?""",
                    (m.get("doi", ""), m.get("ptype", ""), int(m.get("chunk") or 0),
                     m.get("section", ""), m.get("file", ""), int(m.get("start", -1) or -1),
                     int(m.get("end", -1) or -1), m.get("hash", ""), m.get("chunk_md5", ""),
                     m.get("key", "")))
                n += cur.rowcount or 0
            conn.commit()
        return n

    def delete(self, keys: list[str]) -> int:
        """按 key 删除元数据行（向量行成为垃圾，由 compact() 回收）。"""
        keys = [k for k in (keys or []) if k]
        if not keys:
            return 0
        with self._connect() as conn:
            n = 0
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                marks = ",".join("?" * len(chunk))
                cur = conn.execute(f"DELETE FROM passages WHERE key IN ({marks})", chunk)
                n += cur.rowcount or 0
            conn.commit()
        return n

    def refresh_papers_state(self, dois: list[str] | None = None) -> None:
        """重算 papers_state.passages（doi 为空则全量）。"""
        with self._connect() as conn:
            if dois:
                for doi in dois:
                    if not doi:
                        continue
                    conn.execute(
                        """INSERT INTO papers_state(doi, passages, updated) VALUES(?,?,?)
                           ON CONFLICT(doi) DO UPDATE SET
                               passages=(SELECT COUNT(*) FROM passages WHERE doi=?),
                               updated=excluded.updated""",
                        (doi, 0, time.time(), doi))
            else:
                conn.execute("DELETE FROM papers_state")
                conn.execute(
                    """INSERT INTO papers_state(doi, passages, updated)
                       SELECT doi, COUNT(*), ? FROM passages WHERE doi<>'' GROUP BY doi""",
                    (time.time(),))
            conn.commit()

    # ---------------------------------------------------------------- 读
    def load(self) -> tuple[list[str], dict[str, dict], np.ndarray | None]:
        """加载全量索引：`(idx_to_key, key_meta, vectors)`，三者行序严格对齐。

        行序 = 段创建顺序 × 段内 row；仅保留 `passages` 仍指向的行（被刷新/删除的旧行
        只占文件空间、不进结果）。
        """
        with self._connect() as conn:
            segs = conn.execute("SELECT * FROM segments ORDER BY created, name").fetchall()
            live: dict[str, dict[int, str]] = {}      # seg → {row: key}
            for r in conn.execute("SELECT key, seg, row FROM passages WHERE seg<>''"):
                live.setdefault(r["seg"], {})[int(r["row"])] = r["key"]
            keys: list[str] = []
            parts: list[np.ndarray] = []
            for s in segs:
                name = s["name"]
                want = live.get(name) or {}
                if not want:
                    continue
                path = self._seg_path(name)
                if not path.is_file():
                    logger.warning("段文件缺失（跳过）: %s", path)
                    continue
                rows = int(s["rows"])
                dim = int(s["dim"])
                arr = np.fromfile(path, dtype=SEG_DTYPE)
                if rows and arr.size < rows * dim:
                    logger.warning("段文件截断（跳过）: %s", path)
                    continue
                arr = arr[:rows * dim].reshape(rows, dim)
                order = sorted(want)
                parts.append(arr[order])
                keys.extend(want[r] for r in order)
            meta: dict[str, dict] = {}
            for r in conn.execute(f"SELECT {','.join(_PASSAGE_COLS)} FROM passages"):
                meta[r["key"]] = {c: r[c] for c in _PASSAGE_COLS if c != "key"}
        vectors = np.concatenate(parts) if parts else None
        if vectors is not None and len(vectors) != len(keys):
            logger.warning("索引行数不一致（keys=%d vec=%d），丢弃以免错位", len(keys),
                           len(vectors))
            return [], {}, None
        return keys, meta, vectors

    def size(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) AS c FROM passages").fetchone()["c"])

    # ---------------------------------------------------------------- 压实 / 健康
    def compact(self) -> dict:
        """把 live 行重写为**单一段**，删旧段与旧文件（原子切换）。

        触发时机：垃圾行占比过高（刷新/删除累积）或显式重建。单事务内完成
        segments/passages 指针切换，中途崩不会出现"半切换"（SQLite 原子性保证）。
        """
        keys, meta, vectors = self.load()
        with self._connect() as conn:
            old = conn.execute("SELECT SUM(rows) AS r, COUNT(*) AS c FROM segments").fetchone()
            old_rows, old_segs = int(old["r"] or 0), int(old["c"])
            names = [r["name"] for r in conn.execute("SELECT name FROM segments")]
            conn.execute("DELETE FROM segments")
            conn.execute("DELETE FROM passages")
            if keys:
                dim = int(vectors.shape[1])
                name = self._next_seg_name(conn)
                conn.execute(
                    "INSERT INTO segments(name, rows, dim, created, sealed) VALUES(?,?,?,?,1)",
                    (name, len(keys), dim, time.time()))
                path = self._seg_path(name)
                arr = np.ascontiguousarray(vectors, dtype=SEG_DTYPE)
                with open(path, "wb") as fh:
                    fh.write(arr.tobytes(order="C"))
                for i, k in enumerate(keys):
                    m = meta[k]
                    conn.execute(
                        """INSERT OR REPLACE INTO passages(
                               key,doi,ptype,chunk,section,file,start,end,hash,chunk_md5,seg,row)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (k, m.get("doi", ""), m.get("ptype", ""), int(m.get("chunk") or 0),
                         m.get("section", ""), m.get("file", ""), int(m.get("start", -1)),
                         int(m.get("end", -1)), m.get("hash", ""), m.get("chunk_md5", ""),
                         name, i))
            conn.commit()
        for nm in names:                     # 元数据事务已提交后才删物理文件
            p = self._seg_path(nm)
            if p.is_file():
                try:
                    p.unlink()
                except OSError as e:  # noqa: PERF203 - 删不掉只损失磁盘空间
                    logger.warning("旧段文件删除失败: %s (%s)", p, e)
        stats = {"segments_before": old_segs, "rows_before": old_rows,
                 "rows_after": len(keys), "reclaimed_rows": max(0, old_rows - len(keys))}
        logger.info("向量索引压实: %s", stats)
        return stats

    def health(self) -> dict:
        """索引健康快照（只读；供 UI「索引健康」卡片与巡检接口）。"""
        with self._connect() as conn:
            segs = conn.execute(
                "SELECT COUNT(*) AS c, COALESCE(SUM(rows),0) AS r FROM segments").fetchone()
            live = conn.execute("SELECT COUNT(*) AS c FROM passages").fetchone()["c"]
            papers = conn.execute("SELECT COUNT(*) AS c FROM papers_state").fetchone()["c"]
            dead = conn.execute(
                "SELECT COUNT(*) AS c FROM dead_letter WHERE resolved=0").fetchone()["c"]
            schema = conn.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
        missing = [s for s in self._segment_names()
                   if not self._seg_path(s).is_file()]
        return {
            "schema_version": int(schema["v"]) if schema else 0,
            "db_path": str(self.db_path),
            "segments": int(segs["c"]),
            "segment_rows": int(segs["r"]),
            "live_passages": int(live),
            "garbage_rows": max(0, int(segs["r"]) - int(live)),
            "papers": int(papers),
            "dead_letters": int(dead),
            "missing_segments": missing,
            "bytes": self.db_bytes(),
        }

    def _segment_names(self) -> list[str]:
        with self._connect() as conn:
            return [r["name"] for r in conn.execute("SELECT name FROM segments")]

    def db_bytes(self) -> int:
        total = self.db_path.stat().st_size if self.db_path.is_file() else 0
        for p in self.index_dir.glob("*.f32"):
            try:
                total += p.stat().st_size
            except OSError:
                pass
        return total

    # ---------------------------------------------------------------- 死信（编译/索引失败重试）
    def add_dead_letter(self, doi: str, op: str, error: str, delay_sec: float = 300.0) -> int:
        """登记一条失败作业（带退避 next_try）。

        同一 (doi, op) 未解决的旧条目会累加 tries 并顺延重试时间，不重复堆积。
        """
        now = time.time()
        with self._connect() as conn:
            old = conn.execute(
                "SELECT * FROM dead_letter WHERE doi=? AND op=? AND resolved=0",
                (doi, op)).fetchone()
            if old:
                tries = int(old["tries"]) + 1
                backoff = min(delay_sec * (2 ** (tries - 1)), 24 * 3600)
                conn.execute(
                    "UPDATE dead_letter SET tries=?, error=?, next_try=? WHERE id=?",
                    (tries, error, now + backoff, old["id"]))
                conn.commit()
                return int(old["id"])
            cur = conn.execute(
                """INSERT INTO dead_letter(doi, op, error, tries, next_try, created, resolved)
                   VALUES(?,?,?,1,?,?,0)""", (doi, op, error, now + delay_sec, now))
            conn.commit()
            return int(cur.lastrowid)

    def due_dead_letters(self, now: float | None = None, limit: int = 50) -> list[dict]:
        now = time.time() if now is None else now
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM dead_letter WHERE resolved=0 AND next_try<=?
                   ORDER BY next_try LIMIT ?""", (now, limit)).fetchall()
        return [dict(r) for r in rows]

    def dead_letters(self, limit: int = 200) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM dead_letter WHERE resolved=0 ORDER BY created DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    def resolve_dead_letter(self, dl_id: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE dead_letter SET resolved=1 WHERE id=?", (dl_id,))
            conn.commit()

    def resolve_for(self, doi: str, op: str) -> int:
        """某篇某操作的死信全部结案（成功重试后清账）。"""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE dead_letter SET resolved=1 WHERE doi=? AND op=? AND resolved=0",
                (doi, op))
            conn.commit()
            return cur.rowcount or 0

    # ---------------------------------------------------------------- 旧格式探测（不做迁移）
    def legacy_files_present(self) -> bool:
        """旧格式（整份 JSON + 整份 npy）是否还在（本代码不读它，仅用于提示重建）。"""
        return ((self.index_dir / "kb_index_meta.json").is_file()
                or (self.index_dir / "kb_vectors.npy").is_file())


# ---------------------------------------------------------------- 进程级单例
_stores: dict[str, IndexStore] = {}


def get_index_store(roots: Roots) -> IndexStore:
    """按 vector 根路径取单例（不同 tmp 目录的测试互不串档）。"""
    key = str(Path(roots.vector_dir).resolve())
    if key not in _stores:
        _stores[key] = IndexStore(roots)
    return _stores[key]


def reset_index_store(roots: Roots | None = None) -> None:
    """丢弃单例（测试/换根路径用；不关连接——本实现每次操作短连接）。"""
    if roots is None:
        _stores.clear()
        return
    _stores.pop(str(Path(roots.vector_dir).resolve()), None)


def import_legacy(store: IndexStore) -> dict | None:
    """把旧格式（kb_index_meta.json + kb_vectors.npy）一次性导入新存储。

    **不是读时兼容**：仅在显式重建/迁移时调用，导入后旧文件可删。
    """
    meta_path = store.index_dir / "kb_index_meta.json"
    vec_path = store.index_dir / "kb_vectors.npy"
    if not meta_path.is_file() or not vec_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        order = list(meta.get("idx_to_key") or [])
        kmeta = meta.get("key_meta") or {}
        vectors = np.load(vec_path)
    except Exception as e:  # noqa: BLE001
        logger.warning("旧索引导入失败: %s", e)
        return None
    if not order or vectors is None or len(vectors) != len(order):
        return None
    for k in order:
        m = kmeta.get(k) or {}
        m.setdefault("doi", "")
        m.setdefault("ptype", "")
        m["key"] = k
    rows = [kmeta[k] for k in order]
    store.append(rows, vectors.astype(SEG_DTYPE))
    store.set_meta("model", str(meta.get("model") or ""))
    store.refresh_papers_state()
    store.compact()
    logger.info("旧向量索引已导入 SQLite + 段文件: %d 行", len(order))
    return {"imported": len(order)}
