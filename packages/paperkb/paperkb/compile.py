# -*- coding: utf-8 -*-
"""编译系统（卡帕西 LLM Wiki 风格，KB-DESIGN v0.6 §6）。

- L1 知识编译：一次 LLM 调用输出 一句话贡献/六维(带段落引用)/概念标签 → _note.md
- L2 章节要点：注入 L1 压缩版（compile_ctx），只补充不重复 → _details.md
- L3 深度 wiki：注入 L1+L2 压缩版，输出 wiki 结构+概念列表 → _wiki.md + 概念页聚合
- 编译结果链：每级产出 300 字压缩版存 compile_ctx，下级注入 → 省 token
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

logger = logging.getLogger(__name__)

NOTE_TEMPLATE = """---
type: paper-note
doi: {doi}
tags: [paper{concepts}]
---

# 论文核心笔记：{title}

## 基本信息
- 作者：{authors}{corr_line}{aff_line}
- 期刊/年份：{journal} {year}（{journal_meta}）
- DOI：[{doi}](https://doi.org/{doi})
- 被引：{cited}{kw_line}

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
- 详细笔记：[[_details|章节要点]]（翻译/编译后补全）
- 深度编译：[[_wiki|深度编译]]（高价值文献）
"""

# L1 压缩版字数（下级注入省 token）
_CTX_LIMIT = 300


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
        # 2026-09-11（用户模型）：**编译是内容进入知识库的唯一入口**——
        # PDF 解析后文献只留在 library，用户选择编译时才把原文层同步进 kb。
        # 这样编译读到的永远是最新正文（旧行为在解析后立刻复制一份冻结副本，
        # 翻译后再编译会读到翻译前的旧副本，cej 曾因此少 87 段译文）。
        # 幂等：kb 已有 document.json 则跳过（不覆盖，保持 D10 冻结原则）。
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
        """按价值分批量入队：L3(≥4.0 或 ⭐)/L2(≥2.5)/L1(其余，全做)。"""
        n = {"L1": 0, "L2": 0, "L3": 0}
        for s in scores:
            level = s.get("level") or "L1"
            try:
                self.queue(s["doi"], level, value_score=s.get("score", 0))
                n[level] += 1
            except CompileError:
                continue
        return n

    # ---------------------------------------------------------- L1
    def _compile_l1(self, doi: str, force: bool) -> dict:
        """L1 编译 = **一次请求同时产出 L1 笔记与 L2 详细笔记**（2026-09-16 用户决策）。

        为什么合并（对所有供应商一致，不再按 provider 分叉）：
          · 两级共用同一段共享全文前缀（~19k token）⇒ 合并后每篇少发一次全文前缀；
          · L2 不再依赖 worker 的"编完 L1 再看价值分入队 L2"升级链 ⇒ 不会再出现"只出 L1"
            （2026-09-15 实测事故）；L2 队列项此后会命中幂等跳过（`_compile_l2` 开头）。
          · L3 保持原样：价值分 ≥4.0 时由升级链单独入队、单独一轮请求。
        失败语义：组合请求失败 → 只落 L1（保持与旧行为一致），L2 由后续队列项单独重试。

        2026-09-19 新增：L1+L2 编译时 AI 顺手评分（ai_value + topic_score），
        评分写入 papers_meta 后自动重算价值分，达标则自动入队 L3（零额外 API 成本）。
        """
        doc = self._doc(doi)
        meta = self._resolve_meta(doi, doc)
        journal_meta = self._journal_meta(meta.journal, meta.issn, meta.eissn)
        note = self._note_path(doi)
        details = self._details_path(doi)
        meta_json = meta.model_dump(mode="json")
        if note.exists() and not force:
            return {"status": "skipped_existing", "doi": doi, "level": "L1"}
        llm = get_llm()
        topics = self._get_preferred_topics()
        prompt = _prompt_l1_l2(meta_json, doc, journal_meta, topics=topics)
        raw = llm.complete(prompt, context="compile")
        # 两段式解析：L1 段 → JSON；分隔符之后 → L2 Markdown 纯文本。
        # ⚠️ 关键：**L1 解析失败也不能丢 L1**——若整段 JSON 不合法，退回 L1 单发（与旧行为等价），
        # 而不是抛错把这一轮编译判死（2026-09-15 实测：模型塞长 Markdown 进 JSON 时整条失败）。
        l1_raw, l2_md = _split_l1_l2(raw)
        try:
            data = _parse_json(l1_raw)
        except ValueError:
            logger.warning("L1+L2 合并输出解析失败 → 退回 L1 单发（L2 留给队列项）: %s", doi)
            raw = llm.complete(_prompt_l1(meta_json, doc, journal_meta), context="compile")
            data = _parse_json(raw)
            l2_md = ""
        if isinstance(data, dict):
            l2_md = l2_md or str(data.get(_L2_MD_KEY) or "").strip()
        if not isinstance(data, dict) or not data.get("one_liner"):
            raise CompileError("L1 编译输出无效（JSON 缺失 one_liner）")
        note.write_text(_render_note(meta, data, journal_meta), encoding="utf-8")
        self._save_ctx(doi, "L1", _ctx_from_l1(data))
        self._mark_done(doi, "L1")
        l2_md = (l2_md or "").strip()
        if l2_md:
            details.write_text(l2_md + "\n", encoding="utf-8")
            self._save_ctx(doi, "L2", l2_md[: _CTX_LIMIT])
            self._mark_done(doi, "L2")
            logger.info("编译合并完成: %s → _note.md + _details.md（一次请求）", doi)
        else:
            logger.warning("编译合并：本次输出缺 L2（%s）→ 留给 L2 队列项单独编译", doi)
        self._index_paper_notes(doi)
        # ---- AI 评分保存 + L3 自动升级（2026-09-19）----
        ai_value = data.get("ai_value")
        topic_score = data.get("topic_score")
        l3_queued = False
        if ai_value is not None or topic_score is not None:
            self._save_ai_scores_and_maybe_l3(
                doi, ai_value, topic_score if topics else None)
            l3_queued = True
        return {"status": "done", "doi": doi, "level": "L1",
                "l2_written": bool(l2_md),
                "concepts": data.get("concepts", []),
                "ai_value": ai_value, "topic_score": topic_score,
                "l3_auto_queued": l3_queued}

    # ---------------------------------------------------------- L2
    def _compile_l2(self, doi: str, force: bool) -> dict:
        doc = self._doc(doi)
        meta = self._resolve_meta(doi, doc)
        details = self._details_path(doi)
        if details.exists() and not force:
            # 2026-09-12：已有产物**必须是有效产物**才算"跳过"。此前占位/拒绝文本
            # （如 763B 的"待补充：章节原文与段落 ID…"）也算存在 → **永久挡住重编**。
            try:
                existing = details.read_text(encoding="utf-8", errors="replace")
            except OSError:
                existing = ""
            if _l2_output_ok(existing):
                return {"status": "skipped_existing", "doi": doi, "level": "L2"}
            logger.info("L2 已有产物但判定无效（占位/拒绝文本）→ 重新编译: %s", doi)
        l1_ctx = self._ctx(doi, "L1")
        llm = get_llm()
        prompt = _prompt_l2(meta.model_dump(mode="json"), doc, l1_ctx)
        raw = llm.complete(prompt, context="compile")
        md = raw.strip()
        if len(md) < 20:
            raise CompileError("L2 输出过短")
        # 质量闸门（2026-09-12 用户反馈"L2 判定了但 _details.md 没有输出结果"）：
        # 拒绝文本/占位曾以 763B 通过长度闸门被 `_mark_done` 标记成功，UI 全程无 error。
        # 现在显式校验"像不像有效产物"，不合格 → CompileError → job=failed + last_error 可见。
        if not _l2_output_ok(md):
            refs = len(re.findall(r"\[P\d+\]", md))
            raise CompileError(
                f"L2 产出无效（段落引用仅 {refs} 处，疑似占位/拒绝文本；"
                f"开头：{md[:120]!r}）")
        details.write_text(md, encoding="utf-8")
        self._save_ctx(doi, "L2", md[: _CTX_LIMIT])
        self._mark_done(doi, "L2")
        self._index_paper_notes(doi)
        return {"status": "done", "doi": doi, "level": "L2"}

    # ---------------------------------------------------------- L3
    def _compile_l3(self, doi: str, force: bool) -> dict:
        doc = self._doc(doi)
        meta = self._resolve_meta(doi, doc)
        wiki = self._wiki_path(doi)
        if wiki.exists() and not force:
            return {"status": "skipped_existing", "doi": doi, "level": "L3"}
        l1_ctx = self._ctx(doi, "L1")
        l2_ctx = self._ctx(doi, "L2")
        llm = get_llm()
        prompt = _prompt_l3(meta.model_dump(mode="json"), doc,
                            l1_ctx, l2_ctx)
        raw = llm.complete(prompt, context="compile")
        data = _parse_json(raw)
        if not isinstance(data, dict):
            raise CompileError("L3 编译输出无效")
        wiki.write_text(_render_wiki(meta, data), encoding="utf-8")
        self._save_ctx(doi, "L3", (data.get("summary") or str(data))[: _CTX_LIMIT])
        self._mark_done(doi, "L3")
        self._index_paper_notes(doi)
        concepts = data.get("concepts") or []
        agg = self._aggregate_concepts(doi, concepts)
        return {"status": "done", "doi": doi, "level": "L3",
                "concepts": len(concepts), "concept_pages": agg}

    # ---------------------------------------------------------- 概念页（惰性聚合）
    def _aggregate_concepts(self, doi: str, concepts: list[dict]) -> int:
        """记录 L3 概念 → 同名概念 ≥3 文献时生成/更新 _concepts/<slug>.md。"""
        if not concepts:
            return 0
        created = 0
        for c in concepts:
            name = (c.get("name") or "").strip()
            if not name:
                continue
            slug = re.sub(r"[^\w\-]+", "-", name.lower()).strip("-")[:60]
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

    def _render_concept_page(self, name: str) -> str:
        rows = self.store.concept_rows(name)
        lines = [f"---\ntype: concept\ntags: [concept]\n---\n",
                 f"# 概念：{name}\n",
                 "## 定义（多文献聚合）\n"]
        for r in rows:
            lines.append(f"- **[[{r['paper_doi']}/_note|{r['paper_doi']}]]**：{r['definition']}")
        return "\n".join(lines) + "\n"

    # ---------------------------------------------------------- 辅助
    def _index_paper_notes(self, doi: str) -> None:
        """该篇编译产物（_note/_details/_wiki）入 notes_fts（M4 检索主对象）。"""
        from .doi import doi_to_dirname

        folder = self._kb_folder(doi, create=True)
        files = []
        for name in ("_note.md", "_details.md", "_wiki.md"):
            p = folder / name
            if p.exists():
                files.append({"filename": name,
                              "content": p.read_text(encoding="utf-8", errors="replace")})
        if files:
            self.store.index_notes(doi, files)

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

    def _note_path(self, doi: str) -> Path:
        return self._kb_folder(doi) / "_note.md"

    def _details_path(self, doi: str) -> Path:
        return self._kb_folder(doi) / "_details.md"

    def _wiki_path(self, doi: str) -> Path:
        return self._kb_folder(doi) / "_wiki.md"

    def _artifact_path(self, doi: str, level: str) -> Path | None:
        """该等级的编译主产物单文件（L1=`_note.md` / L2=`_details.md` / L3=`_wiki.md`）。

        三级都各有唯一主产物（`_compile_l1/_compile_l2/_compile_l3` 的写盘目标即上列
        三文件），故一律按**产物文件**判定，不用"kb 目录是否存在"兜底：kb 目录可能
        只因 `_ensure_source` 纳入原文层而存在（不等于编译过），拿它当判据会误判。
        """
        lv = (level or "").strip().upper()
        if lv == "L1":
            return self._note_path(doi)
        if lv == "L2":
            return self._details_path(doi)
        if lv == "L3":
            return self._wiki_path(doi)
        return None  # 未知等级：无产物定义

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

    def _get_preferred_topics(self) -> list[str]:
        """从全局设置读取用户配置的主题表（导入界面设置）。"""
        try:
            from . import _settings
            return list(getattr(_settings, "preferred_topics", None) or [])
        except Exception:  # noqa: BLE001
            return []

    def _save_ai_scores_and_maybe_l3(self, doi: str,
                                      ai_value: float | None,
                                      topic_score: float | None) -> None:
        """保存 AI 评分到 papers_meta，重算价值分，达标则自动入队 L3。

        设计思路（2026-09-19 用户决策）：
        - L1+L2 编译时 AI 已读全文，顺手打分零额外成本
        - 客观分(IF+被引+年份)决定初始 L1/L2 入队
        - L1+L2 完成后加入 AI 评分，重算总分
        - 总分 ≥ L3_THRESHOLD → 自动入队 L3（L3 复用缓存的全文，缓存命中率高）
        - L1+L2 都没过的文献不值得打附加分，也就不进 L3
        """
        try:
            self.store.update_ai_scores(doi, ai_value_score=ai_value,
                                        topic_score=topic_score)
            from .api import value_score_for
            from .score import L3_THRESHOLD
            new_score = value_score_for(doi)
            if new_score and new_score.get("score", 0) >= L3_THRESHOLD:
                l3_job = self.store.get_job(doi, "L3")
                if l3_job is None or l3_job.get("status") != "done":
                    self.queue(doi, "L3", value_score=new_score["score"])
                    logger.info("AI 评分触发 L3 自动升级: %s score=%.2f",
                                doi, new_score["score"])
        except Exception as e:  # noqa: BLE001
            logger.warning("AI 评分保存/L3 升级失败（不阻断编译）: doi=%s err=%s", doi, e)


# ---------------------------------------------------------------- 提示词

# L1 合并请求里承载 L2 详细笔记的 JSON 键（兼容旧形状，便于解析与测试）
_L2_MD_KEY = "l2_md"
# 两段式输出的分隔符（**独立一行**）：之前用"把 Markdown 塞进 JSON 字符串"的形状，
# 实测模型给不全导致整个 JSON 解析失败、连 L1 都丢（2026-09-15 真实链路）。
_L2_SEP = "<<<L2_MD>>>"


def _split_l1_l2(raw: str) -> tuple[str, str]:
    """把"两段式"输出拆成 (L1 段, L2 Markdown)。

    容忍：缺分隔符（则 L2 为空、由后续 L2 队列项单独编译）、围栏、分隔符前后多余空行。
    """
    text = raw or ""
    idx = text.find(_L2_SEP)
    if idx < 0:
        return text, ""
    return text[:idx], text[idx + len(_L2_SEP):].strip()


def _prompt_l1_l2(meta: dict, doc: PaperDoc, journal_meta: str,
                    topics: list[str] | None = None) -> str:
    """L1+L2 合并提示词：**一次请求**产出 L1 六维笔记（JSON）+ L2 详细笔记（Markdown）+ AI 评分。

    用户决策 2026-09-16：编译 L1/L2 合并，**对所有供应商一致生效**（不再按 provider 分叉）。
    为什么能省：两级共用同一段共享全文前缀（`shared_ctx(doc)`，~19k token）⇒ 每篇少发一次全文。

    2026-09-19 新增：AI 顺手评分（零额外成本）——JSON 增加 ai_value(0-5) 和 topic_score(0-1)。

    输出形状（严格 JSON，L2 正文放字符串里）：
        {"one_liner": "...", ..., "concepts": [...], "ai_value": 3.5, "topic_score": 0.8, "l2_md": "# 详细笔记\\n..."}
    拼装方式保持"与既有两个构造点同源"：L1 任务文本取自 `_prompt_l1`，L2 任务文本取自 `_prompt_l2`
    （在 `TASK_MARK` 处取任务部分），**不新造第二套任务描述**。
    """
    from .context import split_task

    l1_task = split_task(_prompt_l1(meta, doc, journal_meta))[1]
    l2_task = split_task(_prompt_l2(meta, doc, "(同一次调用内，请以上面 ① 的输出为准)"))[1]
    topic_instruction = ""
    if topics:
        topic_list = "、".join(topics)
        topic_instruction = (
            "\n\n### ③ 主题相关度评分\n"
            f"用户当前研究方向的主题表：{topic_list}\n"
            "请评估该论文与这些主题的相关度，输出 topic_score（0-1，0=完全无关，1=高度相关）。\n"
        )
    task = (
        "## 本次任务：论文知识编译（严格按顺序，三部分输出）\n"
        "先完成 ①，再**基于 ① 的输出**完成 ②（不要重复全景，只补充章节级细节），最后完成 ③。\n\n"
        "### ① L1 核心笔记\n" + l1_task + "\n\n"
        "### ② L2 详细笔记（Markdown）\n" + l2_task + "\n\n"
        "### ③ AI 价值评分\n"
        "基于你对论文全文阅读，评估其研究价值（0-5 分）：\n"
        "- 5分：开创性工作，方法/结论有重大突破\n"
        "- 4分：高质量研究，创新性强，实验充分\n"
        "- 3分：扎实研究，有一定创新，方法可靠\n"
        "- 2分：常规研究，创新性有限但方法正确\n"
        "- 1分：质量较低，方法或结论有明显缺陷\n"
        "- 0分：无学术价值\n"
        "输出 ai_value（0-5 的浮点数）。\n"
        + topic_instruction +
        "\n## 输出格式（**两段式，务必遵守**）\n"
        "第一段：①+③ 的 JSON 对象（含 one_liner / concepts / ai_value / topic_score 等全部字段），不要代码围栏。\n"
        f"然后单独一行输出分隔符：{_L2_SEP}\n"
        "分隔符之后：② 的 Markdown 正文，**直接写 Markdown，不要放进 JSON、不要转义换行**。\n"
        f"（分隔符必须是独立一行、内容就是 {_L2_SEP}；Markdown 正文直到结尾都算 ②。）"
    )
    return with_task(shared_ctx(doc), task)


def _prompt_l1(meta: dict, doc: PaperDoc, journal_meta: str) -> str:
    """L1 编译 prompt：**共享全文前缀**（header + 原文全文块）在前，任务指令在后。

    一次调用产出整个 _note.md：一行摘要(one_liner) + 六维(带 [Pxxx] 引用) + 概念/标签。
    全文块来自 paper_context（text_en 干净正文，跳过 References + 尾部杂项），不再依赖 text_zh。

    元数据块由 `frontmatter.meta_block` 统一渲染（2026-09-12 用户反馈修复）：
    此前只印 标题/期刊/年份/关键词/摘要 —— **缺作者与通信作者标注、缺研究单位、关键词恒空**
    （papers_meta 的 affiliations/keywords 从未被 DOI 补全写入）；现在作者带 `*` 标注 +
    研究单位逐条 + 关键词（papers_meta 优先，缺失时用 document.json/首页段落本地兜底）。
    """
    from .frontmatter import meta_block

    shared = shared_ctx(doc)
    task = (
        "你是科研知识编译助手。请依据上方论文全文，把它编译成结构化中文知识笔记。\n"
        "要求：六维每维必须引用论文段落 ID（如 [P001]）；输出严格 JSON：\n"
        '{"one_liner": "一句话贡献", "background": {"text": "...", "paras": ["P001"]}, '
        '"method": {...}, "result": {...}, "conclusion": {...}, "innovation": {...}, '
        '"limitation": {...}, "concepts": [{"name": "概念名(英文)", "definition": "定义"}], '
        '"tags": ["标签"]}\n'
        "不要输出 JSON 以外的任何内容。\n\n"
        + meta_block(meta, doc)
        + (f"\n期刊(权威)：{journal_meta}" if journal_meta else "")
    )
    return with_task(shared, task)


def _l2_sections(doc: PaperDoc) -> list[dict]:
    """L2 用的章节清单：优先 `doc.sections`，为空时**从段落现聚合**。

    2026-09-12 用户反馈（"判定 L2 但 `_details.md` 没有输出结果"）根因：
    P14 解析产出的 document.json **没有 `sections` 键**（实测 keys =
    schema_version/metadata/paragraphs/figures/tables/references/ai_summary/audit），
    而 `_prompt_l2` 的"章节片段"只从 `doc.sections` 生成 → 恒为"(无可用章节片段)"
    → LLM 只能回退成拒绝文本；763B 的占位又通过了 `len(md) < 20` 闸门被标记成功
    （日志实证：L2 那次输入仅 **372 token**，L1 是 23975）。
    这里在 sections 缺失时按段落 `section` 字段聚合（同一键名 {section,count}），
    让 PDF 解析链也能拿到章节片段。
    """
    secs = list(doc.sections or [])
    if secs:
        return secs
    counts: dict[str, int] = {}
    for p in doc.body_paras():
        name = (getattr(p, "section", "") or "").strip() or "(未分节)"
        counts[name] = counts.get(name, 0) + 1
    return [{"section": k, "count": v} for k, v in counts.items()]


# L2 无效产物的特征词（拒绝文本/占位）：命中即判不合格，触发失败而非静默 done。
_L2_BAD_MARKERS = ("无可用章节片段", "待补充", "未提供", "无法生成", "无法完成",
                   "仅有标题", "缺少章节")


def _l2_output_ok(md: str) -> bool:
    """L2 产物是否"像有效产物"：够长 + 至少 3 处段落引用 + 不含拒绝/占位特征词。"""
    text = (md or "").strip()
    if len(text) < 20:
        return False
    if any(m in text for m in _L2_BAD_MARKERS):
        return False
    return len(re.findall(r"\[P\d+\]", text)) >= 3


def _prompt_l2(meta: dict, doc: PaperDoc, l1_ctx: str) -> str:
    """L2 编译 prompt：**与 L1/L3 同一份共享全文前缀在前**，任务指令与摘要片段在后。

    2026-09-12 用户实测反馈（"L2 只送 1692 token，看不到全文，是否影响理解"）：
    旧实现不含 `shared_ctx`，L2 只能看到「L1 摘要压缩版 + 章节片段（≤10 章 ×5 段 ×400 字符）」
    ⇒ 实测仅 1692 token（全文 14834），章节级细节全靠 L1 摘要转述，
    且**无法继承 L1 已建立的提示词缓存**（两次调用各付一次未命中价）。
    加共享前缀后：① L2 能看到全文；② 与 L1 同前缀 ⇒ 第二轮几乎全命中缓存
    （缓存命中输入 $0.006/M vs 未命中 $0.3/M）。
    """
    sections = _l2_sections(doc)
    parts = []
    allowed = {id(p) for p in context_paragraphs(doc)}   # 与共享前缀同一口径（尾部杂项/References 已剔）
    for sec in (sections or [])[:10]:
        if sec.get("count", 0) > 60:
            continue
        texts = []
        for p in doc.body_paras():
            if _is_ref_section(p.section) or id(p) not in allowed:
                continue
            if p.section != sec.get("section"):
                continue
            t = (p.text_en or "").strip()[:400]  # 原文 text_en，不再读 text_zh
            if t:
                texts.append(f"[{p.para_id}] {t}")
        texts = texts[:5]
        if texts:
            parts.append(f"### {sec.get('section')}\n" + "\n".join(texts))
    sec_ctx = "\n\n".join(parts) or "(无可用章节片段)"
    task = (
        "你是科研笔记助手。以下是某篇论文的 L1 知识编译摘要（已有全景六维）。\n"
        "现在为每章提炼 2-4 条要点（中文，保留段落 ID 引用 [Pxxx]）。\n"
        "**已有内容不要重复**，只补充章节级细节。输出 Markdown：\n"
        "# 详细笔记：<标题>\n\n## 章节要点\n### <章节名>\n- 要点（[P001]）\n...\n\n"
        f"论文标题：{meta.get('title')}\n\n## L1 摘要（勿重复）\n{l1_ctx or '(无)'}\n\n"
        f"## 章节片段（原文 text_en）\n{sec_ctx}"
    )
    # ← 共享全文前缀（与 L1/L3/翻译/问答字节一致）
    return with_task(shared_ctx(doc), task)


def _prompt_l3(meta: dict, doc: PaperDoc, l1_ctx: str, l2_ctx: str) -> str:
    """L3 深度编译：共享全文前缀在前（与 L1/翻译同前缀，缓存友好），任务/摘要在后。"""
    from .frontmatter import meta_block

    shared = shared_ctx(doc)
    task = (
        "你是科研深度编译专家。基于论文产出深度知识卡（Markdown 结构 + JSON 概念列表）。\n"
        "输出严格 JSON：\n"
        '{"summary": "全文 200 字摘要", "wiki": "深度编译 Markdown：## 研究设计 / ## 关键方法 '
        '/ ## 核心结论 / ## 创新点 / ## 局限与批判性分析（方法局限、证据强度）/ ## 开放问题'
        '（正文每条引用段落 ID [P001]）", "concepts": [{"name": "概念名(英文)", "definition": "定义"}], '
        '"cross_refs": ["同主题相关文献建议"]}\n'
        "不要输出 JSON 以外的内容。\n\n"
        + meta_block(meta, doc, abstract_chars=3000)
        + f"\n\n## L1 摘要（已有，勿重复全景）\n{l1_ctx or '(无)'}\n"
        f"## L2 摘要（已有，勿重复）\n{l2_ctx or '(无)'}"
    )
    return with_task(shared, task)


# ---------------------------------------------------------------- 渲染

def _render_note(meta, data: dict, journal_meta: str) -> str:
    """渲染 `_note.md`（L1 产物）。

    2026-09-12（用户实测反馈"L1 基本信息缺关键词/通讯作者/研究机构"）：提示词侧的
    `frontmatter.meta_block` 早先已补齐这三项，但**产物模板/渲染没跟上**（且作者被硬截断到 6 位）
    ⇒ 现在从 `papers_meta` 同一份数据补齐：通信作者、研究单位、关键词，作者全量并给通信作者标 `*`。
    """
    authors = [a for a in (meta.authors or []) if a]
    corr = [c for c in (meta.corresponding or []) if c]

    def _is_corr(a: str) -> bool:
        return any(a == c or (c and (c in a or a in c)) for c in corr)

    a_line = ", ".join(a + ("*" if _is_corr(a) else "") for a in authors) or "(未知)"
    corr_line = f"\n- 通信作者：{'；'.join(corr)}" if corr else ""
    affils = [a for a in (meta.affiliations or []) if a]
    aff_line = f"\n- 研究单位：{'；'.join(affils)}" if affils else ""
    keywords = [k for k in (meta.keywords or []) if k]
    kw_line = f"\n- 关键词：{', '.join(keywords)}" if keywords else ""

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
        authors=a_line, corr_line=corr_line, aff_line=aff_line, kw_line=kw_line,
        journal=meta.journal or "",
        year=meta.year or "", journal_meta=journal_meta or "无指标",
        cited=meta.times_cited, one_liner=data.get("one_liner", ""),
        background=six("background"), method=six("method"),
        result=six("result"), conclusion=six("conclusion"),
        innovation=six("innovation"), limitation=six("limitation"),
        tags=tags)


def _render_wiki(meta, data: dict) -> str:
    return (f"---\ntype: paper-wiki\ndoi: {meta.doi}\n---\n\n"
            f"# 深度编译：{meta.title}\n\n"
            f"> {data.get('summary', '')}\n\n"
            f"{data.get('wiki', '')}\n\n"
            f"## 跨文献链接\n" +
            "\n".join(f"- {x}" for x in (data.get("cross_refs") or [])))


# ---------------------------------------------------------------- 工具

def _parse_json(raw: str) -> dict:
    """容错解析 LLM JSON 输出（剥围栏/提取首个 {...}）。"""
    if not raw:
        return {}
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                return {}
    return {}


def _ctx_from_l1(data: dict) -> str:
    """L1 压缩版（≤300 字）：one_liner + 六维一行。"""
    lines = [data.get("one_liner", "")]
    for k in ("background", "method", "result", "conclusion", "innovation", "limitation"):
        item = data.get(k) or {}
        text = item.get("text") if isinstance(item, dict) else str(item)
        if text:
            lines.append(f"{k}: {str(text)[:80]}")
    return "\n".join(lines)[:_CTX_LIMIT]
