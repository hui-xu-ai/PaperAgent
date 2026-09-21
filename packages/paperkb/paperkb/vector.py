# -*- coding: utf-8 -*-
"""知识库编译结果向量索引（**分块级**，含内容 hash 增量）。

索引对象：L1 _note.md / L2 _wiki.md / L3 _relations.md（分块）+ 标题 + 概念定义。
复用 paperlit 的 SiliconFlow bge-m3 embedding 管线。
存储：data/vector/kb_vectors/（numpy 持久化 + JSON 元数据）。

2026-09-21 审计重写（旧实现缺陷 → 现行为）：
- 旧：整文件 1 向量 + `text[:2000/3000]` 硬截断（实测 4/9 篇被切，丢 359~1014 字符，
  丢的正是「方法论连接/研究趋势/相关文献」尾部）→ 现：按 markdown 标题分块
  （`textseg.split_chunks`，段落→句子边界，偏移可逐字节切回）。
- 旧：`force or key not in _key_to_idx` —— 重编译后**向量永不刷新**（索引与磁盘脱节）
  → 现：每个 passage 记 `hash`（嵌入文本 md5），内容变才重嵌，同 ptype 旧键自动清理。
- 旧：`shared_concepts` 直接返回**全部**查询概念（谎报共有）→ 现：返回真实交集。
- 旧：FAISS 模式下只往 faiss 加、不写 `self._vectors` ⇒ 向量**不落盘**（重启即丢）
  → 现：`self._vectors` 是唯一真值，faiss 索引由它重建。

键格式：首块沿用 `{doi}__{ptype}`（兼容既有索引与调用方），附加块 `{doi}__{ptype}#{n}`。
每个键在 `_key_meta` 里带 `{doi,ptype,chunk,section,file,start,end,hash,chunk_md5}`——
`file/start/end` 让检索能"命中哪块就注入哪块"（`search(with_snippet=True)`）。

检索策略（混合检索）：
1. 概念倒排过滤（biblio.db concepts 表，零成本）
2. 向量相似度排序（在候选集内）
3. 引用关系加权（citations 表）
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import numpy as np

from .config import Roots
from .index_store import get_index_store
from .textseg import boundary_trim, embed_prefix, split_chunks, strip_frontmatter

if TYPE_CHECKING:  # 仅类型标注；运行期在 _query_cache 里惰性导入（避免环）
    from .db import QueryVecCache

logger = logging.getLogger(__name__)

EMBEDDING_DIM = 1024  # bge-m3 维度

# 分块参数：900 字/块（≈450 token，远低于 bge-m3 8192 上限）、120 字同节重叠。
# 实测 9 篇 25 个产物 → 68 块（约 7.5 块/篇），非标题内容零丢失。
CHUNK_CHARS = 900
CHUNK_OVERLAP = 120

# 注入片段上限（字符）：小节扩展后的目标长度（≈ 半页；配合 8k token 预算）
SNIPPET_CHARS = 1200

# 产物正文短于该长度不索引（标题/概念另有入口）
MIN_PASSAGE_CHARS = 50

_PTYPE_FILE = {"note": "_note.md", "wiki": "_wiki.md", "relations": "_relations.md"}
_PTYPE_LABEL = {"note": "L1笔记", "wiki": "L2深度", "relations": "L3关系",
                "title": "标题", "concepts": "概念"}

try:
    import faiss
    HAS_FAISS = True
except ImportError:
    HAS_FAISS = False


def _md5(text: str) -> str:
    return hashlib.md5((text or "").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 查询向量缓存
# 缓存本体在**数据层**（`db.QueryVecCache`，独立 SQLite `data/vector/query_cache.db`）——
# `sqlite3.connect` 只允许出现在数据层（test_version_contract 守卫），业务模块走统一接口。
_caches: dict[str, "QueryVecCache"] = {}


def _query_cache_enabled() -> bool:
    try:
        from .api import _settings

        return bool(getattr(_settings, "query_vec_cache", True))
    except Exception:  # noqa: BLE001
        return True


def _query_cache(roots: Roots) -> "QueryVecCache":
    """按 vector 根路径缓存实例（测试用不同 tmp 目录时互不串档）。"""
    from .db import QueryVecCache

    key = str(roots.vector_dir)
    if key not in _caches:
        _caches[key] = QueryVecCache(roots)
    return _caches[key]


def _body_of(text: str) -> str:
    """索引与回读**共用**的正文口径（去 frontmatter + 去首尾空白）。

    必须共用一个函数：块偏移 `start/end` 是在这个串上算的，回读时若少一次
    `.strip()`（frontmatter 后常跟空行），偏移整体错位 1 字符 ⇒ 每个块的 md5
    都对不上，命中块永远返回空（实测踩过）。
    """
    return strip_frontmatter(text or "").strip()


class VectorIndex(Protocol):
    def add_document(self, doi: str, text: str) -> None: ...
    def search(self, query: str, top_k: int = 10) -> list[dict]: ...
    def rebuild(self, roots: Roots) -> None: ...


class NoopVectorIndex:
    """初期空实现：记录日志不动作（FTS5 为主检索）。"""

    def __init__(self, roots: Roots):
        self.roots = roots

    def add_document(self, doi: str, text: str) -> None:
        logger.debug("[vector:noop] add_document %s (%d chars)", doi, len(text))

    def search(self, query: str, top_k: int = 10) -> list[dict]:
        logger.debug("[vector:noop] search %r top_k=%d", query, top_k)
        return []

    def rebuild(self, roots: Roots) -> None:
        logger.info("[vector:noop] rebuild 无动作（FTS5 为主检索）")


class KbVectorIndex:
    """知识库编译结果向量索引（分块级）。

    每篇文献的 passage：
    - `{doi}__title`：标题（高信噪比，快速定位领域）
    - `{doi}__note[#n]`：L1 _note.md 分块
    - `{doi}__wiki[#n]`：L2 _wiki.md 分块
    - `{doi}__relations[#n]`：L3 _relations.md 分块
    - `{doi}__concepts`：概念定义拼接
    """

    def __init__(self, roots: Roots, api_key: str = "",
                 model: str = "BAAI/bge-m3"):
        self.roots = roots
        self.api_key = api_key
        self.model = model
        self.dim = EMBEDDING_DIM

        self.index_dir = roots.vector_dir / "kb_vectors"
        self.index_dir.mkdir(parents=True, exist_ok=True)
        # 数据层：SQLite 元数据 + 段式向量文件，**进程级单例**（旧路径每次检索/每篇编译
        # 都新建实例并全量读盘 → 10 万篇不可接受）
        self._store = get_index_store(roots)

        self._key_to_idx: dict[str, int] = {}
        self._idx_to_key: list[str] = []
        self._key_meta: dict[str, dict] = {}
        self._index = None
        self._faiss_dirty = False       # 行号重排/就地刷新后置脏，检索前整表重建
        # 向量用**可增长缓冲**（容量倍增），不是每次 vstack —— 后者每追加一批都复制整个
        # 数组 ⇒ 全量重建 O(N²)（10 万篇 ≈104 万块，实测不可接受）。`_vectors` 是等长视图。
        self._vec_buf: np.ndarray | None = None
        self._vec_len = 0

        self._load_index()
        if self.size == 0 and self._store.legacy_files_present():
            logger.warning("检测到旧格式向量索引（kb_index_meta.json/kb_vectors.npy），"
                           "本代码不再读取；需重建（rebuild_kb_vector_index）或先跑 "
                           "paperkb.index_store.import_legacy 导入")
        self._validate()
        self._rebuild_faiss()

    @property
    def _vectors(self) -> np.ndarray | None:
        """索引向量的等长视图（行序与 `_idx_to_key` 一致；无向量时为 None）。

        是视图不是副本：`_vec_buf` 容量通常大于 `_vec_len`，这里只切出有效行。
        写入请走 `_append_vectors` / `_remove_keys`，不要往视图里塞行。
        """
        if self._vec_buf is None or self._vec_len == 0:
            return None
        return self._vec_buf[:self._vec_len]

    @_vectors.setter
    def _vectors(self, arr: np.ndarray | None) -> None:
        """整体替换向量（加载/清空用）。"""
        if arr is None:
            self._vec_buf = None
            self._vec_len = 0
            return
        if arr.dtype == np.float32 and arr.flags["C_CONTIGUOUS"]:
            self._vec_buf = arr          # 已经是连续 float32：直接用，避免加载时再复制一遍
        else:
            self._vec_buf = np.ascontiguousarray(arr, dtype=np.float32)
        self._vec_len = len(self._vec_buf)

    def _append_vectors(self, vecs: list[np.ndarray]) -> None:
        """把若干行追加到缓冲末尾（容量不足则倍增扩容），摊还 O(1)/行。"""
        if not vecs:
            return
        add = np.vstack(vecs)
        need = self._vec_len + len(add)
        if self._vec_buf is None:
            self._vec_buf = np.zeros((max(need, 64), self.dim), dtype=np.float32)
        elif need > len(self._vec_buf):
            cap = max(len(self._vec_buf) * 2, need)
            grown = np.zeros((cap, self.dim), dtype=np.float32)
            grown[:self._vec_len] = self._vec_buf[:self._vec_len]
            self._vec_buf = grown
        self._vec_buf[self._vec_len:need] = add
        self._vec_len = need

    @property
    def size(self) -> int:
        return len(self._idx_to_key)

    def count_for(self, doi: str) -> int:
        """该 DOI 已索引的 passage 数。"""
        return sum(1 for m in self._key_meta.values() if m.get("doi") == doi)

    # ---------------------------------------------------------------- 写入
    def index_paper(self, doi: str, note_text: str = "",
                    wiki_text: str = "", relations_text: str = "",
                    concepts: list[dict] | None = None,
                    title: str = "", force: bool = False,
                    folder: Path | None = None) -> int:
        """索引/刷新一篇文献的编译结果（**分块 + 内容 hash 增量**）。

        Args:
            folder: 该篇 kb 目录（用于把"命中块"从原文切回来；不传则退化为按
                `doi_to_dirname(doi)` 猜目录）。

        Returns:
            **新增** passage 数（内容变化导致的原地刷新不计入，日志里另有统计）。
        """
        if not self.api_key:
            logger.warning("embedding API key 未配置，跳过向量索引")
            return 0

        want: list[dict] = []          # [{key, embed_text, chunk_text, meta}]
        provided: set[str] = set()     # 本次提供了权威内容的 passage 类型
        rel_dir = self._rel_dir(folder, doi)

        if title and len(title.strip()) > 5:
            t = title.strip()
            provided.add("title")
            want.append({"key": f"{doi}__title", "embed_text": t, "chunk_text": t,
                         "meta": self._meta(doi, "title", 0, "", rel_dir, -1, -1)})

        for ptype, text in (("note", note_text), ("wiki", wiki_text),
                            ("relations", relations_text)):
            body = _body_of(text)
            if len(body) <= MIN_PASSAGE_CHARS:
                continue
            provided.add(ptype)
            label = _PTYPE_LABEL[ptype]
            for i, ch in enumerate(split_chunks(body, max_chars=CHUNK_CHARS,
                                                overlap_chars=CHUNK_OVERLAP)):
                key = f"{doi}__{ptype}" if i == 0 else f"{doi}__{ptype}#{i}"
                want.append({
                    "key": key,
                    "embed_text": embed_prefix(title, label, ch["section"]) + ch["text"],
                    "chunk_text": ch["text"],
                    "meta": self._meta(doi, ptype, i, ch["section"], rel_dir,
                                       ch["start"], ch["end"]),
                })

        if concepts:
            ctext = "\n".join(f"{c.get('name', '')}: {c.get('definition', '')}"
                              for c in concepts if c.get("name"))
            if len(ctext.strip()) > 20:
                provided.add("concepts")
                want.append({"key": f"{doi}__concepts", "embed_text": ctext,
                             "chunk_text": ctext,
                             "meta": self._meta(doi, "concepts", 0, "", rel_dir, -1, -1)})

        if not want:
            return 0

        pending: list[tuple[str, str]] = []      # [(key, embed_text)]
        meta_only: list[str] = []                # 内容未变、仅刷新偏移/小节的键
        refreshed = 0
        for item in want:
            key = item["key"]
            h = _md5(item["embed_text"])
            item["meta"]["hash"] = h
            item["meta"]["chunk_md5"] = _md5(item["chunk_text"])
            old = self._key_meta.get(key)
            if old is not None and key in self._key_to_idx and not force \
                    and old.get("hash") == h:
                self._key_meta[key] = item["meta"]   # 内容未变：只刷新偏移/小节
                meta_only.append(key)
                continue
            if old is not None and key in self._key_to_idx:
                refreshed += 1
            pending.append((key, item["embed_text"]))
            self._key_meta[key] = item["meta"]

        applied_keys: list[str] = []
        applied_vecs = np.zeros((0, self.dim), dtype=np.float32)
        added = 0
        if pending:
            from paperlit.vector import encode_texts
            embeddings = encode_texts([t for _, t in pending], model=self.model,
                                      api_key=self.api_key)
            ok: list[tuple[str, list[float]]] = []
            for (key, _), emb in zip(pending, embeddings):
                if emb and len(emb) == self.dim:
                    ok.append((key, emb))
                else:
                    # 编码失败：不留幽灵元数据 + 登记死信（否则该篇语义检索永久缺失、
                    # 且因为没人重试而永远不恢复）
                    self._key_meta.pop(key, None)
                    self._store.add_dead_letter(doi, "vector_index",
                                                f"embedding 编码失败/维度不符: {key}")
            applied_keys, applied_vecs, added = self._apply_embeddings(ok)

        # 清理同 ptype 的旧键（分块数减少 / 该产物已删除）
        # 走 SQLite 按 (doi,ptype) 取键，而不是扫描内存 `_key_meta`：后者 O(N)/篇 ⇒ 全量重建 O(N²)
        stale_keys: list[str] = []
        for ptype in provided:
            desired = {it["key"] for it in want if it["meta"]["ptype"] == ptype}
            stale_keys += [k for k in self._store.keys_for(doi, ptype)
                           if k not in desired]
        removed = self._remove_keys(stale_keys)

        # 持久化（**行级**，不重写全量）：段文件追加 + SQLite 行 upsert
        if applied_keys:
            self._store.append([dict(self._key_meta[k], key=k) for k in applied_keys],
                               applied_vecs)
        if meta_only:
            self._store.save_passage_meta([dict(self._key_meta[k], key=k)
                                           for k in meta_only if k in self._key_meta])
        if stale_keys:
            self._store.delete(stale_keys)
        if applied_keys or stale_keys:
            self._store.refresh_papers_state([doi])
        if applied_keys:
            self._store.resolve_for(doi, "vector_index")
            self._persist_model()

        # added 即 _apply_embeddings 返回的新增键数（≈ len(pending) - refreshed）
        if pending or removed or want:
            logger.info("向量索引更新: doi=%s 新增=%d 刷新=%d 清理=%d 总=%d",
                        doi, added, refreshed, removed, self.size)
        return max(0, added)

    def _meta(self, doi: str, ptype: str, chunk: int, section: str,
              rel_dir: str, start: int, end: int) -> dict:
        return {"doi": doi, "ptype": ptype, "chunk": chunk, "section": section,
                "file": rel_dir, "start": start, "end": end, "hash": "",
                "chunk_md5": ""}

    def _rel_dir(self, folder: Path | None, doi: str) -> str:
        """kb 目录的相对路径（供命中块回读；未知则空串）。"""
        if folder is None:
            return ""
        try:
            return Path(folder).relative_to(self.roots.kb_dir).as_posix()
        except (ValueError, OSError):
            return ""

    def _validate(self) -> None:
        """元数据/向量自洽性校验：不一致就清空内存索引（下次编译/重建会补回）。

        为什么要清：`_key_to_idx` 与 `_vectors` 行号错位会让"命中块→原文"张冠李戴
        （比"没有向量"坏得多）。宁可丢弃，不可错位。
        """
        n = len(self._idx_to_key)
        ok = (self._vectors is not None and len(self._vectors) == n
              and self._key_to_idx == {k: i for i, k in enumerate(self._idx_to_key)})
        if ok:
            return
        if n or self._vectors is not None:
            logger.warning("KB 向量索引元数据/向量不一致（keys=%d vec=%s），已清空待重建",
                           n, None if self._vectors is None else len(self._vectors))
        self._key_to_idx = {}
        self._idx_to_key = []
        self._key_meta = {}
        self._vectors = None
        self._index = None

    # ---------------------------------------------------------------- 读取
    def search(self, query: str, top_k: int = 20, exclude_doi: str = "",
               with_snippet: bool = False, store=None,
               expand_section: bool = True,
               snippet_chars: int = SNIPPET_CHARS) -> list[dict]:
        """向量相似度搜索（块级）。

        Args:
            exclude_doi: 排除的 DOI（搜索自身时排除）
            with_snippet: 附带片段（**命中段 = 注入段**，从产物文件按偏移切回）
            store: KBStore（`concepts` 类型的片段需要它重建定义文本）
            expand_section: 片段做 small-to-big 扩展到所属小节整段（≤snippet_chars）

        Returns:
            [{"key","doi","passage_type","chunk","section","score"[,"snippet"]}, ...]
        """
        if not self.api_key or self.size == 0:
            return []

        query_vec = self._encode_query(query)
        if not query_vec:
            return []

        output = []
        for key, score in self._search_by_vector(query_vec, top_k * 3):
            meta = self._meta_of(key)
            doi = meta.get("doi") or ""
            if exclude_doi and doi == exclude_doi:
                continue
            row = {
                "key": key,
                "doi": doi,
                "passage_type": meta.get("ptype") or "unknown",
                "chunk": int(meta.get("chunk") or 0),
                "section": meta.get("section") or "",
                "score": score,
            }
            if with_snippet:
                text = (self.section_text(key, limit=snippet_chars)
                        if expand_section else self.passage_text(key, store=store))
                if not text and (meta.get("ptype") or "") == "concepts":
                    text = self.passage_text(key, store=store)
                row["snippet"] = text
            output.append(row)
            if len(output) >= top_k:
                break
        return output

    def _encode_query(self, query: str) -> list[float]:
        """查询向量（带 SQLite 缓存；失败返回 []）。

        缓存的价值在 L3：同一批关键词会在多篇文献的候选检索里反复出现命中率高；
        问答的自然语言问句命中率低但成本是一次点查，无副作用。
        """
        from paperlit.vector import encode_query

        cache = _query_cache(self.roots) if _query_cache_enabled() else None
        if cache is not None:
            hit = cache.get(self.model, query)
            if hit:
                return hit
        vec = encode_query(query, model=self.model, api_key=self.api_key)
        if vec and len(vec) == self.dim and cache is not None:
            cache.put(self.model, query, vec)
        return vec or []

    def search_by_concepts(self, concept_names: list[str], top_k: int = 20,
                           store=None, exclude_doi: str = "") -> list[dict]:
        """混合检索：概念倒排过滤 + 向量相似度。

        Returns:
            [{"doi","score","shared_concepts",...}]，`shared_concepts` 是**真实交集**
            （旧实现直接把全部查询概念当成共有概念，会把无关概念写进 L3 关系图）。
        """
        if not concept_names:
            return []

        candidate_dois: set[str] = set()
        if store is not None:
            for name in concept_names:
                for r in store.concept_rows(name):
                    candidate_dois.add(r["paper_doi"])
        if exclude_doi:
            candidate_dois.discard(exclude_doi)
        if not candidate_dois:
            return []

        vector_results = self.search(" ".join(concept_names), top_k=top_k * 3,
                                     exclude_doi=exclude_doi)
        filtered = [r for r in vector_results if r["doi"] in candidate_dois]

        best: dict[str, dict] = {}
        for r in filtered:
            doi = r["doi"]
            if doi not in best or r["score"] > best[doi]["score"]:
                best[doi] = r

        out = []
        for doi, r in sorted(best.items(), key=lambda kv: kv[1]["score"], reverse=True):
            shared: list[str] = []
            if store is not None:
                try:
                    names = {c.get("name") for c in store.concepts_for_doi(doi)}
                    shared = [n for n in concept_names if n in names]
                except Exception:  # noqa: BLE001 - 交集查询失败不影响排序
                    shared = []
            out.append({"doi": doi, "score": r["score"],
                        "shared_concepts": shared,
                        "passage_type": r["passage_type"], "key": r["key"]})
        return out[:top_k]

    def section_text(self, key: str, *, limit: int = SNIPPET_CHARS) -> str:
        """small-to-big：把命中块扩到**所属小节整段**（同 (doi,ptype,section) 的块合并）。

        为什么：块级匹配精度高但上下文窄（900 字里可能只命中半句），业界做法是"小块匹配、
        大块注入"（parent-document / small-to-big）。小节是天然父级，且分块时同小节的块
        偏移本就连续，合并 = 取 min(start)..max(end) 的原文切片。
        校验：首块与末块的 md5 必须与索引一致（中间被改会让末块偏移变化而被发现），
        任一不符 → 退回单块（宁可窄，不可错）。
        """
        meta = self._meta_of(key)
        if (meta.get("ptype") or "") not in _PTYPE_FILE:
            return self.passage_text(key)
        section = meta.get("section") or ""
        if not section:
            return self.passage_text(key)
        sibs = [m for m in self._key_meta.values()
                if m.get("doi") == meta.get("doi") and m.get("ptype") == meta.get("ptype")
                and (m.get("section") or "") == section
                and int(m.get("start") or -1) >= 0 and int(m.get("end") or -1) > 0]
        if len(sibs) < 2:
            return self.passage_text(key)
        sibs.sort(key=lambda m: int(m["start"]))
        first, last = sibs[0], sibs[-1]
        path = self._passage_path(first)
        if path is None:
            return self.passage_text(key)
        try:
            body = _body_of(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return self.passage_text(key)
        head = body[int(first["start"]):int(first["end"])].strip()
        tail = body[int(last["start"]):int(last["end"])].strip()
        if _md5(head) != (first.get("chunk_md5") or "") \
                or _md5(tail) != (last.get("chunk_md5") or ""):
            logger.info("小节扩展偏移不符（文件已变），退回单块: %s", key)
            return self.passage_text(key)
        return boundary_trim(body[int(first["start"]):int(last["end"])].strip(), limit)

    def passage_text(self, key: str, store=None) -> str:
        """命中块原文（按索引时的偏移从产物切回；偏移不符则返回空串）。

        `_key_meta` 里的 `chunk_md5` 是索引时该块文本的摘要——文件被外部改动后
        偏移会失准，此时**宁可返回空**也不给出错位文本（宁可无证据，不可给错证据）。
        """
        meta = self._meta_of(key)
        ptype = meta.get("ptype") or ""
        if ptype == "title":
            return meta.get("text") or ""
        if ptype == "concepts":
            if store is None:
                return ""
            try:
                return "\n".join(
                    f"{c.get('name', '')}: {c.get('definition', '')}"
                    for c in store.concepts_for_doi(meta.get("doi") or "")
                    if c.get("name"))
            except Exception:  # noqa: BLE001
                return ""
        path = self._passage_path(meta)
        if path is None:
            return ""
        try:
            body = _body_of(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return ""
        start, end = int(meta.get("start") or -1), int(meta.get("end") or -1)
        if start < 0 or end <= start or end > len(body):
            return ""
        chunk = body[start:end].strip()
        if _md5(chunk) != (meta.get("chunk_md5") or ""):
            logger.info("块偏移与索引不符（文件已变）: %s", key)
            return ""
        return chunk

    def prune_unreadable(self) -> int:
        """摘除"产物文件已不在"的块（返回删除块数）。

        场景：文献目录被外部删除/移出（不经回收站流程）→ 向量仍在，语义检索照样召回，
        但命中块读不回原文（只会得到空片段）。这里做一次对账，把读不回的一律摘掉。
        """
        drop = [k for k, m in self._key_meta.items()
                if m.get("ptype") in _PTYPE_FILE and self._passage_path(m) is None]
        if not drop:
            return 0
        removed = self._remove_keys(drop)
        self._store.delete(drop)
        self._store.refresh_papers_state()
        logger.info("向量索引清理不可读块: %d（剩余 %d）", removed, self.size)
        return removed

    def _passage_path(self, meta: dict) -> Path | None:
        from .doi import doi_to_dirname

        name = _PTYPE_FILE.get(meta.get("ptype") or "")
        if not name:
            return None
        rel = meta.get("file") or ""
        if rel:
            p = self.roots.kb_dir / rel / name
            if p.is_file():
                return p
        p = self.roots.kb_dir / doi_to_dirname(meta.get("doi") or "") / name
        return p if p.is_file() else None

    def _meta_of(self, key: str) -> dict:
        meta = self._key_meta.get(key)
        if meta:
            return meta
        # 旧格式元数据兜底：`{doi}__{ptype}`（无 key_meta）
        doi, sep, ptype = key.rpartition("__")
        base, _, chunk = ptype.partition("#")
        return {"doi": doi if sep else key, "ptype": base or "unknown",
                "chunk": int(chunk) if chunk.isdigit() else 0,
                "section": "", "file": "", "start": -1, "end": -1}

    # ---------------------------------------------------------------- 索引底层
    def _apply_embeddings(self, items: list[tuple[str, list[float]]]
                          ) -> tuple[list[str], np.ndarray, int]:
        """批量写入向量，返回 `(全部写入键, 归一化向量, 其中新增键数)`。

        已存在的键**就地刷新**（内存行号稳定，避免整表重排）；新键追加到缓冲末尾；
        faiss 只在本批结束后重建**一次**。旧实现逐行 `np.vstack` + 逐行 `_rebuild_faiss()`
        ⇒ 每次追加都复制整个数组、重建整个索引 ⇒ 全量重建 O(N²)。

        返回**全部**键（含刷新键）：刷新也必须落盘（追加新行 + 重指向），否则磁盘上
        仍是被替换掉的旧向量（实测：只就地改内存会让重载后拿到陈旧向量）。
        """
        if not items:
            return [], np.zeros((0, self.dim), dtype=np.float32), 0
        all_keys: list[str] = []
        all_vecs: list[np.ndarray] = []
        tail: list[np.ndarray] = []
        new_count = 0
        inplace = False
        for key, embedding in items:
            vec = np.asarray([embedding], dtype=np.float32)
            norm = float(np.linalg.norm(vec))
            if norm > 0:
                vec = vec / norm
            idx = self._key_to_idx.get(key)
            if idx is not None and self._vec_len > idx:
                self._vec_buf[idx] = vec[0]
                inplace = True
            else:
                if idx is None:
                    self._key_to_idx[key] = len(self._idx_to_key)
                    self._idx_to_key.append(key)
                new_count += 1
                tail.append(vec[0])
            all_keys.append(key)
            all_vecs.append(vec[0])
        self._append_vectors(tail)
        if HAS_FAISS and inplace:
            self._faiss_dirty = True          # faiss 无法就地改行 → 检索前整表重建
        elif tail:
            self._faiss_add(np.vstack(tail))
        arr = np.ascontiguousarray(np.vstack(all_vecs)) if all_vecs \
            else np.zeros((0, self.dim), dtype=np.float32)
        return all_keys, arr, new_count

    def _remove_keys(self, keys: list[str]) -> int:
        """按 key 删除向量（缓冲重排 + faiss 重建）。"""
        drop = {k for k in keys if k in self._key_to_idx}
        if not drop:
            return 0
        keep = [i for i, k in enumerate(self._idx_to_key) if k not in drop]
        self._idx_to_key = [self._idx_to_key[i] for i in keep]
        self._key_to_idx = {k: i for i, k in enumerate(self._idx_to_key)}
        self._key_meta = {k: v for k, v in self._key_meta.items() if k not in drop}
        if self._vec_buf is not None and self._vec_len:
            self._vectors = self._vec_buf[:self._vec_len][keep] if keep else None
        self._faiss_dirty = True          # 行号已重排 → faiss 需在检索前整表重建
        return len(drop)

    def _rebuild_faiss(self) -> None:
        """整表重建 faiss 索引（O(N)）：删除/就地刷新后走 `_faiss_dirty` 延迟到检索前。"""
        if not HAS_FAISS:
            return
        self._faiss_dirty = False
        if self._vectors is None or len(self._vectors) == 0:
            self._index = None
            return
        self._index = faiss.IndexFlatIP(self.dim)
        self._index.add(np.ascontiguousarray(self._vectors, dtype=np.float32))

    def _faiss_add(self, vecs: np.ndarray) -> None:
        """**增量**追加到 faiss（O(新增行数)）。

        每篇都整表重建是 O(N²)：10 万篇 ≈104 万块，每次编译重加 100 万行不可接受。
        有脏行（删除/就地刷新）时退化为整表重建——那种情况本就少见。
        """
        if not HAS_FAISS or vecs is None or len(vecs) == 0:
            return
        if self._faiss_dirty or self._index is None:
            self._rebuild_faiss()
            return
        if self._index.ntotal + len(vecs) > self.size:
            self._rebuild_faiss()          # 行数对不上：宁可整表重建，不可错位
            return
        self._index.add(np.ascontiguousarray(vecs, dtype=np.float32))

    def _search_by_vector(self, query_vec: list[float], top_k: int
                          ) -> list[tuple[str, float]]:
        """向量最近邻搜索。"""
        if self.size == 0:
            return []
        q = np.array([query_vec], dtype=np.float32)

        if HAS_FAISS and self._index is not None:
            if self._faiss_dirty or self._index.ntotal != self.size:
                self._rebuild_faiss()      # 脏/错位：先对齐再查（宁可慢，不可错）
            if self._index is not None:
                faiss.normalize_L2(q)
                scores, indices = self._index.search(q, min(top_k, self.size))
                return [(self._idx_to_key[idx], float(score))
                        for score, idx in zip(scores[0], indices[0])
                        if 0 <= idx < len(self._idx_to_key)]

        if self._vectors is None or len(self._vectors) == 0:
            return []
        norm = np.linalg.norm(q)
        if norm > 0:
            q = q / norm
        sims = (self._vectors @ q.T).flatten()
        top_indices = np.argsort(sims)[::-1][:top_k]
        return [(self._idx_to_key[i], float(sims[i])) for i in top_indices]

    # ---------------------------------------------------------------- 持久化
    def _load_index(self) -> None:
        """从数据层加载索引（元数据 SQLite + 向量段文件），行序严格对齐。

        `_store.load()` 只返回段内仍被 `passages` 指向的行，因此被刷新/删除的旧行
        不会进入结果（旧格式 `kb_index_meta.json` / `kb_vectors.npy` 不再读取——
        数据层已由 `index_store` 接管；存量需重建或用 `index_store.import_legacy` 导入）。
        """
        try:
            keys, meta, vectors = self._store.load()
        except Exception as e:  # noqa: BLE001 - 索引损坏不应让编译失败
            logger.warning("加载 KB 向量索引失败: %s", e)
            return
        if not keys:
            return
        self._idx_to_key = list(keys)
        self._key_to_idx = {k: i for i, k in enumerate(keys)}
        self._key_meta = {k: dict(v) for k, v in meta.items()}
        self._vectors = vectors
        logger.info("KB vector index loaded: %d vectors (%s)", self.size,
                    self._store.health().get("segments"))

    def _persist_model(self) -> None:
        """记录模型指纹（不同向量空间禁止混用；健康接口展示）。"""
        try:
            self._store.set_meta("model", self.model)
            self._store.set_meta("dim", str(self.dim))
            self._store.set_meta("chunk_chars", str(CHUNK_CHARS))
        except Exception as e:  # noqa: BLE001
            logger.debug("写入索引 meta 失败: %s", e)

    def compact(self) -> dict:
        """压实段文件（回收刷新/删除产生的垃圾行）；需先重启/重载索引再使用。"""
        return self._store.compact()

    def health(self) -> dict:
        return self._store.health()

    # --- Protocol 兼容 ---
    def add_document(self, doi: str, text: str) -> None:
        self.index_paper(doi, note_text=text)

    def rebuild(self, roots: Roots) -> None:
        logger.info("[kb-vector] rebuild 需调用 build_from_kb()")


def build_vector_index(roots: Roots, impl: str = "noop",
                       api_key: str = "") -> VectorIndex:
    """按配置构建向量索引实现。"""
    if impl == "noop":
        return NoopVectorIndex(roots)
    if impl == "kb":
        return KbVectorIndex(roots, api_key=api_key)
    raise ValueError(f"未知向量实现: {impl!r}（支持 noop / kb）")


# ---------------------------------------------------------------- 进程级单例
# 旧路径每次检索（api.kb_vector_search）与每次编译（compile）都新建 `KbVectorIndex` 并
# **全量读盘**：10 万篇 ≈104 万块 ≈4.3GB，单次问答读一遍盘不可接受。单例后只加载一次，
# 写路径（index_paper / drop）都作用在同一实例上，内存与磁盘始终一致。
_INDEX_CACHE: dict[tuple, "KbVectorIndex"] = {}


def _default_api_key() -> str:
    import os

    return os.environ.get("SILICONFLOW_API_KEY", "").strip()


def get_kb_vector_index(roots: Roots, api_key: str = "",
                        model: str = "BAAI/bge-m3") -> KbVectorIndex:
    """按 (vector 根路径, api_key, model) 取进程级单例。"""
    key = (str(Path(roots.vector_dir).resolve()), api_key or _default_api_key(), model)
    idx = _INDEX_CACHE.get(key)
    if idx is None:
        idx = KbVectorIndex(roots, api_key=key[1], model=model)
        _INDEX_CACHE[key] = idx
    return idx


def reset_kb_vector_index(roots: Roots | None = None) -> None:
    """丢弃单例（测试/换根路径用）。"""
    if roots is None:
        _INDEX_CACHE.clear()
        return
    prefix = str(Path(roots.vector_dir).resolve())
    for k in [k for k in _INDEX_CACHE if k[0] == prefix]:
        _INDEX_CACHE.pop(k, None)


def _key_aliases(key: str) -> set[str]:
    """资源键的几种写法归一（DOI / RID `doi-…` / 目录名）。"""
    from .doi import dirname_to_doi, doi_to_dirname

    out = {key}
    if key.startswith("doi-"):
        out.add(key[4:])
    try:
        out.add(doi_to_dirname(key))
    except Exception:  # noqa: BLE001
        pass
    try:
        back = dirname_to_doi(key)
        if back:
            out.add(back)
    except Exception:  # noqa: BLE001
        pass
    return out


def drop_papers_from_index(roots: Roots, keys: list[str]) -> int:
    """把若干资源（DOI/RID/目录名任一写法）从向量索引里摘除，返回删除的块数。

    纯本地操作（**不调 embedding**）：文献移入回收站/删除后，若不摘除，语义检索仍会
    召回它，且命中块读不回原文（文件已不在）→ 空片段、"有引用无证据"。
    """
    from .doi import doi_to_dirname

    aliases: set[str] = set()
    for k in keys or []:
        if k:
            aliases |= _key_aliases(k)
    if not aliases:
        return 0
    idx = get_kb_vector_index(roots)
    drop = [k for k, m in idx._key_meta.items()
            if (m.get("doi") in aliases)
            or (doi_to_dirname(m.get("doi") or "") in aliases)]
    if not drop:
        return 0
    removed = idx._remove_keys(drop)
    idx._store.delete(drop)
    idx._store.refresh_papers_state()
    logger.info("向量索引摘除: keys=%s 删除块=%d 剩余=%d", sorted(aliases)[:3],
                removed, idx.size)
    return removed
