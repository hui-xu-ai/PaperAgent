# -*- coding: utf-8 -*-
"""编译系统（卡帕西 LLM Wiki 风格，KB-DESIGN v0.6 §6）。

- L1+L2 合并编译：一次 LLM 调用同时产出 L1（一句话/六维/概念）+ L2（深度 wiki）→ 省 50% 全文输入
- L1 知识编译：_note.md（一句话贡献 + 六维总结 + 概念标签 + AI 评分）
- L2 深度 wiki：_wiki.md（方法论批判 + 可复现性 + 应用转化）
- L3 概念关系层：_relations.md（用编译结果代替全文，分析跨文献概念关系，省 77% token）
- 队列：compile_jobs 状态机；价值分决定等级；幂等（done **且产物在**才跳过 / 产物存在跳过 / force 覆盖）
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from .config import Roots
from .context import _is_ref_section, context_paragraphs, shared_ctx, with_task
from .db import KBStore
from .doc import PaperDoc, find_document_in_kb, read_document
from .journals import JournalsDB
from .llm import get_llm
from .models import PaperMeta
from .textseg import boundary_trim

logger = logging.getLogger(__name__)

NOTE_TEMPLATE = """---
type: paper-note
doi: {doi}
tags: [paper{concepts}]
---

# {title}

## 一句话贡献
> {one_liner}

## 六维总结
### 研究背景
{background}
### 研究方法
{method}
### 研究结果
{result}
### 结论
{conclusion}
### 创新点
{innovation}
### 局限
{limitation}

## 概念标签
{tags}

## 延伸阅读
- 原文：[[en|English]]
- 深度编译：[[_wiki|深度编译]]（高价值文献）
"""

# L1/L2 压缩版字数（下级注入省 token）
_CTX_LIMIT = 1000


class CompileError(Exception):
    pass


class Compiler:
    def __init__(self, store: KBStore, journals: JournalsDB | None,
                 roots: Roots):
        self.store = store
        self.journals = journals
        self.roots = roots

    # ---------------------------------------------------------- 入口
    def compile(self, doi: str, level: str, force: bool = False) -> dict:
        """编译入口（键可为 DOI / RID / 目录名 / md5 目录；统一归一化后执行）。"""
        doi = self._canon(doi)
        level = level.upper()
        if level not in ("L1", "L2", "L3"):
            raise ValueError(f"未知编译等级: {level}")
        self._ensure_source(doi)
        fn = {"L1": self._compile_l1, "L2": self._compile_l2,
              "L3": self._compile_l3}[level]
        return fn(doi, force)

    def _ensure_source(self, doi: str) -> None:
        """编译前确保 kb 已纳入该文献原文层四件（缺失才同步；失败不在此处抛）。

        P0-B step4：键可为 RID/目录名（无 DOI 文献）；同步从 library 按同一键解析。
        """
        try:
            from .imports import sync_source_to_kb

            store = getattr(self, "store", None)   # 单测可只注入 roots（无 store）
            # 2026-09-12 用户反馈：旧实现在"kb 已有 document.json"时直接 return，
            # 于是 kb 缺的文件（典型：`source.pdf`）**永远补不回来**。
            # `sync_source_to_kb(force=False)` 本身是"已存在即跳过、只补缺不覆盖"的冻结
            # 语义 → 可以安全地每次都调，代价是几次 os.path 探测。
            r = sync_source_to_kb(doi, self.roots, force=False, store=store)
            logger.info("编译前纳入 kb: key=%s copied=%s skipped=%s",
                        doi, r.get("copied"), r.get("skipped"))
        except Exception as e:  # noqa: BLE001 - 交由下方读取阶段报出更准确的错误
            logger.warning("编译前纳入 kb 失败（继续尝试编译）: key=%s err=%s", doi, e)

    def _current_score(self, doi: str) -> float:
        """该篇**当前价值分**（与 `/api/kb-meta/scores`、批量入队同一判据）。

        批5（2026-09-12 用户实测 bug：编译队列「价值分」显示 0.00）：入队时若调用方未给分值，
        旧实现写死 0.0 ⇒ 队列表格显示 0.00，与知识库列表里的真实分（该篇 3.48）不一致。
        现在缺省即现算；取不到（无元数据/无期刊库）按 0 记，不抛错、不阻塞入队。
        """
        try:
            from .api import value_score_for      # 惰性导入：api 在模块级依赖本模块
            s = value_score_for(doi)
            return float((s or {}).get("score") or 0.0)
        except Exception as e:  # noqa: BLE001 - 评分失败不阻塞入队
            logger.warning("入队取价值分失败（按 0 记）: doi=%s err=%s", doi, e)
            return 0.0

    def queue(self, doi: str, level: str, value_score: float | None = None,
              priority: int = 0) -> dict:
        """入队（幂等：同键+level 存在则跳过；done 后可 force 重编）。

        `value_score=None`（默认）⇒ **现算当前价值分**（批5 修复：此前缺省 0.0，队列显示 0.00）；
        调用方显式传入（如 `queue_all_by_value` 批量入队）则用传入值。

        A3：kb 中无 document.json（bib-only 篇）→ **不入队**，返回
        skipped_no_doc（不抛异常、不产生 queued/failed 垃圾）。

        A5（bib 可空）：**未导 bib（无 papers_meta）不再硬拦**——编译执行时从
        document.json 兜底元数据（title/abstract）；缺关键元数据（标题/摘要）仅提示
        不阻断。缺失 DOI 仍可由 missing_dois 生成的 WOS 检索式补足（不在此拦）。

        P0-B step4（2026-09-12）：**无 DOI 文献同样可入队**——键可以是 rid（`nd-…`）
        或目录名；document.json 在 library 里（尚未纳入 kb）也算命中，编译时自动纳入。

        2026-09-11 bug 修复：done 幂等加**产物存在性**第二判据——只信 compile_jobs
        状态会在"产物被清理后重新解析"时永远挡掉重建（实测 `10.1002/adma.202407106`
        即此因），故 status=done 但产物缺失时继续往下走 find_doc 检查并重新入队。
        """
        doi = self._canon(doi)
        row = self._job(doi, level)
        if row and row["status"] == "done" and self._artifact_exists(doi, level):
            return {"status": "skipped_done", "doi": doi, "level": level}
        from .resource import find_doc
        store = getattr(self, "store", None)
        # library 侧兜底：无 DOI 文献的 document.json 可能落在 md5 目录里，
        # 与键（nd-<指纹12>）无字面关系 → 允许模糊扫描（几十篇规模，代价可接受）。
        if (find_document_in_kb(self.roots.kb_dir, doi, store) is None
                and find_doc(self.roots.library_dir, doi, store,
                             search_root=self.roots.library_dir) is None):
            return {"status": "skipped_no_doc", "doi": doi, "level": level}
        self.store.upsert_job(doi, level, status="queued",
                              value_score=(self._current_score(doi)
                                           if value_score is None else float(value_score)),
                              priority=priority)
        return {"status": "queued", "doi": doi, "level": level}

    def process_next(self) -> dict | None:
        """处理队列中最高优先级的一项（worker 循环调用）。"""
        job = self._next_job()
        if job is None:
            return None
        doi, level = job["paper_doi"], job["level"]
        try:
            r = self.compile(doi, level)
            return {"doi": doi, "level": level, "result": r}
        except CompileError as e:
            self.store.upsert_job(doi, level, status="failed", error=str(e))
            return {"doi": doi, "level": level, "error": str(e)}

    def queue_all_by_value(self, scores: list[dict]) -> dict:
        """按价值分批量入队：L2(≥4.0)/L1(其余，全做)。"""
        n = {"L1": 0, "L2": 0}
        for s in scores:
            level = s.get("level") or "L1"
            try:
                self.queue(s["doi"], level, value_score=s.get("score", 0))
                n[level] += 1
            except CompileError:
                continue
        return n

    # ---------------------------------------------------------- L1+L2 合并编译
    def _compile_l1(self, doi: str, force: bool) -> dict:
        """L1+L2 合并编译：一次 LLM 调用同时产出 _note.md + _wiki.md（节省 50% 全文输入）。

        如果合并编译失败或 L2 输出不完整，回退到只产出 L1。
        """
        doc = self._doc(doi)
        meta = self._resolve_meta(doi, doc)
        journal_meta = self._journal_meta(meta.journal, meta.issn, meta.eissn)
        note = self._note_path(doi)
        wiki = self._wiki_path(doi)
        meta_json = meta.model_dump(mode="json")
        if note.exists() and not force:
            return {"status": "skipped_existing", "doi": doi, "level": "L1"}
        llm = get_llm()
        qa_ctx = self._qa_context(doi)

        # 尝试合并编译（L1+L2 一次调用）
        prompt = _prompt_l1_l2_merged(meta_json, doc, journal_meta, qa_ctx=qa_ctx)
        raw = llm.complete(prompt, context="compile")
        data = _parse_json(raw)

        # 拆分 L1/L2 输出
        l1_data, l2_data = _split_l1_l2_merged(data)

        # 验证 L1 输出完整性
        if not (isinstance(l1_data, dict) and l1_data.get("one_liner")):
            l1_data = _salvage_l1(raw)
        if not isinstance(l1_data, dict) or not l1_data.get("one_liner"):
            raise CompileError("L1 编译输出无效（JSON 缺失 one_liner）")

        # 保存 L1 产物
        note.write_text(_render_note(meta, l1_data, journal_meta), encoding="utf-8")
        self._save_ctx(doi, "L1", _ctx_from_l1(l1_data))
        self._mark_done(doi, "L1")
        self._index_paper_notes(doi)

        # 向量索引（编译后自动更新）
        self._maybe_vector_index(doi, l1_data, l2_data, title=meta.title)

        # AI 评分保存（无论 L2 是否完成都要保存）
        ai_value = l1_data.get("ai_value")
        topic_score = l1_data.get("topic_score")
        l2_done = False
        l2_queued = False
        if ai_value is not None or topic_score is not None:
            self._save_ai_scores_and_maybe_l2(
                doi, ai_value, topic_score if self._get_preferred_topics() else None)

        # 保存 L2 产物（如果存在）
        if l2_data and l2_data.get("wiki"):
            try:
                wiki.write_text(_render_wiki(meta, l2_data), encoding="utf-8")
                self._save_ctx(doi, "L2", (l2_data.get("wiki") or "")[: _CTX_LIMIT])
                self._mark_done(doi, "L2")
                self._index_paper_notes(doi)
                # 概念聚合（从 L2 的 concepts）
                concepts = l2_data.get("concepts") or []
                self._aggregate_concepts(doi, concepts)
                l2_done = True
                logger.info("L1+L2 合并编译成功: doi=%s", doi)
                # L2 完成 → 检查是否自动升级 L3（AI评分已保存，可以正确计算value_score）
                self._maybe_auto_l3(doi)
            except Exception as e:  # noqa: BLE001
                logger.warning("L2 产物保存失败（L1 不受影响）: doi=%s err=%s", doi, e)
        
        # 如果L2没有在合并编译中完成，检查是否需要入队
        if not l2_done and (ai_value is not None or topic_score is not None):
            l2_queued = True

        return {"status": "done", "doi": doi, "level": "L1",
                "concepts": l1_data.get("concepts", []),
                "ai_value": ai_value, "topic_score": topic_score,
                "l2_merged": l2_done, "l2_auto_queued": l2_queued}

    # ---------------------------------------------------------- L2（深度 wiki）
    def _compile_l2(self, doi: str, force: bool) -> dict:
        doc = self._doc(doi)
        meta = self._resolve_meta(doi, doc)
        wiki = self._wiki_path(doi)
        if wiki.exists() and not force:
            return {"status": "skipped_existing", "doi": doi, "level": "L2"}
        l1_ctx = self._ctx(doi, "L1")
        llm = get_llm()
        prompt = _prompt_l2(meta.model_dump(mode="json"), doc, l1_ctx)
        raw = llm.complete(prompt, context="compile")
        data = _parse_json(raw)
        if not isinstance(data, dict):
            raise CompileError("L2 编译输出无效")
        wiki.write_text(_render_wiki(meta, data), encoding="utf-8")
        self._save_ctx(doi, "L2", (data.get("wiki") or str(data))[: _CTX_LIMIT])
        self._mark_done(doi, "L2")
        self._index_paper_notes(doi)
        concepts = data.get("concepts") or []
        agg = self._aggregate_concepts(doi, concepts)
        self._maybe_vector_index(doi, {"concepts": concepts}, {"wiki": data.get("wiki"), "concepts": concepts},
                                 title=meta.title)
        self._maybe_auto_l3(doi)
        return {"status": "done", "doi": doi, "level": "L2",
                "concepts": len(concepts), "concept_pages": agg}

    # ---------------------------------------------------------- L3（概念关系层）
    def _compile_l3(self, doi: str, force: bool) -> dict:
        """L3 概念关系层：LLM 驱动检索 + 跨文献关系分析。

        两步走：
        1. LLM 读取本文 L1+L2，提取检索关键词
        2. 用关键词搜索向量索引（标题+笔记+wiki+概念），取回候选文献编译结果
        3. LLM 分析跨文献概念关系 → _relations.md

        省 77% token（~20K vs 全文 ~88K）。
        """
        meta = self._resolve_meta_l3(doi)
        relations_path = self._relations_path(doi)
        if relations_path.exists() and not force:
            return {"status": "skipped_existing", "doi": doi, "level": "L3"}

        self_ctx = self._compiled_context(doi)
        if not self_ctx:
            raise CompileError(f"L3 无法读取编译结果: {doi}")

        llm = get_llm()

        # 第一步：LLM 提取检索关键词；失败回退到已聚合概念/bib 关键词（不硬判死）
        keyword_prompt = _prompt_l3_keywords(meta.model_dump(mode="json"), self_ctx)
        keyword_raw = llm.complete(keyword_prompt, context="compile")
        keywords = self._parse_l3_keywords(keyword_raw)
        if not keywords:
            keywords = self._fallback_keywords(doi, meta)
            logger.info("L3 关键词提取失败，回退概念/标签: doi=%s kw=%s",
                        doi, keywords[:5])

        # 第二步：候选检索——向量优先；空则回退混合检索（概念倒排+引用+共被引，零 API）
        related = (self._search_related_by_keywords(keywords, doi, top_k=15)
                   if keywords else [])
        if not related:
            related = self._find_related_papers(doi, top_k=10)
            if related:
                logger.info("L3 向量检索空，回退混合检索: doi=%s n=%d",
                            doi, len(related))
        if not related:
            raise CompileError(
                f"L3 无相关文献（向量+混合检索均空，需库内≥2篇已编译同领域文献）: {doi}")

        # 第三步：取回候选文献编译结果 + LLM 分析关系
        related_ctxs = []
        for r in related:
            ctx = self._compiled_context(r["doi"])
            if ctx:
                related_ctxs.append({"doi": r["doi"], "context": ctx,
                                     "score": r.get("score", 0),
                                     "connection": (r.get("passage_type")
                                                    or r.get("connection") or "")})

        if not related_ctxs:
            raise CompileError("L3 候选文献无编译结果")

        prompt = _prompt_l3(meta.model_dump(mode="json"), self_ctx, related_ctxs)
        raw = llm.complete(prompt, context="compile")
        data = _parse_json(raw)
        if not isinstance(data, dict):
            raise CompileError("L3 编译输出无效")

        relations_path.write_text(
            _render_relations(meta, data, related_ctxs), encoding="utf-8")
        self._save_ctx(doi, "L3",
                       (data.get("summary") or "")[: _CTX_LIMIT])
        self._mark_done(doi, "L3")
        self._index_paper_notes(doi)
        self._maybe_vector_index(doi, {}, {}, title=meta.title)

        # 双向 cross_refs：把本文 wiki link 追加到相关文献的 _wiki.md
        self._bidirectional_cross_refs(doi, related_ctxs)

        return {"status": "done", "doi": doi, "level": "L3",
                "related": len(related_ctxs),
                "keywords": keywords[:5],
                "concepts": data.get("concept_map", [])}

    def _search_related_by_keywords(self, keywords: list[str],
                                    exclude_doi: str, top_k: int = 15
                                    ) -> list[dict]:
        """用关键词搜索向量索引，返回去重后的候选文献列表。"""
        import os
        api_key = os.environ.get("SILICONFLOW_API_KEY", "").strip()
        if not api_key:
            return []

        try:
            from .api import _settings
            if getattr(_settings, "vector_impl", "noop") != "kb":
                return []
        except Exception:  # noqa: BLE001
            return []

        from .vector import KbVectorIndex
        idx = KbVectorIndex(self.roots, api_key=api_key)

        seen_dois: set[str] = set()
        results: list[dict] = []

        # 每个关键词独立搜索，按 DOI 聚合最高分
        doi_best: dict[str, dict] = {}
        for kw in keywords:
            hits = idx.search(kw, top_k=top_k, exclude_doi=exclude_doi)
            for h in hits:
                d = h["doi"]
                if d == exclude_doi:
                    continue
                if d not in doi_best or h["score"] > doi_best[d]["score"]:
                    doi_best[d] = h

        # 按分数排序
        sorted_hits = sorted(doi_best.values(), key=lambda x: x["score"], reverse=True)
        return sorted_hits[:top_k]

    @staticmethod
    def _parse_l3_keywords(raw: str) -> list[str]:
        """从 LLM 输出中提取检索关键词（JSON 数组或逗号分隔）。"""
        if not raw:
            return []
        text = raw.strip()
        # 去掉 ```json ... ``` 标记
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()
        # 尝试 JSON 解析
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return [str(k).strip() for k in data if str(k).strip()]
            if isinstance(data, dict):
                kws = data.get("keywords") or data.get("search_terms") or []
                return [str(k).strip() for k in kws if str(k).strip()]
        except (json.JSONDecodeError, ValueError):
            pass
        # 兜底：按逗号/换行分割，过滤掉非关键词字符
        parts = re.split(r"[,，\n\[\]]+", text)
        keywords = []
        for p in parts:
            p = p.strip().strip('"').strip("'").strip()
            # 去掉序号（1. 2. 等）
            p = re.sub(r"^\d+[\.\)]\s*", "", p)
            # 去掉标题标记（# 等）
            p = re.sub(r"^#+\s*", "", p)
            # 过滤掉空字符串和纯符号
            if p and not re.match(r'^[\s\[\]{}"\'`,]+$', p):
                # 过滤掉明显的标题/说明文字
                if not re.search(r'(检索|关键词|提取|搜索|关键词提取)', p, re.IGNORECASE):
                    keywords.append(p)
        return keywords

    def _fallback_keywords(self, doi: str, meta) -> list[str]:
        """关键词提取失败时的回退：已聚合概念名 + bib 关键词（零 LLM 成本）。"""
        kws: list[str] = []
        try:
            for c in self.store.concepts_for_doi(doi):
                n = (c.get("name") or "").strip()
                if n and n not in kws:
                    kws.append(n)
        except Exception:  # noqa: BLE001
            pass
        try:
            raw = getattr(meta, "keywords_json", "") or ""
            for k in (json.loads(raw) if raw else []):
                k = str(k).strip()
                if k and k not in kws:
                    kws.append(k)
        except Exception:  # noqa: BLE001
            pass
        return kws[:8]

    def _find_related_papers(self, doi: str, top_k: int = 10
                             ) -> list[dict]:
        """混合检索相关文献：概念倒排 + 向量 + 引用 + 共被引。

        Returns:
            [{"doi": "...", "score": 0.8, "connection": "concept:GNN"}, ...]
        """
        candidates: dict[str, dict] = {}

        # 通道 1：概念倒排（从 concepts 表）
        concepts = self.store.concepts_for_doi(doi)
        if concepts:
            concept_names = [c["name"] for c in concepts[:7]]
            # 向量索引混合检索
            try:
                import os
                api_key = os.environ.get("SILICONFLOW_API_KEY", "").strip()
                if api_key:
                    from .api import _settings
                    if getattr(_settings, "vector_impl", "noop") == "kb":
                        from .vector import KbVectorIndex
                        idx = KbVectorIndex(self.roots, api_key=api_key)
                        vec_results = idx.search_by_concepts(
                            concept_names, top_k=top_k * 2,
                            store=self.store, exclude_doi=doi)
                        for r in vec_results:
                            d = r["doi"]
                            if d not in candidates:
                                candidates[d] = {
                                    "doi": d, "score": 0, "connection": ""}
                            candidates[d]["score"] = max(
                                candidates[d]["score"], r["score"])
                            shared = r.get("shared_concepts", [])
                            if shared:
                                candidates[d]["connection"] = (
                                    f"concept:{shared[0]}")
            except Exception as e:  # noqa: BLE001
                logger.warning("L3 向量检索失败（继续其他通道）: %s", e)

            # 纯 SQL 概念共现（零成本补充）
            for name in concept_names:
                for row in self.store.concept_rows(name):
                    d = row["paper_doi"]
                    if d == doi:
                        continue
                    if d not in candidates:
                        candidates[d] = {"doi": d, "score": 0,
                                         "connection": ""}
                    candidates[d]["score"] = max(candidates[d]["score"], 0.3)
                    if not candidates[d]["connection"]:
                        candidates[d]["connection"] = f"concept:{name}"

        # 通道 2：引用关系
        try:
            rows = self.store.citation_peers(doi, limit=top_k)
            for r in rows:
                d = r.get("doi", "")
                if d and d not in candidates:
                    candidates[d] = {"doi": d, "score": 0.4,
                                     "connection": "citation"}
        except Exception:  # noqa: BLE001
            pass

        # 通道 3：共被引聚类
        try:
            meta_obj = self._resolve_meta(doi)
            cluster = getattr(meta_obj, "cocitation_cluster", 0) or 0
            if cluster > 0:
                peers = self.store.cluster_peers(
                    cluster, exclude_doi=doi, limit=top_k)
                for p in peers:
                    d = p.get("doi", "")
                    if d and d not in candidates:
                        candidates[d] = {"doi": d, "score": 0.35,
                                         "connection": "cluster"}
        except Exception:  # noqa: BLE001
            pass

        # 按分数排序，取 top_k
        sorted_candidates = sorted(
            candidates.values(), key=lambda x: x["score"], reverse=True)
        return sorted_candidates[:top_k]

    def _compiled_context(self, doi: str, limit_chars: int = 1500) -> str:
        """读取某篇文献的 L1+L2 编译结果，压缩为上下文。"""
        folder = self._kb_folder(doi)
        parts = []

        note_path = folder / "_note.md"
        if note_path.exists():
            note_text = note_path.read_text(encoding="utf-8", errors="replace")
            # 提取关键段落：one_liner + 六维 + 概念标签
            parts.append(self._extract_note_summary(note_text, limit_chars))

        wiki_path = folder / "_wiki.md"
        if wiki_path.exists():
            wiki_text = wiki_path.read_text(encoding="utf-8", errors="replace")
            parts.append(self._extract_wiki_summary(wiki_text, limit_chars // 2))

        return "\n\n".join(parts) if parts else ""

    @staticmethod
    def _extract_note_summary(note_text: str, limit: int) -> str:
        """从 _note.md 提取一句话贡献 + 六维摘要 + 概念标签。"""
        lines = note_text.split("\n")
        summary_lines = []
        in_section = ""
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("## 一句话贡献"):
                in_section = "one_liner"
                continue
            elif stripped.startswith("## 六维总结"):
                in_section = "six_dim"
                continue
            elif stripped.startswith("## 概念标签"):
                in_section = "concepts"
                continue
            elif stripped.startswith("## "):
                in_section = ""
                continue

            if in_section and stripped and not stripped.startswith(">"):
                summary_lines.append(stripped)
            elif in_section == "one_liner" and stripped.startswith(">"):
                summary_lines.append(stripped.lstrip("> ").strip())

        result = "\n".join(summary_lines)
        return boundary_trim(result, limit)

    @staticmethod
    def _extract_wiki_summary(wiki_text: str, limit: int) -> str:
        """从 _wiki.md 提取深度编译摘要（去 frontmatter）。"""
        lines = wiki_text.split("\n")
        # 跳过 frontmatter
        start = 0
        if lines and lines[0].strip() == "---":
            for i, line in enumerate(lines[1:], 1):
                if line.strip() == "---":
                    start = i + 1
                    break

        content = "\n".join(lines[start:]).strip()
        return boundary_trim(content, limit)

    def _bidirectional_cross_refs(self, doi: str,
                                  related_ctxs: list[dict]) -> None:
        """双向 cross_refs：在相关文献的 _wiki.md 末尾**合并**一条指向本文的 wiki link。

        2026-09-21 审计修复：旧实现每次都 `f.write(f"\\n\\n## 相关文献\\n- {link}\\n")` ——
        每被引用一次就多出一个 `## 相关文献` 段（实测 snb 的 _wiki.md 尾部有 6 个重复段），
        文件虚胖 3359 字符：既挤占向量嵌入预算（把正文挤出 3000 字上限），
        又让"相关文献"在检索里反复命中。现在写进**唯一段**并去重。
        """
        from .doi import doi_to_dirname

        self_dirname = doi_to_dirname(doi)
        link_to_self = f"[[{self_dirname}/_note]]"

        for r in related_ctxs:
            related_doi = r["doi"]
            wiki_path = self._wiki_path(related_doi)
            if not wiki_path.exists():
                continue
            try:
                text = wiki_path.read_text(encoding="utf-8", errors="replace")
                if link_to_self in text:
                    continue  # 已存在
                line = f"- {link_to_self}（{r.get('connection', '')}）"
                wiki_path.write_text(_merge_cross_ref(text, line),
                                     encoding="utf-8")
            except Exception as e:  # noqa: BLE001
                logger.warning("cross_ref 追加失败: %s → %s: %s",
                               doi, related_doi, e)

    # ---------------------------------------------------------- 概念页（惰性聚合）
    def _aggregate_concepts(self, doi: str, concepts: list[dict]) -> int:
        """记录 L2 概念 → 同名概念 ≥3 文献时生成/更新 _concepts/<slug>.md。

        概念名先归一化（去括号限定语/小写/去标点）再入库与计数——否则 LLM 每篇
        措辞不同（"n-p junction (heterointerface)" vs "n-p Junction Internal ..."）
        永远撞不满 3 篇阈值，概念页一张都生成不出（2026-09-21 审计：210 行/207 个不同名）。
        """
        if not concepts:
            return 0
        created = 0
        for c in concepts:
            raw_name = (c.get("name") or "").strip()
            if not raw_name:
                continue
            name = _canon_concept(raw_name)
            slug = re.sub(r"[^\w\-]+", "-", name).strip("-")[:60]
            if not slug:
                continue
            self.store.upsert_concept(doi, name, c.get("definition", ""))
            n = self.store.concept_count(name)
            if n >= 3:
                page = self.roots.kb_dir / "_concepts" / f"{slug}.md"
                page.parent.mkdir(parents=True, exist_ok=True)
                page.write_text(self._render_concept_page(name), encoding="utf-8")
                created += 1
        return created

    def rebuild_concept_pages(self) -> dict:
        """一次性回灌：把既有 concepts 行归一化重建，并重新生成 ≥3 文献的概念页。

        旧数据用 LLM 原始名存储（归一化前概念页永远空）；本方法读取全表→归一化去重→
        重写→按归一名计数生成概念页。幂等，可重复跑。
        """
        from collections import Counter

        rows = self.store.all_concepts()
        canon: dict[tuple[str, str], str] = {}   # (doi, 归一名) -> 定义（取最长）
        for r in rows:
            doi = r.get("paper_doi") or ""
            name = _canon_concept(r.get("name") or "")
            if not doi or not name:
                continue
            defn = r.get("definition") or ""
            key = (doi, name)
            if key not in canon or len(defn) > len(canon[key]):
                canon[key] = defn
        self.store.clear_concepts()
        for (doi, name), defn in canon.items():
            self.store.upsert_concept(doi, name, defn)
        cnt = Counter(name for (_doi, name) in canon.keys())
        created = 0
        for name, n in cnt.items():
            if n < 3:
                continue
            slug = re.sub(r"[^\w\-]+", "-", name).strip("-")[:60]
            if not slug:
                continue
            page = self.roots.kb_dir / "_concepts" / f"{slug}.md"
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_text(self._render_concept_page(name), encoding="utf-8")
            created += 1
        return {"rows": len(rows), "canonical": len(canon),
                "distinct": len(cnt), "pages": created}

    def _render_concept_page(self, name: str) -> str:
        from .doi import doi_to_dirname

        rows = self.store.concept_rows(name)
        lines = [f"---\ntype: concept\ntags: [concept]\n---\n",
                 f"# 概念：{name}\n",
                 "## 定义（多文献聚合）\n"]
        for r in rows:
            # 目录名形式（DOI 的 "/" → "_"），否则 Obsidian 死链
            lines.append(f"- **[[{doi_to_dirname(r['paper_doi'])}/_note"
                         f"|{r['paper_doi']}]]**：{r['definition']}")
        return "\n".join(lines) + "\n"

    # ---------------------------------------------------------- 辅助
    def _index_paper_notes(self, doi: str) -> None:
        """该篇编译产物（_note/_wiki）入 notes_fts（M4 检索主对象）。"""
        from .doi import doi_to_dirname

        folder = self._kb_folder(doi, create=True)
        files = []
        for name in ("_note.md", "_wiki.md", "_relations.md"):
            p = folder / name
            if p.exists():
                files.append({"filename": name,
                              "content": p.read_text(encoding="utf-8", errors="replace")})
        if files:
            self.store.index_notes(doi, files)

    def _maybe_vector_index(self, doi: str, l1_data: dict,
                            l2_data: dict | None, title: str = "") -> None:
        """编译后自动更新向量索引（需 vector_impl=kb + embedding API key）。"""
        try:
            from .api import _settings
            if getattr(_settings, "vector_impl", "noop") != "kb":
                return
        except Exception:  # noqa: BLE001
            return

        import os
        api_key = os.environ.get("SILICONFLOW_API_KEY", "").strip()
        if not api_key:
            return

        try:
            from .vector import KbVectorIndex
            idx = KbVectorIndex(self.roots, api_key=api_key)

            note_text = ""
            note_path = self._note_path(doi)
            if note_path.exists():
                note_text = note_path.read_text(encoding="utf-8", errors="replace")

            wiki_text = ""
            wiki_path = self._wiki_path(doi)
            if wiki_path.exists():
                wiki_text = wiki_path.read_text(encoding="utf-8", errors="replace")

            relations_text = ""
            relations_path = self._relations_path(doi)
            if relations_path.exists():
                relations_text = relations_path.read_text(encoding="utf-8",
                                                          errors="replace")

            concepts = l1_data.get("concepts") or []
            if l2_data and l2_data.get("concepts"):
                existing_names = {c.get("name") for c in concepts}
                for c in l2_data["concepts"]:
                    if c.get("name") and c["name"] not in existing_names:
                        concepts.append(c)

            added = idx.index_paper(doi, note_text=note_text, wiki_text=wiki_text,
                                    relations_text=relations_text,
                                    concepts=concepts, title=title,
                                    folder=self._kb_folder(doi))
            if added:
                logger.info("向量索引已更新: doi=%s added=%d", doi, added)
        except Exception as e:  # noqa: BLE001
            logger.warning("向量索引更新失败（不影响编译）: doi=%s err=%s", doi, e)

    def _canon(self, key: str) -> str:
        """键归一化（P0-B step4）：DOI / RID / 目录名 / md5 目录 → 同一资源键。

        编译任务、产物目录、notes 索引都以此为键——不归一化会出现"同一篇两个键"
        （用 DOI 入队、用 RID 查不到任务）。

        ⚠️ **DOI 一律保持原样**（P0-B step2 约定：键解析内置，既有调用方零改动；
        `compile_jobs.paper_doi` / `compile_ctx` 表按 DOI 存量数据不能变键），
        只有"非 DOI 形态"的键（RID 目录名 / md5 目录）才归一化。
        """
        from .doi import is_doi

        k = (key or "").strip()
        if not k or is_doi(k):
            return k
        from .resource import resource_key

        return resource_key(getattr(self, "store", None), k) or k

    def _kb_folder(self, key: str, *, create: bool = False) -> Path:
        """该资源的 kb 目录（P0-B step4：键可为 DOI/RID/目录名/md5 目录）。

        已存在则返回真实目录（兼容历史命名）；不存在时按**候选首位**命名
        （`doi-…` → `doi_to_dirname`；`nd-…`/目录名 → 原样），必要时创建。
        """
        from .resource import candidate_dirnames, resources_dir

        store = getattr(self, "store", None)
        hit = resources_dir(self.roots.kb_dir, key, store)
        if hit is not None:
            return hit
        cands = candidate_dirnames(key, store)
        folder = self.roots.kb_dir / (cands[-1] if len(cands) > 1 and cands[0].startswith("doi-")
                                      else (cands[0] if cands else "nd-untitled"))
        if create:
            folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _doc(self, doi: str) -> PaperDoc:
        p = find_document_in_kb(self.roots.kb_dir, doi, self.store)
        if p is None:
            raise CompileError(f"kb 中无 document.json: {doi}（先纳入知识库）")
        return read_document(p)

    def _resolve_meta(self, doi: str, doc: PaperDoc | None = None) -> PaperMeta:
        """编译元数据：优先 bib 权威（papers_meta）；未导 bib（可空）→ document.json 兜底。

        A5：缺关键元数据（标题/摘要）时**提示但不阻断**——编译依赖 document.json 正文
        全文（text_en，经 paper_context）；无 bib 也能产出笔记。缺失 DOI 仍由
        missing_dois.WOS 检索式补足。

        P0-B step4：键可以是 rid（`nd-…`，无 DOI 文献）——按 rid 查不到时回退到
        元数据表里"同一目录"的记录（`meta_for`）。
        """
        from .resource import meta_for

        meta = self.store.get_meta(doi)
        if meta is not None:
            return meta
        d = doc or self._doc(doi)
        logger.info("编译无 bib 元数据，用 document.json 兜底 title（%s）", doi)
        return meta_for(self.store, doi, d)

    def _resolve_meta_l3(self, doi: str) -> PaperMeta:
        """L3 轻量元数据解析：优先 papers_meta，其次从 _note.md frontmatter 提取。

        L3 只需要 doi + title（渲染用），不需要 document.json 全文。
        """
        meta = self.store.get_meta(doi)
        if meta is not None:
            return meta
        note_path = self._note_path(doi)
        if note_path.exists():
            text = note_path.read_text(encoding="utf-8")
            title = ""
            for line in text.splitlines():
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
            return PaperMeta(doi=doi, title=title or doi)
        return PaperMeta(doi=doi, title=doi)

    def _note_path(self, doi: str) -> Path:
        return self._kb_folder(doi) / "_note.md"

    def _wiki_path(self, doi: str) -> Path:
        return self._kb_folder(doi) / "_wiki.md"

    def _relations_path(self, doi: str) -> Path:
        return self._kb_folder(doi) / "_relations.md"

    def _artifact_path(self, doi: str, level: str) -> Path | None:
        """该等级的编译主产物单文件。"""
        lv = (level or "").strip().upper()
        if lv == "L1":
            return self._note_path(doi)
        if lv == "L2":
            return self._wiki_path(doi)
        if lv == "L3":
            return self._relations_path(doi)
        return None

    def _artifact_exists(self, doi: str, level: str) -> bool:
        """done 幂等的第二判据：该等级产物**确实还在磁盘上**。

        bug（2026-09-11 实测）：knowledge_base 下该篇目录被清理后 compile_jobs 仍是
        done，重新解析时入队被幂等挡掉 → 产物永不重建（用户看不到它进知识库）。
        故"done"只在前置条件"产物在"时才算完成；产物缺失 → 视为需重建。
        """
        p = self._artifact_path(doi, level)
        if p is None:
            return True  # 未知等级：保持旧语义（不因判据本身改变行为）
        try:
            return p.exists()
        except OSError as e:  # 目录不可读等：保守按"在"处理，不改变既有跳过语义
            logger.warning("编译产物检查失败（按已存在处理）: key=%s level=%s err=%s",
                           doi, level, e)
            return True

    def _journal_meta(self, journal: str, issn: str, eissn: str) -> str:
        if self.journals is None:
            return ""
        info = self.journals.lookup_issn(issn, eissn)
        if info is None and journal:
            info = self.journals.lookup(journal)
        if not info:
            return journal or ""
        j = info.get("jcr") or {}
        c = info.get("cas") or {}
        bits = [str(j.get("quartile") or ""), f"JIF {j.get('jif')}"]
        if c.get("zone"):
            bits.append(f"中科院{c['zone']}区")
        return " ".join(x for x in bits if x)

    def _job(self, doi: str, level: str) -> dict | None:
        return self.store.get_job(doi, level)

    def _next_job(self) -> dict | None:
        return self.store.next_job()

    def _save_ctx(self, doi: str, level: str, summary: str) -> None:
        self.store.upsert_ctx(doi, level, summary)

    def _ctx(self, doi: str, level: str) -> str:
        return self.store.get_ctx(doi, level)

    def _mark_done(self, doi: str, level: str) -> None:
        # 2026-09-12：补写 done_at（此前恒空 → 无法判断产物是什么时候编出来的）。
        self.store.upsert_job(doi, level, status="done",
                              done_at=datetime.now().isoformat(timespec="seconds"))

    def _cluster_context(self, meta: PaperMeta, limit: int = 5) -> str:
        """同共被引聚类的其他文献列表（供 cross_refs 后处理使用）。

        从 papers_meta 查同 cocitation_cluster 的其他文献，按 paper_rank 降序取 top N。
        无聚类数据时返回空字符串。
        """
        cluster = getattr(meta, "cocitation_cluster", 0) or 0
        if cluster <= 0:
            return ""
        rows = self.store.cluster_peers(cluster, exclude_doi=meta.doi, limit=limit)
        if not rows:
            return ""
        parts = []
        for r in rows:
            title = r.get("title", "") or "(无标题)"
            year = r.get("year", "") or ""
            journal = r.get("journal", "") or ""
            cited = r.get("times_cited", 0) or 0
            line = f"- {title}（{journal} {year}，被引 {cited}）"
            parts.append(line)
        return "## 同聚类文献（共被引聚类，供对比参考）\n" + "\n".join(parts)

    def _qa_context(self, doi: str, limit_chars: int = 800) -> str:
        """该文献的用户 QA 卡片（Query→Wiki 反馈循环）。

        读取 kb/<DOI>/cards/ 下 card-qa 类型卡片，提取问题与答案摘要。
        编译时注入 → LLM 知道用户关心什么，六维总结可侧重这些热点。
        无 QA 卡片时返回空字符串。
        """
        from .cards import read_cards_for_compile
        kb_dir = self.roots.kb_dir
        raw = read_cards_for_compile(kb_dir, doi, types=["card-qa"],
                                     limit_chars=limit_chars)
        if not raw:
            return ""
        return "## 用户关注热点（已有问答，编译时请侧重这些方面）\n" + raw

    def _get_preferred_topics(self) -> list[str]:
        """从全局设置读取用户配置的主题表（导入界面设置）。"""
        try:
            from . import _settings
            return list(getattr(_settings, "preferred_topics", None) or [])
        except Exception:  # noqa: BLE001
            return []

    def _save_ai_scores_and_maybe_l2(self, doi: str,
                                      ai_value: float | None,
                                      topic_score: float | None) -> None:
        """保存 AI 评分到 papers_meta，重算价值分，达标则自动入队 L2（深度 wiki）。"""
        try:
            self.store.update_ai_scores(doi, ai_value_score=ai_value,
                                        topic_score=topic_score)
            self.store.fill_journal_meta(doi)
            from .api import value_score_for
            from .score import L2_THRESHOLD
            new_score = value_score_for(doi)
            if new_score and new_score.get("score", 0) >= L2_THRESHOLD:
                l2_job = self.store.get_job(doi, "L2")
                if l2_job is None or l2_job.get("status") != "done":
                    self.queue(doi, "L2", value_score=new_score["score"])
                    logger.info("AI 评分触发 L2 自动升级: %s score=%.2f",
                                doi, new_score["score"])
        except Exception as e:  # noqa: BLE001
            logger.warning("AI 评分保存/L2 升级失败（不阻断编译）: doi=%s err=%s", doi, e)

    def _maybe_auto_l3(self, doi: str) -> None:
        """L2 编译完成后：重算价值分，≥ L3_THRESHOLD 且 ai_value 可用 → 自动入队 L3。"""
        try:
            self.store.fill_journal_meta(doi)
            from .api import value_score_for
            from .score import L3_THRESHOLD
            new_score = value_score_for(doi)
            if not new_score:
                return
            parts = new_score.get("parts", {})
            ai_value_info = parts.get("ai_value", {})
            ai_value = ai_value_info.get("value") if ai_value_info else None
            ai_available = ai_value_info.get("available", False) if ai_value_info else False
            if not ai_available or ai_value is None or ai_value <= 0:
                return
            if new_score.get("score", 0) >= L3_THRESHOLD:
                l3_job = self.store.get_job(doi, "L3")
                if l3_job is None or l3_job.get("status") != "done":
                    self.queue(doi, "L3", value_score=new_score["score"])
                    logger.info("L2 完成触发 L3 自动升级: %s score=%.2f ai_value=%.2f",
                                doi, new_score["score"], ai_value)
        except Exception as e:  # noqa: BLE001
            logger.warning("L3 自动升级失败（不阻断编译）: doi=%s err=%s", doi, e)


# ---------------------------------------------------------------- 提示词

# 编译输出格式硬约束（2026-09-21 用户拍板）：编译产物（_note/_wiki/_relations）是给 AI/人
# 理解的知识卡，**不需要精确排版公式**。glm 等模型会把共享前缀"公式对照表"里的 LaTeX 原样抄进
# JSON 字符串，单反斜杠是非法 JSON 转义 → 整段解析失败（"L1 编译输出无效（JSON 缺失 one_liner）"）。
# 从源头禁止 LaTeX/反斜杠即可根除，比事后修转义更稳（成熟模型对此类指令遵循度高）。
# 注意：**不改 shared_ctx**（它与翻译逐字节共享以命中前缀缓存），只在任务后缀加约束；
# `_jsonutil` 防御式解析仍保留作残余兜底（模型偶发不听话时）。
_NO_LATEX_RULE = (
    "【输出格式硬约束】只用纯文本和简单 Markdown，严禁使用 LaTeX 数学公式："
    "不要出现美元符号包裹的公式，不要出现任何反斜杠命令（例如 mathrm、approx、frac 这类带反斜杠的写法），"
    "也不要用下划线或脱字符做上下标。化合物与离子请用普通文字书写（例如 Co(Ox/Px)、K+、BF4-、Co2P），"
    "数值与单位用普通字符（例如 约 20000 S/cm、9.80 Am2/kg）。"
    "本知识笔记仅供 AI 与人理解，无需精确公式排版；如需指代某个公式，请用文字描述其物理含义即可。\n"
)


def _prompt_l1(meta: dict, doc: PaperDoc, journal_meta: str,
               qa_ctx: str = "") -> str:
    """L1 编译 prompt：**共享全文前缀**（header + 原文全文块）在前，任务指令在后。

    一次调用产出整个 _note.md：一行摘要(one_liner) + 六维(带 [Pxxx] 引用) + 概念/标签 + AI 评分。
    """
    from .frontmatter import meta_block

    shared = shared_ctx(doc)
    qa_block = ("\n\n" + qa_ctx) if qa_ctx else ""
    task = (
        "你是科研知识编译助手。请依据上方论文全文，把它编译成结构化中文知识笔记。\n"
        + _NO_LATEX_RULE +
        "要求：六维每维必须引用论文段落 ID（如 [P001]）；输出严格 JSON：\n"
        '{"one_liner": "一句话贡献", "background": {"text": "...", "paras": ["P001"]}, '
        '"method": {...}, "result": {...}, "conclusion": {...}, "innovation": {...}, '
        '"limitation": {...}, "concepts": [{"name": "概念名(英文)", "definition": "定义"}], '
        '"tags": ["标签"]}\n'
        "不要输出 JSON 以外的任何内容。\n\n"
        "基于你对论文全文阅读，评估其研究价值（0-5 分），输出 ai_value（0-5 的浮点数）：\n"
        "5=开创性 / 4=高质量 / 3=扎实 / 2=常规 / 1=低质量 / 0=无价值\n"
        + meta_block(meta, doc)
        + (f"\n期刊(权威)：{journal_meta}" if journal_meta else "")
        + qa_block
    )
    return with_task(shared, task)


def _prompt_l2(meta: dict, doc: PaperDoc, l1_ctx: str) -> str:
    """L2 深度编译 prompt：共享全文前缀在前，任务在后。

    产出 _wiki.md：方法论批判 / 可复现性 / 应用转化（L1 未覆盖的深度分析维度）。
    """
    shared = shared_ctx(doc)
    task = (
        "你是科研深度编译专家。基于论文产出深度知识卡（JSON）。\n"
        + _NO_LATEX_RULE +
        "⚠️ L1 已覆盖六维摘要（背景/方法/结果/结论/创新/局限）。\n"
        "你的 wiki **禁止重复**上述内容，只写 L1 未涉及的深度分析。\n"
        "输出严格 JSON：\n"
        '{"wiki": "深度编译 Markdown：## 方法论批判'
        '（设计缺陷/统计效力/内外部效度）'
        '/ ## 可复现性分析（数据/代码/实验条件）'
        '/ ## 潜在应用与转化路径'
        '（正文每条引用段落 ID [P001]）", '
        '"concepts": [{"name": "概念名(英文)", "definition": "定义"}]}\n'
        "不要输出 JSON 以外的内容。\n\n"
        + f"## L1 摘要（已覆盖，勿重复）\n{l1_ctx or '(无)'}"
    )
    return with_task(shared, task)


def _prompt_l1_l2_merged(meta: dict, doc: PaperDoc, journal_meta: str,
                          qa_ctx: str = "") -> str:
    """L1+L2 合并编译 prompt：一次调用产出两级知识卡。

    输出 JSON 包含 L1（one_liner/六维/concepts/scores）+ L2（wiki），
    相比分离调用节省 50% 全文输入 token。
    """
    from .frontmatter import meta_block

    shared = shared_ctx(doc)
    qa_block = ("\n\n" + qa_ctx) if qa_ctx else ""
    task = (
        "你是科研知识编译专家。请依据上方论文全文，产出两级结构化中文知识卡（JSON）。\n\n"
        + _NO_LATEX_RULE + "\n"
        "## L1 知识卡（基础摘要）\n"
        "- one_liner: 一句话核心贡献（≤50字）\n"
        "- background/method/result/conclusion/innovation/limitation: 六维总结\n"
        "  每维格式：{\"text\": \"内容\", \"paras\": [\"P001\"]}（必须引用段落 ID）\n"
        "- concepts: 3-7 个核心概念，格式 [{\"name\": \"概念名(英文)\", \"definition\": \"定义\"}]\n"
        "- tags: 标签列表\n\n"
        "## L2 深度分析（禁止重复六维摘要）\n"
        "- wiki: 深度编译 Markdown，包含三个章节：\n"
        "  ## 方法论批判（设计缺陷/统计效力/内外部效度）\n"
        "  ## 可复现性分析（数据/代码/实验条件）\n"
        "  ## 潜在应用与转化路径（引用段落 ID [P001]）\n\n"
        "## AI 评分\n"
        "- ai_value: 0-5 研究价值（5=开创性/4=高质量/3=扎实/2=常规/1=低质量/0=无价值）\n"
        "- topic_score: 0-1 主题相关度\n\n"
        "输出严格 JSON：\n"
        '{"one_liner": "...", "background": {"text": "...", "paras": ["P001"]}, '
        '"method": {...}, "result": {...}, "conclusion": {...}, "innovation": {...}, '
        '"limitation": {...}, "wiki": "## 方法论批判\\n...\\n## 可复现性分析\\n...\\n## 潜在应用\\n...", '
        '"concepts": [{"name": "...", "definition": "..."}], "tags": ["..."], '
        '"ai_value": 4, "topic_score": 0.8}\n'
        "不要输出 JSON 以外的任何内容。\n\n"
        + meta_block(meta, doc)
        + (f"\n期刊(权威)：{journal_meta}" if journal_meta else "")
        + qa_block
    )
    return with_task(shared, task)


def _split_l1_l2_merged(data: dict) -> tuple[dict, dict | None]:
    """拆分合并编译输出为 L1 和 L2 两部分。

    返回 (l1_data, l2_data)，l2_data 可能为 None（如果 wiki 字段缺失）。
    """
    l1_keys = {"one_liner", "background", "method", "result",
               "conclusion", "innovation", "limitation",
               "concepts", "tags", "ai_value", "topic_score"}

    l1_data = {k: v for k, v in data.items() if k in l1_keys}
    l2_data = None

    # L2 部分：wiki 字段存在且非空
    wiki = data.get("wiki")
    if wiki and isinstance(wiki, str) and len(wiki.strip()) > 50:
        l2_data = {
            "wiki": wiki,
            "concepts": data.get("concepts", []),  # L2 也记录概念
        }

    return l1_data, l2_data


# ---------------------------------------------------------------- 渲染

def _render_note(meta, data: dict, journal_meta: str = "") -> str:
    """渲染 `_note.md`（L1 产物）。元数据（作者/期刊/被引等）由翻译 frontmatter 承载，
    _note.md 只保留知识内容（one_liner + 六维 + 概念标签）。"""
    concepts = "".join(f", {c}" for c in data.get("tags", [])[:8])
    if not concepts:
        concepts = ", paper"

    def six(key: str) -> str:
        item = data.get(key) or {}
        if isinstance(item, str):
            return item
        text = item.get("text") or ""
        paras = item.get("paras") or []
        return text + (" " + " ".join(f"[{p}]" for p in paras) if paras else "")

    tags = " ".join(f"#{t.replace(' ', '-')}" for t in data.get("tags", [])[:6]) or "(无)"
    return NOTE_TEMPLATE.format(
        doi=meta.doi, concepts=concepts, title=meta.title or "(无标题)",
        one_liner=data.get("one_liner", ""),
        background=six("background"), method=six("method"),
        result=six("result"), conclusion=six("conclusion"),
        innovation=six("innovation"), limitation=six("limitation"),
        tags=tags)


def _render_wiki(meta, data: dict) -> str:
    return (f"---\ntype: paper-wiki\ndoi: {meta.doi}\n---\n\n"
            f"# 深度编译：{meta.title}\n\n"
            f"{data.get('wiki', '')}\n")


def _prompt_l3_keywords(meta: dict, self_ctx: str) -> str:
    """L3 第一步 prompt：让 LLM 从编译结果提取检索关键词。

    输出：5-8 个关键词/短语，用于向量检索相关文献。
    """
    title = meta.get("title", "")
    return (
        "你是科研文献检索专家。根据下方文献的编译结果，"
        "提取 5-8 个最适合用于检索相关文献的关键词或短语。\n\n"
        "要求：\n"
        "- 包含核心概念、方法、应用场景\n"
        "- 中英文混合（该领域通用术语用英文，特定概念用中文）\n"
        "- 避免过于宽泛的词（如\u201c深度学习\u201d\u201c神经网络\u201d）\n"
        "- 优先选择能区分研究方向的精确术语\n\n"
        f"## 文献：{title}\n"
        f"DOI: {meta.get('doi', '')}\n\n"
        f"{self_ctx}\n\n"
        "直接输出关键词，每行一个，或用 JSON 数组格式。\n"
    )


def _prompt_l3(meta: dict, self_ctx: str,
               related_ctxs: list[dict]) -> str:
    """L3 概念关系层 prompt：用编译结果（L1+L2）代替全文，分析跨文献概念关系。

    输入：本文编译结果 + 相关文献编译结果（~20K tokens，省 77% vs 全文）。
    输出：概念关系图 + 研究簇摘要 + 方法论连接。
    """
    related_blocks = []
    for i, r in enumerate(related_ctxs, 1):
        block = (f"### 相关文献 {i}（{r['doi']}，"
                 f"connection={r.get('connection', 'unknown')}）\n"
                 f"{r['context']}")
        related_blocks.append(block)
    related_text = "\n\n".join(related_blocks)
    cite_map = "、".join(f"{i}={r['doi']}" for i, r in enumerate(related_ctxs, 1))

    task = (
        "你是科研知识关系分析专家。基于下方本文及多篇相关文献的编译结果，"
        "分析它们之间的概念关系、方法论连接和研究演进脉络。\n\n"
        + _NO_LATEX_RULE + "\n"
        "## 引用约定（硬性要求）\n"
        f"相关文献编号与 DOI 对照：{cite_map}\n"
        "正文中每次提及相关文献，必须写成「文献N（DOI）」形式，"
        "例如：文献2（10.1016/j.snb.2022.132616）；不得只写「文献N」。\n\n"
        "## 本文编译结果\n"
        f"{self_ctx}\n\n"
        "## 相关文献编译结果\n"
        f"{related_text}\n\n"
        "## 任务\n"
        "输出严格 JSON：\n"
        '{"summary": "研究簇整体概述（100-200字）",\n'
        ' "concept_map": [\n'
        '   {"concept": "概念名", "papers": ["doi1", "doi2"],\n'
        '    "evolution": "概念在这些文献中的演进关系"}\n'
        ' ],\n'
        ' "methodology_connections": [\n'
        '   {"from": "doi1", "to": "doi2",\n'
        '    "relation": "方法继承/改进/对比/互补"}\n'
        ' ],\n'
        ' "research_trajectory": "该研究方向的演进趋势（50-100字）"}\n'
        "不要输出 JSON 以外的任何内容。\n"
    )
    return task


def _render_relations(meta, data: dict, related_ctxs: list[dict]) -> str:
    """渲染 `_relations.md`（L3 产物）。"""
    from .doi import doi_to_dirname

    self_dir = doi_to_dirname(meta.doi)

    def _ref(p: str) -> str:
        """引用项 → Obsidian wikilink 目标（**目录名**：DOI 的 "/" 已转为 "_"）。

        兼容模型写法：裸 DOI / 目录名 / 「文献N（DOI）」/「本文」（指自身）。
        必须输出目录名——`[[10.1016/j.snb.../_note]]` 在 Obsidian 是死链。
        """
        s = (p or "").strip()
        m = re.search(r"(10\.\d{4,9}/[^\s（）()]+)", s)
        if m:
            return doi_to_dirname(m.group(1))
        if s.startswith("本文"):
            return self_dir
        return doi_to_dirname(s)

    lines = [
        f"---\ntype: paper-relations\ndoi: {meta.doi}\n---\n",
        f"# 概念关系分析：{meta.title}\n",
        f"## 研究簇概述\n{data.get('summary', '')}\n",
    ]

    concept_map = data.get("concept_map", [])
    if concept_map:
        lines.append("## 概念关系图\n")
        for cm in concept_map:
            concept = cm.get("concept", "")
            papers = cm.get("papers", [])
            evolution = cm.get("evolution", "")
            paper_links = ", ".join(f"[[{_ref(p)}/_note]]" for p in papers[:5])
            lines.append(f"### {concept}\n"
                         f"- 相关文献：{paper_links}\n"
                         f"- 演进：{evolution}\n")

    method_conns = data.get("methodology_connections", [])
    if method_conns:
        lines.append("## 方法论连接\n")
        for mc in method_conns:
            lines.append(f"- [[{_ref(mc.get('from', ''))}/_note]] → "
                         f"[[{_ref(mc.get('to', ''))}/_note]]："
                         f"{mc.get('relation', '')}")

    trajectory = data.get("research_trajectory", "")
    if trajectory:
        lines.append(f"\n## 研究趋势\n{trajectory}\n")

    lines.append("\n## 相关文献\n")
    for i, r in enumerate(related_ctxs, 1):
        # 编号与 _prompt_l3 的「相关文献 N」一致；目标用目录名（DOI 的 "/" → "_"）
        lines.append(f"{i}. [[{doi_to_dirname(r['doi'])}/_note]]"
                     f"（{r.get('connection', '')}）")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 工具

def _parse_json(raw: str) -> dict:
    r"""容错解析 LLM JSON 输出（剥围栏 / 非法转义修复 / 推理前后缀 / 截断子对象回收）。

    2026-09-21：与 translate 共用 `paperkb._jsonutil.extract_json_object`。此前是裸
    ``json.loads`` + 贪婪 ``{.*}``，glm 等模型编译输出含 LaTeX 单反斜杠（``$\mathrm{…}$``
    的 ``\m`` 是非法 JSON 转义）或带思维链前后缀时整段解析失败 → 偶发
    "L1 编译输出无效（JSON 缺失 one_liner）" 判死、_note.md 不生成。
    expected_keys 覆盖 L1(one_liner)/L2(wiki)/L3(summary) 三级产物。
    """
    from ._jsonutil import extract_json_object
    return extract_json_object(raw, expected_keys=("one_liner", "wiki", "summary"))


def _salvage_l1(text: str) -> dict:
    """平衡括号抢救：从 malformed 输出里提取首个含 one_liner 的完整 JSON 对象（共用 _jsonutil）。

    glm 常把 L2 Markdown 塞进 JSON 字符串或尾部多吐杂文本 ⇒ 整段解析失败；但 one_liner
    对象本身往往括号完整，平衡扫描（含非法转义修复）可救回，避免整轮编译判死→worker
    整轮重试（烧全量 token）。
    """
    from ._jsonutil import balanced_extract
    if not text:
        return {}
    start = text.find("{")
    while 0 <= start < len(text):
        obj = balanced_extract(text, start)
        if isinstance(obj, dict) and obj.get("one_liner"):
            return obj
        start = text.find("{", start + 1)
    return {}


def _canon_concept(name: str) -> str:
    """概念名归一化：去括号限定语 → 去标点/连字符 → 折叠空白 → 小写。

    让 LLM 每篇的措辞变体聚合到同一键，例如
    "n-p Junction (LIG/CoP_x)" / "n-p junction (heterointerface)" → "n p junction"，
    "back-relaxation" / "back relaxation" → "back relaxation"。
    保留中英文字、数字、空格与斜杠；连字符及其余标点一律转为空格。
    """
    n = (name or "").strip()
    n = re.sub(r"\([^)]*\)", " ", n)        # 去括号限定语
    n = re.sub(r"[^\w\s/]", " ", n)          # 去标点与连字符（保留 空格 与 /）
    n = re.sub(r"\s+", " ", n).strip().lower()
    return n


# cross_refs 的段标题（写入相关文献 _wiki.md 的"唯一"段落）
_XREF_HEADING = re.compile(r"^##[ \t]*相关文献[ \t]*$")


def _merge_cross_ref(text: str, line: str) -> str:
    """把一条 cross_ref 合并进**唯一**的「## 相关文献」段（去重 + 合并历史重复段）。

    返回新文本（不落盘）。历史数据里同一文件可能有多个 `## 相关文献` 段（旧实现
    每次追加一个新段）——这里一并合并到文末的单一段落，条目按出现顺序去重保序。
    """
    lines = (text or "").rstrip("\n").split("\n")
    items = [line]
    out: list[str] = []
    seen = False
    i = 0
    while i < len(lines):
        if _XREF_HEADING.match(lines[i]):
            j = i + 1
            while j < len(lines) and not lines[j].startswith("#"):
                t = lines[j].strip()
                if t.startswith("- ") and t not in items:
                    items.append(t)
                j += 1
            if out and out[-1].strip():
                out.append("")          # 保住"段落 / 下一个标题"之间的空行
            seen = True
            i = j
            continue
        out.append(lines[i])
        i += 1
    while out and not out[-1].strip():
        out.pop()
    out += ["", "## 相关文献", *items]
    return "\n".join(out).rstrip("\n") + "\n"


def _ctx_from_l1(data: dict) -> str:
    """L1 压缩版（≤1000 字）：one_liner + 六维一行；按边界收尾（不切进句子）。"""
    lines = [data.get("one_liner", "")]
    for k in ("background", "method", "result", "conclusion", "innovation", "limitation"):
        item = data.get(k) or {}
        text = item.get("text") if isinstance(item, dict) else str(item)
        if text:
            lines.append(f"{k}: {str(text)[:80]}")
    return boundary_trim("\n".join(lines), _CTX_LIMIT)
