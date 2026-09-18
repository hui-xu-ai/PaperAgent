# -*- coding: utf-8 -*-
"""LitService：backend 侧 paperlit 门面薄访问器。

与 KbMetaService 不同，此处用**显式方法**而非 ``__getattr__`` 透传：
paperlit 返回 Pydantic 模型（Paper / SearchResult），
每个调用需要 ``.model_dump()`` 转 dict 以维持 API 契约稳定。

数据布局：``data/literature/lit.db`` + ``data/literature/faiss/``。
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from paperlit.config import LitSettings, Roots

logger = logging.getLogger(__name__)

_import_progress: dict = {}
_import_lock = threading.Lock()
_normalize_progress: dict = {}
_normalize_lock = threading.Lock()


def _to_dict(obj) -> dict:
    """Pydantic model → plain dict（兼容 model_dump / dict）。"""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    if isinstance(obj, dict):
        return obj
    return {"value": obj}


class LitService:
    """paperlit 门面薄访问器（backend 侧唯一入口）。"""

    def __init__(self, roots: Roots, settings: LitSettings | None = None,
                 settings_service=None) -> None:
        self._roots = roots
        self._settings = settings or LitSettings()
        self._settings_service = settings_service
        self._ready = False

    def _refresh_settings(self) -> None:
        """从 settings_service 刷新 API key（运行时可更新）。"""
        if self._settings_service:
            emb_key, rer_key = self._settings_service.get_lit_api_keys()
            if emb_key:
                self._settings.embedding_api_key = emb_key
            if rer_key:
                self._settings.reranker_api_key = rer_key

    def ensure(self) -> None:
        self._ensure()

    def _ensure(self) -> None:
        if self._ready:
            self._refresh_settings()
            return
        from paperlit import api as lit
        self._refresh_settings()
        lit.init_lit(self._roots, self._settings)
        self._ready = True
        logger.info("paperlit 初始化完成（db=%s）", self._roots.lit_db)

    @property
    def ready(self) -> bool:
        return self._ready

    # ---------------------------------------------------------- 状态总览

    def status(self) -> dict:
        self._ensure()
        from paperlit import api as lit
        try:
            topic_count = len(lit.list_topics(limit=1000))
        except Exception:
            topic_count = 0
        try:
            journal_mapping_count = lit.get_journal_mapper_stats().get("total", 0)
        except Exception:
            journal_mapping_count = 0
        return {
            "paper_count": lit.paper_count(),
            "citation_count": lit.citation_count(),
            "unenriched_count": lit.unenriched_count(),
            "vector_index_size": lit.vector_index_size(),
            "ranked_count": lit.ranked_count(),
            "topic_count": topic_count,
            "journal_mapping_count": journal_mapping_count,
            "if_covered_count": lit.if_covered_count(),
        }

    # ---------------------------------------------------------- 导入

    def ingest_bib(self, path: str, import_refs: bool = True,
                   min_year: int | None = None, max_year: int | None = None) -> dict:
        self._ensure()
        from paperlit import api as lit
        return lit.ingest_bib(path, import_refs=import_refs,
                              min_year=min_year, max_year=max_year)

    def ingest_bib_dir(self, path: str, import_refs: bool = True,
                       min_year: int | None = None, max_year: int | None = None) -> dict:
        self._ensure()
        from paperlit import api as lit
        return lit.ingest_bib_dir(path, import_refs=import_refs,
                                  min_year=min_year, max_year=max_year)

    def ingest_upload(self, filename: str, data: bytes) -> dict:
        """上传 bib → 临时文件 → ingest_bib → 清理。"""
        import tempfile
        self._ensure()
        from paperlit import api as lit
        suffix = Path(filename).suffix or ".bib"
        tmp = Path(tempfile.gettempdir()) / f"lit_{filename}{suffix}"
        tmp.write_bytes(data)
        try:
            return lit.ingest_bib(str(tmp), source_file=filename)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    # ---------------------------------------------------------- 异步导入 + 进度

    def get_import_progress(self) -> dict:
        with _import_lock:
            return dict(_import_progress)

    def ingest_bib_async(self, path: str, import_refs: bool = True,
                         min_year: int | None = None,
                         max_year: int | None = None) -> str:
        self._ensure()
        task_id = f"import_{int(time.time() * 1000)}"
        with _import_lock:
            _import_progress.clear()
            _import_progress.update({
                "task_id": task_id, "status": "running",
                "current": 0, "total": 0, "phase": "解析文件",
                "doi": "", "result": None, "error": None,
            })

        def _run():
            try:
                from paperlit import api as lit

                def cb(current, total, doi, phase=None):
                    with _import_lock:
                        _import_progress["current"] = current
                        _import_progress["total"] = total
                        _import_progress["doi"] = doi
                        if phase:
                            _import_progress["phase"] = phase

                with _import_lock:
                    _import_progress["phase"] = "解析文件"
                result = lit.ingest_bib(path, import_refs=import_refs,
                                        min_year=min_year, max_year=max_year,
                                        progress_cb=cb)
                with _import_lock:
                    _import_progress["status"] = "done"
                    _import_progress["phase"] = "完成"
                    _import_progress["result"] = result
            except Exception as e:
                logger.exception("异步导入失败")
                with _import_lock:
                    _import_progress["status"] = "error"
                    _import_progress["error"] = str(e)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return task_id

    def ingest_upload_async(self, filename: str, data: bytes) -> str:
        import tempfile
        self._ensure()
        suffix = Path(filename).suffix or ".bib"
        tmp = Path(tempfile.gettempdir()) / f"lit_{filename}{suffix}"
        tmp.write_bytes(data)
        task_id = f"upload_{int(time.time() * 1000)}"
        with _import_lock:
            _import_progress.clear()
            _import_progress.update({
                "task_id": task_id, "status": "running",
                "current": 0, "total": 0, "phase": "解析文件",
                "doi": "", "result": None, "error": None,
            })

        def _run():
            try:
                from paperlit import api as lit

                def cb(current, total, doi, phase=None):
                    with _import_lock:
                        _import_progress["current"] = current
                        _import_progress["total"] = total
                        _import_progress["doi"] = doi
                        if phase:
                            _import_progress["phase"] = phase

                result = lit.ingest_bib(str(tmp), source_file=filename,
                                        progress_cb=cb)
                with _import_lock:
                    _import_progress["status"] = "done"
                    _import_progress["phase"] = "完成"
                    _import_progress["result"] = result
            except Exception as e:
                logger.exception("异步上传导入失败")
                with _import_lock:
                    _import_progress["status"] = "error"
                    _import_progress["error"] = str(e)
            finally:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return task_id

    # ---------------------------------------------------------- 文献浏览

    def get_paper(self, doi: str) -> dict | None:
        self._ensure()
        from paperlit import api as lit
        paper = lit.get_paper(doi)
        return _to_dict(paper) if paper else None

    def list_papers(self, offset: int = 0, limit: int = 20,
                    is_reference: bool | None = None) -> dict:
        self._ensure()
        from paperlit import api as lit
        papers = lit.list_papers(offset=offset, limit=limit,
                                 is_reference=is_reference)
        return {
            "items": [_to_dict(p) for p in papers],
            "total": lit.paper_count(),
        }

    def paper_count(self) -> int:
        self._ensure()
        from paperlit import api as lit
        return lit.paper_count()

    def citation_count(self) -> int:
        self._ensure()
        from paperlit import api as lit
        return lit.citation_count()

    # ---------------------------------------------------------- 关键词检索（FTS5）

    def search_keyword(self, query: str, limit: int = 20) -> list[dict]:
        self._ensure()
        from paperlit import api as lit
        results = lit.search_keyword(query, limit=limit)
        return [_to_dict(r) for r in results]

    # ---------------------------------------------------------- 元数据补全

    def enrich_pending(self) -> dict:
        self._ensure()
        from paperlit import api as lit
        return lit.enrich_pending()

    def enrich_batch(self, dois: list[str]) -> dict:
        self._ensure()
        from paperlit import api as lit
        return lit.enrich_batch(dois)

    def enrich_one(self, doi: str) -> dict:
        self._ensure()
        from paperlit import api as lit
        return lit.enrich_one(doi)

    def unenriched_count(self) -> int:
        self._ensure()
        from paperlit import api as lit
        return lit.unenriched_count()

    def pending_dois(self, include_non_wos: bool = True) -> list[str]:
        self._ensure()
        from paperlit import api as lit
        return lit.pending_dois(include_non_wos=include_non_wos)

    def all_dois(self) -> list[str]:
        self._ensure()
        from paperlit import api as lit
        return lit.all_dois()

    # ---------------------------------------------------------- 期刊名规范化

    def normalize_journals(self) -> dict:
        self._ensure()
        from paperlit import api as lit
        return lit.normalize_journals()

    def get_normalize_progress(self) -> dict:
        with _normalize_lock:
            return dict(_normalize_progress)

    def normalize_journals_async(self) -> str:
        """后台线程跑规范化，进度经 get_normalize_progress 查询。"""
        self._ensure()
        task_id = f"normalize_{int(time.time() * 1000)}"
        with _normalize_lock:
            _normalize_progress.clear()
            _normalize_progress.update({
                "task_id": task_id, "status": "running",
                "current": 0, "total": 0, "journal": "",
                "result": None, "error": None,
            })

        def _run():
            try:
                from paperlit import api as lit

                def cb(current, total, journal):
                    with _normalize_lock:
                        _normalize_progress["current"] = current
                        _normalize_progress["total"] = total
                        _normalize_progress["journal"] = journal

                result = lit.normalize_journals(progress_cb=cb)
                with _normalize_lock:
                    _normalize_progress["status"] = "done"
                    _normalize_progress["result"] = result
            except Exception as e:
                logger.exception("异步规范化失败")
                with _normalize_lock:
                    _normalize_progress["status"] = "error"
                    _normalize_progress["error"] = str(e)

        threading.Thread(target=_run, daemon=True).start()
        return task_id

    def get_unique_journals(self) -> list[dict]:
        self._ensure()
        from paperlit import api as lit
        return lit.get_unique_journals()

    def get_journal_mapper_stats(self) -> dict:
        self._ensure()
        from paperlit import api as lit
        return lit.get_journal_mapper_stats()

    def add_journal_mapping(self, abbreviation: str, full_name: str,
                            source: str = "manual") -> bool:
        self._ensure()
        from paperlit import api as lit
        return lit.add_journal_mapping(abbreviation, full_name, source)

    # ---------------------------------------------------------- 文献清洗

    def attach_journal_metrics(self) -> dict:
        self._ensure()
        from paperlit import api as lit
        return lit.attach_journal_metrics(self._roots.journals_db)

    def compute_library_citations(self) -> dict:
        self._ensure()
        from paperlit import api as lit
        return lit.compute_library_citations()

    def preview_cleaning(self, rules: dict) -> dict:
        self._ensure()
        from paperlit import api as lit
        return lit.preview_cleaning(rules)

    def execute_cleaning(self, rules: dict, mode: str = "delete") -> dict:
        self._ensure()
        from paperlit import api as lit
        return lit.execute_cleaning(rules, mode)

    # ---------------------------------------------------------- 引用图谱

    def compute_paper_rank(self, damping: float = 0.85) -> dict:
        self._ensure()
        from paperlit import api as lit
        return lit.compute_paper_rank(damping=damping)

    def top_papers(self, limit: int = 20) -> list[dict]:
        self._ensure()
        from paperlit import api as lit
        return lit.top_papers(limit=limit)

    def compute_clusters(self, min_cocitations: int = 2) -> dict:
        self._ensure()
        from paperlit import api as lit
        return lit.compute_clusters(min_cocitations=min_cocitations)

    def cluster_papers(self, cluster_id: int) -> list[dict]:
        self._ensure()
        from paperlit import api as lit
        return lit.cluster_papers(cluster_id)

    # ---------------------------------------------------------- 文献计量图谱

    def graph_network(self, *, kb_dois: set[str] | None = None,
                      preview: bool = False, **filters) -> dict:
        """引用网络导出（服务端过滤）。kb_dois 由路由层注入（标记 in_kb）。"""
        self._ensure()
        from paperlit import api as lit
        return lit.graph_network(kb_dois=kb_dois, preview=preview, **filters)

    def graph_filter_facets(self, *, kb_dois: set[str] | None = None) -> dict:
        self._ensure()
        from paperlit import api as lit
        return lit.graph_filter_facets(kb_dois=kb_dois)

    def graph_neighbors(self, doi: str) -> dict:
        self._ensure()
        from paperlit import api as lit
        return lit.graph_neighbors(doi)

    def graph_node_detail(self, doi: str) -> dict | None:
        self._ensure()
        from paperlit import api as lit
        return lit.graph_node_detail(doi)

    # ---------------------------------------------------------- 向量索引

    def build_vector_index(self, batch_size: int = 32) -> dict:
        self._ensure()
        from paperlit import api as lit
        api_key = self._settings.embedding_api_key
        return lit.build_vector_index(api_key=api_key, batch_size=batch_size)

    def vector_search(self, query: str, top_k: int = 20) -> list[dict]:
        self._ensure()
        from paperlit import api as lit
        api_key = self._settings.embedding_api_key
        results = lit.vector_search(query, api_key=api_key, top_k=top_k)
        return [{"doi": doi, "score": score} for doi, score in results]

    def vector_index_size(self) -> int:
        self._ensure()
        from paperlit import api as lit
        return lit.vector_index_size()

    # ---------------------------------------------------------- 三层漏斗检索

    def search(self, query: str) -> list[dict]:
        self._ensure()
        from paperlit import api as lit
        results = lit.search(query, reranker_api_key=self._settings.reranker_api_key)
        return [_to_dict(r) for r in results]

    def cached_search(self, query: str) -> list[dict]:
        self._ensure()
        from paperlit import api as lit
        results = lit.cached_search(query,
                                    reranker_api_key=self._settings.reranker_api_key)
        return [_to_dict(r) for r in results]

    # ---------------------------------------------------------- 查询缓存

    def cache_stats(self) -> dict:
        self._ensure()
        from paperlit import api as lit
        return lit.cache_stats()

    def cache_clear(self) -> int:
        self._ensure()
        from paperlit import api as lit
        return lit.cache_clear()

    # ---------------------------------------------------------- 主题编译

    def compile_topics(self, field: str = "wos_categories",
                       min_papers: int = 2) -> dict:
        self._ensure()
        from paperlit import api as lit
        return lit.compile_topics(field=field, min_papers=min_papers)

    def list_topics(self, limit: int = 50, offset: int = 0) -> list[dict]:
        self._ensure()
        from paperlit import api as lit
        return lit.list_topics(limit=limit, offset=offset)

    def get_topic(self, slug: str) -> dict | None:
        self._ensure()
        from paperlit import api as lit
        return lit.get_topic(slug)
