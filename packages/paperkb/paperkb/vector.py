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
import json
import logging
from pathlib import Path
from typing import Protocol

import numpy as np

from .config import Roots
from .textseg import embed_prefix, split_chunks, strip_frontmatter

logger = logging.getLogger(__name__)

EMBEDDING_DIM = 1024  # bge-m3 维度

# 分块参数：900 字/块（≈450 token，远低于 bge-m3 8192 上限）、120 字同节重叠。
# 实测 9 篇 25 个产物 → 68 块（约 7.5 块/篇），非标题内容零丢失。
CHUNK_CHARS = 900
CHUNK_OVERLAP = 120

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

        self._key_to_idx: dict[str, int] = {}
        self._idx_to_key: list[str] = []
        self._key_meta: dict[str, dict] = {}
        self._index = None
        self._vectors: np.ndarray | None = None

        self._load_meta()
        self._load_vectors()
        self._validate()
        self._rebuild_faiss()

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
            body = strip_frontmatter(text or "").strip()
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
                continue
            if old is not None and key in self._key_to_idx:
                refreshed += 1
            pending.append((key, item["embed_text"]))
            self._key_meta[key] = item["meta"]

        if pending:
            from paperlit.vector import encode_texts
            embeddings = encode_texts([t for _, t in pending], model=self.model,
                                      api_key=self.api_key)
            for (key, _), emb in zip(pending, embeddings):
                if emb and len(emb) == self.dim:
                    self._upsert(key, emb)
                else:
                    self._key_meta.pop(key, None)   # 编码失败：不留幽灵元数据

        # 清理同 ptype 的旧键（分块数减少 / 该产物已删除）
        removed = 0
        for ptype in provided:
            desired = {it["key"] for it in want if it["meta"]["ptype"] == ptype}
            stale = [k for k, m in self._key_meta.items()
                     if m.get("doi") == doi and m.get("ptype") == ptype
                     and k not in desired]
            removed += self._remove_keys(stale)

        added = len(pending) - refreshed
        if pending or removed or want:
            self._save_meta()
            self._save_vectors()
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
               with_snippet: bool = False, store=None) -> list[dict]:
        """向量相似度搜索（块级）。

        Args:
            exclude_doi: 排除的 DOI（搜索自身时排除）
            with_snippet: 命中块原文（从产物文件按偏移切回，**命中段 = 注入段**）
            store: KBStore（`concepts` 类型的片段需要它重建定义文本）

        Returns:
            [{"key","doi","passage_type","chunk","section","score"[,"snippet"]}, ...]
        """
        if not self.api_key or self.size == 0:
            return []

        from paperlit.vector import encode_query
        query_vec = encode_query(query, model=self.model, api_key=self.api_key)
        if not query_vec or len(query_vec) != self.dim:
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
                row["snippet"] = self.passage_text(key, store=store)
            output.append(row)
            if len(output) >= top_k:
                break
        return output

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
            body = strip_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
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
    def _upsert(self, key: str, embedding: list[float]) -> bool:
        """写入/就地刷新一条向量（`self._vectors` 是唯一真值）。"""
        vec = np.array([embedding], dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec = vec / norm
        idx = self._key_to_idx.get(key)
        if idx is not None and self._vectors is not None and idx < len(self._vectors):
            self._vectors[idx] = vec[0]
            self._rebuild_faiss()
            return False
        if idx is None:
            self._key_to_idx[key] = len(self._idx_to_key)
            self._idx_to_key.append(key)
        self._vectors = vec if self._vectors is None else np.vstack([self._vectors, vec])
        self._rebuild_faiss()
        return True

    def _remove_keys(self, keys: list[str]) -> int:
        """按 key 删除向量（numpy 真值重排 + faiss 重建）。"""
        drop = {k for k in keys if k in self._key_to_idx}
        if not drop:
            return 0
        keep = [i for i, k in enumerate(self._idx_to_key) if k not in drop]
        self._idx_to_key = [self._idx_to_key[i] for i in keep]
        self._key_to_idx = {k: i for i, k in enumerate(self._idx_to_key)}
        self._key_meta = {k: v for k, v in self._key_meta.items() if k not in drop}
        if self._vectors is not None:
            self._vectors = self._vectors[keep] if keep else None
        self._rebuild_faiss()
        return len(drop)

    def _rebuild_faiss(self) -> None:
        if not HAS_FAISS:
            return
        if self._vectors is None or len(self._vectors) == 0:
            self._index = None
            return
        self._index = faiss.IndexFlatIP(self.dim)
        self._index.add(self._vectors.astype(np.float32))

    def _search_by_vector(self, query_vec: list[float], top_k: int
                          ) -> list[tuple[str, float]]:
        """向量最近邻搜索。"""
        if self.size == 0:
            return []
        q = np.array([query_vec], dtype=np.float32)

        if HAS_FAISS and self._index is not None:
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
    def _save_meta(self) -> None:
        meta_path = self.index_dir / "kb_index_meta.json"
        meta = {
            "version": 2,
            "dim": self.dim,
            "model": self.model,
            "size": self.size,
            "chunk_chars": CHUNK_CHARS,
            "key_to_idx": self._key_to_idx,
            "idx_to_key": self._idx_to_key,
            "key_meta": self._key_meta,
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False),
                             encoding="utf-8")

    def _load_meta(self) -> None:
        meta_path = self.index_dir / "kb_index_meta.json"
        if not meta_path.exists():
            return
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self._key_to_idx = meta.get("key_to_idx", {})
            self._idx_to_key = meta.get("idx_to_key", [])
            self._key_meta = meta.get("key_meta", {}) or {}
            if not self._key_meta:      # v1 元数据：无 key_meta → 按旧键解析
                for k in self._idx_to_key:
                    self._key_meta[k] = self._meta_of(k)
        except Exception as e:
            logger.warning("Failed to load KB vector index meta: %s", e)

    def _load_vectors(self) -> None:
        vec_path = self.index_dir / "kb_vectors.npy"
        if not vec_path.exists():
            return
        try:
            self._vectors = np.load(vec_path)
            self._rebuild_faiss()
            logger.info("KB vector index loaded: %d vectors", len(self._vectors))
        except Exception as e:
            logger.warning("Failed to load KB vectors: %s", e)

    def _save_vectors(self) -> None:
        vec_path = self.index_dir / "kb_vectors.npy"
        if self._vectors is not None:
            np.save(vec_path, self._vectors)

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
