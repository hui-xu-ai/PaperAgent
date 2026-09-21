# -*- coding: utf-8 -*-
"""★paperkb 唯一门面（facade）：backend/agent 只经本模块调用。

对外接口（M0+M1）：
    init_kb / bib_preview / bib_import / citations_for / missing_dois /
    wos_query / list_papers_meta / get_paper_meta / search_papers_meta /
    status
后续 M2-M5 追加：期刊表导入、价值评分、编译、检索问答、卡片（同一门面扩展）。

设计约束（KB-DESIGN v0.6 第 12 章）：
- 路径不硬编码：一切根路径经 Roots 注入
- 预览只回摘要（不 dump 全文，用户要求省 token）
- 元数据唯一权威 = bib（papers_meta）
"""
from __future__ import annotations

import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

from .config import KbSettings, Roots
from .db import KBStore
from .doi import (dir_to_key, dirname_to_doi, doi_to_dirname, extract_doi_from_text,
                  is_doi, is_md5_dir, make_rid, normalize_doi)
from .journals import JournalsDB
from .models import PaperMeta

logger = logging.getLogger(__name__)

# WOS 检索式单批 DOI 上限（避免检索式过长）
WOS_BATCH = 50

_store: KBStore | None = None
_journals: JournalsDB | None = None
_compiler: "Compiler | None" = None
_settings: KbSettings = KbSettings()


def init_kb(roots: Roots, settings: KbSettings | None = None) -> dict:
    """初始化：建目录 + 建表迁移。返回状态摘要。"""
    global _store, _settings, _journals, _compiler
    roots = roots.ensure()
    _settings = settings or KbSettings()
    _store = KBStore(roots)
    _store.init_schema()
    _journals = JournalsDB(roots)
    _journals.init_schema()
    _compiler = _build_compiler(roots)
    return status()


def configure_llm(client) -> None:
    """注入 LLM 客户端（编译/翻译用；backend 经 llm_service 适配）。"""
    from .llm import configure_llm as _cfg

    _cfg(client)


def _build_compiler(roots: Roots):
    from .compile import Compiler

    return Compiler(_store, _journals, roots)


def _need_store() -> KBStore:
    if _store is None:
        raise RuntimeError("paperkb 未初始化：请先调用 init_kb(roots)")
    return _store


def shared_doc_json(key: str) -> str:
    """[全局] **共享全文前缀的唯一取用入口**：kb 优先 → library 兜底。

    为什么要统一（2026-09-12 用户实测"首次提问缓存命中 0"）：
      - 编译/翻译读 `knowledge_base/<资源>/document.json`（= 编译请求**实际发送**的那份）；
      - 问答曾读 `papers.doc_json`（**library** 正本）；
      - 复核写回（`review_service` → `engine._post_parse_clean` → `sanitize_document`）
        会在编译之后重写 library 的 document.json ⇒ 两侧 `shared_ctx` 从**第 317 个
        字符**就分叉（公共前缀仅 ≈82 token）⇒ DeepSeek 前缀缓存按 64-token 块命中 ⇒
        14834 token 全部按未命中计费。
    三处（编译/翻译/问答）都走本函数后，共享全文前缀逐字节一致：
      · 未编译/未翻译的文献 → 回退 library（提问照样能带全文，符合用户模型）；
      · 已编译的文献 → 取 kb 快照 = 编译当时那份 ⇒ 问答继承编译建立的缓存。
    返回 "" 表示两侧都没有 document.json。
    """
    store = _need_store()
    roots = store.roots
    from .doc import find_document_in_kb
    from .resource import find_doc

    hit = find_document_in_kb(roots.kb_dir, key, store)
    if hit is None:
        hit = find_doc(roots.library_dir, key, store, search_root=roots.library_dir)
    return str(hit) if hit is not None else ""


def canonical_doc_json(key: str, *, prefer_kb: bool = True) -> str:
    """[全局] **定版（唯一权威文件）解析器**——编译/翻译/问答/复核**必须**都走它。

    2026-09-16 用户决策（方案 A）：`knowledge_base/` 是**唯一成品区**（定版），
    `library/` 只是中转仓库；因此：
      · 定版 = kb 的那份 `document.json`（若已纳入 kb）；
      · 尚未纳入 kb（解析完但未定版）→ 返回 **library** 那份（此时它就是要被纳入的源）；
    两类调用者（读上下文 + 写回产物）**用同一个返回值**，从而保证：
      ① 编译与翻译读到**同一份文件**（来源统一、共享前缀逐字节一致）；
      ② 译文/复核修改写回**同一份**（不再出现"复核改 library、编译读 kb"的分叉）。

    返回 "" 表示两侧都没有 document.json。
    """
    return shared_doc_json(key)


def translation_target(doc_json: str | Path) -> str:
    """翻译/复核的**写回目标 = 定版文件**（2026-09-16）。

    传入的 `doc_json` 若是 kb 或 library 下的 `<...>/<资源>/document.json`，取其目录名作 key
    走 `canonical_doc_json`；定位不到就**原样返回传入路径**（旧行为兜底，绝不因定位失败而丢写入）。
    """
    p = Path(doc_json)
    key = p.parent.name
    if not key:
        return str(p)
    try:
        hit = canonical_doc_json(key)
    except Exception as e:  # noqa: BLE001 - kb 未初始化等 → 退回传入路径
        logger.debug("定版写入目标定位失败（退回传入路径）: %s", e)
        return str(p)
    return hit or str(p)


def _dir_doi(store: KBStore, name: str) -> str:
    """目录名 → 真实 DOI（T6）：DOI 目录直接反推；md5 目录经 doi_md5_map 反查
    （map 命中返回 DOI；纯 md5 文献无 DOI 返回 ""）。"""
    key, kind = dir_to_key(name, store)
    if not key:
        return ""
    return "" if kind == "md5" and is_md5_dir(key) else key


# ---------------------------------------------------------------- bib 导入

def bib_preview(path: str | Path) -> dict:
    """解析 bib 文件 → 预览摘要（不 dump 全文）。

    返回：{records, valid, missing_doi, doi_dups, missing_fields, sample}
    """
    _need_store()  # 未初始化先报错（在解析前）
    from .bib_parser import parse_bib_file

    metas = parse_bib_file(path)
    store = _need_store()
    known = store.all_dois()
    dups, no_doi, missing_fields = [], [], []
    for m in metas:
        if not m.doi:
            no_doi.append(m.title[:60] or "(无标题无 DOI)")
            continue
        if m.doi in known:
            dups.append(m.doi)
        miss = [f for f in ("title", "abstract", "journal", "year", "authors")
                if not getattr(m, f)]
        if miss:
            missing_fields.append({"doi": m.doi, "missing": miss})
    return {
        "records": len(metas),
        "valid": sum(1 for m in metas if m.doi),
        "no_doi": no_doi,
        "doi_dups": dups,
        "missing_fields": missing_fields,
        "sample": [{"doi": m.doi, "title": m.title[:80],
                    "journal": m.journal, "year": m.year,
                    "refs": len(m.references)} for m in metas[:10]],
    }


def bib_import(path: str | Path) -> dict:
    """确认导入：papers_meta upsert + 引用边重建 + FTS 更新。幂等（重导=更新）。"""
    from .bib_parser import parse_bib_file

    metas = parse_bib_file(path)
    store = _need_store()
    imported, updated, citations, skipped = 0, 0, 0, 0
    for m in metas:
        if not m.doi:
            skipped += 1
            continue
        existed = store.has_meta(m.doi)
        store.upsert_meta(m)
        store.replace_citations(m.doi, m.references)
        citations += len(m.references)
        if existed:
            updated += 1
        else:
            imported += 1
    return {"imported": imported, "updated": updated,
            "citations": citations, "skipped_no_doi": skipped,
            "total": len(metas)}


# ---------------------------------------------------------------- 引用

def citations_for(doi: str) -> dict:
    """引用关系：cited（它引用的）/ citing（引用它的）。"""
    store = _need_store()
    return store.citations_for(normalize_doi(doi))


# ---------------------------------------------------------------- WOS 检索式

def missing_dois(roots: Roots | None = None) -> list[str]:
    """收集内容源中缺 papers_meta 的 DOI（library + kb + 用户提供的文献/ 文件名）。"""
    store = _need_store()
    known = store.all_dois()
    found: set[str] = set()
    roots = roots or _store.roots if _store else None
    if roots is None:
        return []
    # 1) document.json 的 metadata.doi（library 与 kb 各扫一遍，去重）
    for base in (roots.library_dir, roots.kb_dir):
        if not base.exists():
            continue
        for doc_json in base.rglob("document.json"):
            try:
                import json
                data = json.loads(doc_json.read_text(encoding="utf-8", errors="replace"))
                d = (data.get("metadata") or {}).get("doi") or ""
                if d:
                    found.add(normalize_doi(d))
            except (OSError, ValueError):
                continue
    # 2) 目录名反推（library/<dir>/ 与 kb/<dir>/ 首层目录 = doi_to_dirname；T6：md5 目录经 map 反查 DOI）
    for base in (roots.library_dir, roots.kb_dir):
        if not base.exists():
            continue
        for d in base.iterdir():
            if d.is_dir() and not d.name.startswith("."):
                doi = _dir_doi(store, d.name)
                if doi:
                    found.add(doi)
    # 3) 用户提供的文献/ 文件名（PDF/md/HTML 前缀 DOI，含下划线形态）
    src_dir = roots.library_dir.parent / "用户提供的文献"
    if src_dir.exists():
        for f in src_dir.rglob("*"):
            if f.is_file() and f.suffix.lower() in (".pdf", ".md", ".html"):
                stem = f.stem
                doi = extract_doi_from_text(stem)
                if not doi and re.match(r"10\.\d{4,9}_", stem):
                    doi = dirname_to_doi(stem)   # 下划线形态：10.xxxx_yyy → 10.xxxx/yyy
                if doi:
                    found.add(doi)
    missing = sorted(doi for doi in found if doi and doi not in known)
    return missing


def wos_query(dois: list[str], batch: int = WOS_BATCH) -> list[str]:
    """DOI 列表 → WOS 检索式列表（DO=(d1 OR d2 ...)，>batch 分批）。"""
    clean = sorted({normalize_doi(d) for d in dois if normalize_doi(d)})
    out: list[str] = []
    for i in range(0, len(clean), batch):
        chunk = clean[i:i + batch]
        out.append("DO=(" + " OR ".join(chunk) + ")")
    return out


# ---------------------------------------------------------------- 查询

def list_papers_meta(limit: int = 500) -> list[dict]:
    store = _need_store()
    return [m.model_dump(mode="json") for m in store.list_meta(limit)]


def get_paper_meta(doi: str) -> dict | None:
    store = _need_store()
    m = store.get_meta(normalize_doi(doi))
    return m.model_dump(mode="json") if m else None


def search_papers_meta(query: str, limit: int = 20) -> list[dict]:
    store = _need_store()
    return [m.model_dump(mode="json") for m in store.search_meta(query, limit)]


# ---------------------------------------------------------------- 期刊指标（M2）

def journals_preview(path: str | Path) -> dict:
    """JCR xlsx 预览（只回摘要：sheet/年份/行数/样例 2 行，不 dump 全文）。"""
    from .xlsx_import import parse_xlsx

    return parse_xlsx(path)


def journals_import(path: str | Path) -> dict:
    """确认导入 JCR xlsx → journals.db（jcr+cas 两表 upsert）。"""
    _need_store()
    if _journals is None:
        raise RuntimeError("journals 未初始化")
    from .xlsx_import import import_xlsx

    return import_xlsx(path, _journals)


def journals_lookup(journal_name: str, year: int | None = None) -> dict | None:
    """按期刊名查最新/指定年份指标（jcr+cas）。"""
    _need_store()
    if _journals is None:
        return None
    return _journals.lookup(journal_name, year)


def journals_lookup_issn(issn: str, eissn: str = "") -> dict | None:
    """按 ISSN/eISSN 查（bib 文献的精确匹配首选）。"""
    _need_store()
    if _journals is None:
        return None
    return _journals.lookup_issn(issn, eissn)


# ---------------------------------------------------------------- 变体头部（frontmatter）

def variant_frontmatter(key: str, doc_meta: dict | None = None,
                        tags: list[str] | None = None) -> str:
    """`zh.md` / `en_zh.md` 头部 YAML frontmatter 的**唯一装配入口**。

    2026-09-16 用户要求：头部元数据按「作者、通讯作者、研究单位、年份、期刊、影响因子、
    JCR分区、中科院分区、DOI、被引、关键词」排列，并**删掉重复的 `> [!info] 文献信息`**。
    此前模板自己从 `doc.metadata`（document.json，**没有期刊/年份/被引/指标**）拼块，
    与 `_note.md`（走 papers_meta + journals.db）各说各话 ⇒ 本次统一到本函数。

    取值（字段级，与 L1 编译 `_resolve_meta` 同精神）：
      `papers_meta`（权威，键可为 RID / DOI / **DOI 目录名**）优先 → `document.json` 兜底；
      期刊指标（JIF / JCR / 中科院分区）经 `journals.db`（ISSN 优先 → 期刊名）。
    """
    from .headmeta import journal_info, render_frontmatter

    store = _need_store()
    dm = doc_meta or {}
    meta = store.get_meta(key)

    merged: dict = {
        "authors": dm.get("authors") or [],
        "journal": dm.get("journal") or "",
        "year": dm.get("year") or "",
        "doi": dm.get("doi") or "",
        "keywords": dm.get("keywords") or [],
    }
    if meta is not None:
        for field in ("authors", "journal", "year", "doi", "keywords",
                      "corresponding", "affiliations"):
            v = getattr(meta, field, None)
            if v not in (None, "", [], {}):
                merged[field] = v
        if isinstance(meta.times_cited, int):
            merged["times_cited"] = meta.times_cited

    year = str(merged.get("year") or "").strip()
    info = journal_info(_journals, str(merged.get("journal") or ""),
                        getattr(meta, "issn", "") if meta else "",
                        getattr(meta, "eissn", "") if meta else "",
                        year=int(year) if year.isdigit() else None)
    title = str(dm.get("title") or "") or (getattr(meta, "title", "") if meta else "")
    return render_frontmatter(merged, info=info, title=title, tags=tags,
                              source="pdf", created=str(dm.get("extraction_time") or ""))


def journals_stats() -> dict:
    _need_store()
    if _journals is None:
        return {}
    return _journals.stats()


# ---------------------------------------------------------------- 价值评分（M2）

def value_score_for(doi: str) -> dict | None:
    """单篇价值评分（IF 档来自 journals.db；AI 评分来自 papers_meta 编译产出）。"""
    store = _need_store()
    meta = store.get_meta(normalize_doi(doi))
    if meta is None:
        return None
    return _score_meta(meta)


def list_scores() -> list[dict]:
    """全部文献评分（含等级）——编译队列排序依据。"""
    store = _need_store()
    out = []
    for meta in store.list_meta(limit=2000):
        s = _score_meta(meta)
        s["doi"] = meta.doi
        s["title"] = meta.title
        s["journal"] = meta.journal
        s["year"] = meta.year
        out.append(s)
    out.sort(key=lambda x: -x["score"])
    return out


def _score_meta(meta: PaperMeta) -> dict:
    from .score import value_score

    journal_info = None
    if _journals is not None:
        # 匹配顺序：人工纠正 → ISSN/eISSN（精确）→ 期刊名（规范化）
        override = getattr(meta, "journal_override", "") or ""
        if override:
            journal_info = _journals.lookup(override)
        if journal_info is None:
            journal_info = _journals.lookup_issn(meta.issn, meta.eissn)
        if journal_info is None and meta.journal:
            journal_info = _journals.lookup(meta.journal)
    has_bib = bool(meta.source_file)
    return value_score(meta, journal_info=journal_info, has_bib=has_bib)


def set_journal_override(doi: str, journal_name: str) -> dict:
    """人工纠正期刊绑定（匹配失败时指定 journals.db 中的标准名）。"""
    store = _need_store()
    doi = normalize_doi(doi)
    meta = store.get_meta(doi)
    if meta is None:
        raise KeyError(f"未找到元数据: {doi}")
    meta.journal_override = journal_name.strip()
    store.upsert_meta(meta)
    return {"doi": doi, "journal_override": meta.journal_override,
            "score": _score_meta(meta)}


# ---------------------------------------------------------------- 编译（M3）

def _need_compiler():
    _need_store()
    if _compiler is None:
        raise RuntimeError("compiler 未初始化")
    return _compiler


def compile_now(doi: str, level: str = "L1", force: bool = False) -> dict:
    """立即编译（同步；LLM 调用可能较慢）。键可为 DOI 或 RID（无 DOI 文献）。"""
    return _need_compiler().compile(doi, level, force)


def rebuild_concept_pages() -> dict:
    """归一化重建概念表并重新生成 ≥3 文献的概念页（一次性回灌，幂等）。"""
    return _need_compiler().rebuild_concept_pages()


def ensure_paper_registered(doc_json: str, *, paper_id: int = 0, pdf_md5: str = "") -> str:
    """确保 document.json 对应的资源在 papers_meta 里有记录，返回资源键（RID）。

    **无 DOI 文献编译的前提**（P0-B step4）：编译链按 rid 取数（元数据/编译任务/
    产物目录），若 papers_meta 没有该资源行，就会退化成"只有标题的兜底元数据"，
    且每次调用都算不出稳定的键。规则：
    - 有 DOI → 返回 `doi-<dirname>`（既有语义不变，且顺带登记 DOI 别名）；
    - 无 DOI → 用**内容指纹**：`nd-<pdf_md5 前12>`（同一 PDF 重复解析天然同键）；
      paper_id 也写进元数据，便于目录反查。
    - 已登记（同键或同 paper_id）→ 不覆盖既有 bib 元数据（bib 权威），只返回键。
    """
    import json
    from pathlib import Path

    store = _need_store()
    data = json.loads(Path(doc_json).read_text(encoding="utf-8", errors="replace"))
    meta = data.get("metadata") or {}
    doi = normalize_doi(meta.get("doi") or "")
    if doi:
        return store.upsert_meta(PaperMeta(
            rid=make_rid("paper", doi=doi), doi=doi,
            title=str(meta.get("title") or ""), source_file=str(doc_json)[:200],
            paper_id=paper_id or None))
    md5 = (pdf_md5 or "").strip().lower()
    if not md5:
        src = str(meta.get("source_pdf") or "")
        if src:
            try:
                import hashlib

                md5 = hashlib.md5(Path(src).read_bytes()).hexdigest()
            except OSError:
                md5 = ""
    rid = make_rid("paper", fingerprint=md5) if md5 else ""
    if not rid:
        rid = make_rid("paper", fingerprint=str(meta.get("title") or doc_json))
    existing = store.get_meta(rid)
    if existing is not None:
        return rid
    store.upsert_meta(PaperMeta(
        rid=rid, doi="", title=str(meta.get("title") or ""),
        kind=str(meta.get("kind") or ""), source_file=str(doc_json)[:200],
        paper_id=paper_id or None))
    logger.info("无 DOI 文献登记为内容指纹资源: rid=%s title=%s", rid, meta.get("title"))
    return rid


def compile_queue(doi: str, level: str | None = None) -> dict:
    """入队（level 缺省按价值分自动判定）。"""
    c = _need_compiler()
    if level:
        return c.queue(doi, level)
    from .score import L2_THRESHOLD

    score = value_score_for(doi)
    lv = ("L2" if score and score.get("level") == "L2" else "L1")
    return c.queue(doi, lv, value_score=(score or {}).get("score", 0))


def compile_queue_all() -> dict:
    """按价值分批量入队（L1 全做，L2/L3 按分）。"""
    c = _need_compiler()
    scores = list_scores()
    return c.queue_all_by_value(scores)


def compile_process(limit: int = 1) -> list[dict]:
    """处理队列（worker：每次取最高优先级一项，循环 limit 次）。"""
    c = _need_compiler()
    out = []
    for _ in range(limit):
        r = c.process_next()
        if r is None:
            break
        out.append(r)
    if out:
        regenerate_index()  # P3：编译产出后刷新总索引
    return out


def compile_status(status: str | None = None) -> list[dict]:
    return _need_store().list_jobs(status)


# ---------------------------------------------------------------- 卡片（M3）

def card_write(doi: str, card_type: str, content: str, title: str = "",
               para_ids: list[str] | None = None,
               tags: list[str] | None = None) -> dict:
    from .cards import write_card

    _need_store()
    return write_card(_store.roots.kb_dir, doi, card_type, content,
                      title=title, para_ids=para_ids, tags=tags)


def card_list(doi: str | None = None, card_type: str | None = None) -> list[dict]:
    from .cards import list_cards

    _need_store()
    return list_cards(_store.roots.kb_dir, doi, card_type)


# ---------------------------------------------------------------- 问答回灌（E3 写回飞轮）

def _kb_paper_compiled(store: KBStore, doi: str) -> bool:
    """该文献是否已编译纳入知识库：kb/<DOI>/ 目录存在且含编译产物。

    判定标志：目录存在，且含任一纳库/编译产物（en.md / zh.md / document.json /
    _note.md / _wiki.md / _relations.md）——即非 bib-only 空壳，才允卡片写回。
    """
    if not doi:
        return False
    d = store.roots.kb_dir / doi_to_dirname(doi)
    if not d.is_dir():
        return False
    markers = ("en.md", "zh.md", "document.json",
               "_note.md", "_wiki.md", "_relations.md")
    return any((d / m).exists() for m in markers)


def save_qa(question: str, answer: str, doi: str = "",
            sources: list[str] | None = None,
            tags: list[str] | None = None,
            scope: str = "paper") -> dict:
    """保存问答为卡片（写回飞轮，用户一键）。

    scope（命名空间分隔）：
    - "paper"（默认）：绑定单篇文献，前置校验该文献已编译纳入知识库
      （kb/<DOI>/ 存在编译产物）才可保存；否则返回
      {"ok": False, "error": "文献未纳入知识库，无法保存问答"}（不抛异常）。
      写入 kb/<doi_to_dirname(doi)>/cards/qa-<slug>.md，notes_fts.doi=该篇真实 DOI。
    - "global"：全局知识库问答（不绑定单篇），不需要 _kb_paper_compiled 校验；
      写入 kb/_global/cards/qa-<slug>.md，notes_fts.doi="_global"（保留键）。

    与旧版差别：问答不再写入全局 knowledge_base/_qa/，而是写入卡片目录（card-qa）；
    frontmatter 由卡片模板预置 type/doi/scope/para_ids/tags/created，内容为
    "# Q: ..." + answer + "## 来源"。

    并索引进 notes_fts（文献 QA 的 doi=该篇真实 DOI；全局 QA 的 doi="_global"，
    按卡片相对路径追加）使后续问答可检索到（飞轮增值）；索引失败不阻断落盘
    （try/except warning）。
    成功返回 {"ok": True, "path", "file", "slug", "scope"}；ok=False 见上。
    """
    from .cards import write_card

    if scope not in ("paper", "global"):
        scope = "paper"
    store = _need_store()
    doi = normalize_doi(doi)
    is_global = scope == "global"
    if is_global:
        key = "_global"                      # 保留命名空间键（全局 QA，不绑定单篇）
    else:
        if not _kb_paper_compiled(store, doi):
            return {"ok": False, "error": "文献未纳入知识库，无法保存问答"}
        key = doi
    slug = re.sub(r"[^\w\-]+", "-", (question or "q")[:40]).strip("-")[:50] or "q"
    refs = "\n".join(f"- {s}" for s in (sources or []))
    content = f"# Q: {question}\n\n{answer}\n\n## 来源\n{refs or '(无)'}"
    meta = write_card(store.roots.kb_dir, key, "card-qa", content,
                      title=question, tags=tags, slug=slug, scope=scope)
    # 索引进 notes_fts（doi=该篇真实 DOI / "_global"；按卡片相对路径追加，多条 QA 共存）
    try:
        store.append_note(key, meta["path"], content)
    except Exception as e:  # noqa: BLE001 - 索引失败不影响落盘
        logger.warning("保存 _qa 索引进 notes_fts 失败: %s", e)
    rel = meta["path"].replace("\\", "/")
    return {"ok": True, "path": rel, "file": rel.rsplit("/", 1)[-1],
            "slug": meta["slug"], "scope": scope}


def card_read_for_compile(doi: str, types: list[str] | None = None,
                          limit_chars: int = 4000) -> str:
    from .cards import read_cards_for_compile

    _need_store()
    return read_cards_for_compile(_store.roots.kb_dir, doi, types, limit_chars)


# ---------------------------------------------------------------- 翻译（M3b）

def translate_paper(doc_json: str | Path, *, compact: bool = False) -> dict:
    """翻译 document.json（共享全文前缀 + 译文任务；写回 text_zh，供双语/中文版本）。

    **批4（2026-09-12 用户要求"编译与翻译的全文必须一样"）**：构造全文前缀的那份 document.json
    统一走 `shared_doc_json(key)`（**kb 快照优先 → library 兜底**）——与**编译**（`Compiler._doc`
    读 kb）和**问答**（`chat_service._paper_shared_ctx` 走 shared_doc_json）**同一来源**，
    三者前缀逐字节一致 ⇒ 提示词缓存互相继承（整篇一次请求 ≈17k token 前缀否则每次全价）。
    译文仍**写回传入的 `doc_json`**（翻译真相源在 library，P0-B）。

    compact=True：紧凑模式（小上下文翻译专用模型），段落内联、无共享前缀、更小分批。

    产物落盘，不进入用户后续提问上下文（问答检索只读编译产物，D20）。
    不做「独立 summary 步骤」（D16：六维总结合成到 L1 编译 _note.md，含在 _note 内）。
    """
    from .llm import get_llm
    from .translate import run_translate

    target = translation_target(doc_json)
    ctx_path = ""
    try:
        key = Path(target).parent.name
        if key:
            ctx_path = canonical_doc_json(key)
    except Exception as e:  # noqa: BLE001
        logger.warning("翻译上下文同源定位失败（退回传入路径）: %s", e)
        ctx_path = ""
    return run_translate(target, get_llm(), context_path=(ctx_path or target),
                         compact=compact)


# ---------------------------------------------------------------- 检索/问答（M4）

def recall(query: str, top_k: int = 8, include_fulltext: bool = False,
           rerank: bool | None = None) -> list[dict]:
    """多路召回（编译产物/向量/元数据/引用邻域/卡片）→ RRF 融合 → 可选重排。

    `rerank=None` 时按 KbSettings.rerank_enabled（应用侧默认开、库默认关）。
    """
    from .retrieve import recall as _recall

    store = _need_store()
    out = _recall(store, store.roots, query, top_k=top_k,
                  include_fulltext=include_fulltext, rerank=rerank)
    return _enrich_recall(store, out)


def _enrich_recall(store, items: list[dict]) -> list[dict]:
    """召回条目补 `rid`/`kind`/`attachments`：附件命中的 doi 列是**父资源键**
    （目录名，如 `10.1016_j.cej…`），直接当 DOI 引用会出错——这里补上真实 DOI，
    并标出附件归属（A6：回答里能说清"这是某篇的支撑信息"）。"""
    for it in items or []:
        key = (it.get("doi") or "").strip()
        if not key:
            continue
        it["rid"] = key
        doi = key[4:] if key.startswith("doi-") else ""
        if not doi and _looks_like_doi(key):
            doi = key
        if not doi:
            try:
                m = store.get_meta(key)
                doi = (m.doi or "") if m else ""
            except Exception:  # noqa: BLE001
                doi = ""
        if not doi:
            # 兜底：键是目录名（`10.1016_j.cej…`）→ 反推真实 DOI；不猜不合法形态。
            try:
                from .doi import dirname_to_doi

                cand = dirname_to_doi(key)
                if cand and is_doi(cand):
                    doi = cand
            except Exception:  # noqa: BLE001
                doi = ""
        if doi:
            it["doi"] = doi
        it["kind"] = _kind_of_rid(key, store) or "paper"
        f = it.get("file") or ""
        it["attachment"] = f.startswith("attachments/") or f in ("si", "review", "data")
    return items


def qa_context(query: str, top_k: int = 8, budget_chars: int = 8000,
               include_fulltext: bool = False) -> dict:
    """[问答注入] 一次调用拿到「召回条目 + 组装好的注入上下文」。

    为什么要有它（2026-09-21）：backend 问答路径此前自己拼片段
    （`f"[{doi}/{file}] {snippet}"` 再 `boundary_trim`），与 paperkb 的
    `build_context`（产物整份注入）是**两套口径**——同一批命中，一边给整份产物、
    一边只给片段（实测因此丢掉「研究结果」里的数值）。注入文本该由检索层决定：
    API 契约优先，前后端解耦（前端/后端都不需要知道"片段怎么拼"）。

    返回 `{"items", "context", "context_chars"}`；`items` 与 `recall` 同形
    （含 rid/kind/rrf/rerank_score，供 UI 溯源）。
    """
    from .retrieve import build_context, recall as _recall

    store = _need_store()
    items = _enrich_recall(store, _recall(
        store, store.roots, query, top_k=top_k,
        include_fulltext=include_fulltext))
    ctx = build_context(items, store, store.roots, budget_chars)
    return {"items": items, "context": ctx, "context_chars": len(ctx)}


def recall_paper(doi: str, query: str, top_k: int = 4) -> list[dict]:
    """单篇编译笔记召回（paper 会话 Q5 阶段2）：只取该 DOI 编译产物按相关性。"""
    from .retrieve import recall_paper as _rp

    store = _need_store()
    return _rp(store, store.roots, doi, query, top_k=top_k)


def paper_notes_all(doi: str, max_files: int = 2, max_chars: int = 4000) -> list[dict]:
    """该篇**全部已编译笔记**（不做问句匹配）。

    用于"单篇文献会话"兜底：目标文档已知，中文问句检索不到时也要把笔记给到模型
    （2026-09-12 用户反馈"针对该文献提问没有利用已上传的文献信息"）。
    """
    from .retrieve import paper_notes_all as _all

    store = _need_store()
    return _all(store, store.roots, doi, max_files=max_files, max_chars=max_chars)


def paper_compiled(doi: str) -> list[str]:
    """该篇已编译产物文件名（会话提示注入编译状态用）；无则空。"""
    from .retrieve import paper_compiled_files

    store = _need_store()
    return paper_compiled_files(store, doi)


def ask(query: str, top_k: int = 8, include_fulltext: bool = False) -> dict:
    """问答编排：召回 → 组装 ≤预算 → LLM 回答（引用 [[DOI]]）。"""
    from .retrieve import answer as _answer

    return _answer(query, top_k=top_k, include_fulltext=include_fulltext)


def rebuild_fts(fulltext: bool | None = None) -> dict:
    """扫 kb 全量重建 notes_fts（+fulltext_fts 可选）。幂等。"""
    store = _need_store()
    use_full = _settings.fulltext_fts if fulltext is None else fulltext
    return store.reindex_from_kb(fulltext=use_full)


# ---------------------------------------------------------------- M5 导入流程

def import_markdown(md_path: str | Path, queue_compile: bool = True) -> dict:
    """途径 B：markdown 导入（写 library/<DOI>/en.md + 简版 document.json →
    kb 原文层纳入 → 入编译队列 L1）。

    DOI 识别链：文件名/内容提取 → 标题匹配 papers_meta → 报错提示先导 bib。
    """
    store = _need_store()
    roots = store.roots
    p = Path(md_path)
    if not p.exists():
        raise FileNotFoundError(f"markdown 文件不存在: {p}")
    text = p.read_text(encoding="utf-8", errors="replace")

    from .imports import detect_doi, import_markdown as _imp, sync_source_to_kb

    doi = detect_doi(p, text)
    meta = None
    if not doi:
        from .imports import _md_title
        title = _md_title(text)
        meta = _find_meta_by_title(title) if title else None
        if meta is not None:
            doi = meta.doi
    if meta is None and doi:
        meta = store.get_meta(doi)
    if not doi:
        raise ValueError(
            "无法识别 DOI：文件名/内容无 DOI 且无标题匹配元数据（请先导入 bib）")
    r = _imp(p, roots, meta=meta.model_dump(mode="json") if meta else None, doi=doi)
    r["sync"] = sync_source_to_kb(doi, roots)          # 首次纳入（不覆盖已有 kb）
    if queue_compile:
        try:
            r["queued"] = compile_queue(doi, "L1")
        except Exception as e:  # noqa: BLE001
            r["queued"] = {"error": str(e)}
    return r


def _find_meta_by_title(title: str) -> PaperMeta | None:
    """标题（归一化）匹配 papers_meta——md 无 DOI 时的兜底识别。"""
    if not title:
        return None
    want = re.sub(r"\s+", " ", title.strip().lower())
    for m in _need_store().list_meta(limit=5000):
        t = re.sub(r"\s+", " ", (m.title or "").strip().lower())
        if t and (t == want or t.startswith(want) or want.startswith(t)):
            return m
    return None


def sync_source_to_kb(doi: str, force: bool = False) -> dict:
    """kb 原文层四件物理复制（force=False 冻结原则；force=True=手动重新同步）。

    P0-B step4：键可为 DOI / RID / 目录名 / md5 目录（无 DOI 文献同样可纳入）。
    """
    store = _need_store()
    from .imports import sync_source_to_kb as _sync

    r = _sync(doi, store.roots, force=force, store=store)
    regenerate_index()  # P3：纳入/同步后刷新总索引
    return r


def source_status(doi: str) -> dict:
    """library/kb 两侧原文层四件状态（键同上）。"""
    store = _need_store()
    from .imports import source_status as _st

    return _st(doi, store.roots, store=store)


def kb_status() -> list[dict]:
    """全部 kb/<DOI>/ 目录状态（前端同步总览）。"""
    store = _need_store()
    from .imports import kb_status as _kbs

    return _kbs(store.roots)


def verify_kb_doc(doi: str, base: str = "kb") -> dict:
    """en.md ↔ document.json 一致性校验（导入/重新同步时调用，防旧产物漂移）。"""
    store = _need_store()
    from .imports import verify_kb_doc as _verify

    return _verify(doi, store.roots, base=base)


def backfill_kb(force: bool = False) -> dict:
    """扫 library/ 全部文献，补齐 kb 原文层四件（source.pdf/en.md/document.json/images）。

    冻结原则：force=False 只复制缺失项（不覆盖已有 kb 文件）——历史文献一键补齐；
    force=True 全部覆盖（重新同步）。
    """
    store = _need_store()
    from .imports import sync_source_to_kb

    done, skipped, failed = [], [], []
    if store.roots.library_dir.exists():
        for d in sorted(store.roots.library_dir.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            key, kind = dir_to_key(d.name, store)
            if kind == "md5":
                continue  # T6：md5 目录（无 DOI 文献）sync 按 DOI 路径复制，暂不支持（只做索引识别）
            doi = key
            if not doi:
                continue
            try:
                r = sync_source_to_kb(doi, store.roots, force=force)
                (done if r["copied"] else skipped).append(
                    {"doi": doi, "copied": r["copied"], "skipped": r["skipped"],
                     "verify": (r.get("verify") or {}).get("ok")})
            except Exception as e:  # noqa: BLE001
                failed.append({"doi": doi, "error": str(e)})
    regenerate_index()  # P3：补全后刷新总索引
    return {"done": done, "skipped": skipped, "failed": failed,
            "count": len(done) + len(skipped) + len(failed)}


# ---------------------------------------------------------------- T6：目录名 ↔ DOI/md5 双映射

def set_doi_md5_map(key: str, doi: str = "", pdf_md5: str = "", paper_id: int = 0) -> None:
    """登记目录名 ↔ (DOI, pdf_md5) 映射（解析完成后由 backend 写入；md5 目录反查 DOI 用）。"""
    _need_store().set_doi_md5_map(key, doi=doi, pdf_md5=pdf_md5, paper_id=paper_id)


def get_doi_md5_map(key: str) -> dict | None:
    """按目录名查映射（md5 目录反查 DOI；未登记返回 None）。"""
    return _need_store().get_doi_md5_map(key)


def list_doi_md5_map() -> list[dict]:
    """全部目录名 ↔ DOI/md5 映射（调试/巡检用）。"""
    return _need_store().list_doi_md5_map()


# ---------------------------------------------------------------- 知识库聚合列表（A4 三视图）

# 聚合候选上限（本地 SQLite/FTS + 目录扫描，单次批量取数）
_KB_LIST_MAX = 10000
_KB_LEVELS = ("L1", "L2", "L3")

# P0-A（2026-09-11）：DOI 形态判据统一走 doi.is_doi（单一来源，避免两处正则不一致）。
# 非 DOI 目录名不得被当成文献身份，避免幽灵文献与串档。
_looks_like_doi = is_doi


def _kb_dir_title(dir_path) -> str:
    """磁盘 kb 目录的人类可读标题（供无 bib 元数据的目录显示）。

    优先 `_note.md` frontmatter 的 title，其次 `document.json` 的 metadata.title；
    都取不到返回 ""（调用方回退目录名）。只读小片段，避免大文件全量 IO。
    """
    import json as _json
    try:
        note = dir_path / "_note.md"
        if note.exists():
            head = note.read_text(encoding="utf-8", errors="replace")[:1200]
            m = re.search(r"^title:\s*(.+)$", head, re.M)
            if m:
                return m.group(1).strip().strip('"\'')
        doc = dir_path / "document.json"
        if doc.exists():
            data = _json.loads(doc.read_text(encoding="utf-8", errors="replace"))
            meta = (data or {}).get("metadata") or {}
            return str(meta.get("title") or "").strip()
    except Exception:  # noqa: BLE001 - 标题兜底失败不影响列表
        return ""
    return ""


def _levels_from_status(krow: dict) -> list[str]:
    """由 kb 目录状态推导已编译层级（磁盘为准）。"""
    out = []
    if krow.get("note"):
        out.append("L1")
    if krow.get("wiki"):
        out.append("L2")
    if krow.get("relations"):
        out.append("L3")
    return out


def kb_list(q: str = "", journal: str = "", compile_status: str = "",
            score_min: float = 0, sort: str = "value",
            page: int = 1, page_size: int = 50,
            kind: str = "", has_attachment: bool = False,
            on_disk_only: bool = True,
            quartile: str = "", year_from: int | None = None,
            year_to: int | None = None, min_if: float = 0.0) -> dict:
    """知识库三视图聚合列表：papers_meta × 价值分 × kb/编译状态。

    条目 = papers_meta（bib 权威元数据），doi 唯一键；评分/目录/编译三次批量
    取数后按 DOI 建 map（无逐条 N+1）。过滤（q/journal/compile_status/score_min）
    与排序在内存做，分页在排序后切片。

    参数：
        q              全文检索（store.search_meta FTS）
        journal        期刊名子串（大小写不敏感；匹配 journal 或人工纠正 journal_override）
        compile_status 过滤："done"=compiled 非空；"none"=compiled 空；L1/L2/L3=已编译该等级
        score_min      价值分下限（value_score >= score_min）
        sort           value 降序（默认）/ year 降序 / title 升序；未知值回落 value
        page / page_size  分页（page>=1；page_size 1..200，超出截断）
        on_disk_only   默认 True：过滤掉 kb 目录已删除的幽灵记录（文件不在磁盘）
    返回：
        {"items": [{doi,title,journal,year,value_score,level,in_kb,
                    source_files:{source_pdf,en_md,document_json,images},
                    compiled:[], queued:[], last_error:str}],
         "total": int, "page": int, "page_size": int}
    """
    store = _need_store()
    from .score import L3_THRESHOLD as _L3_TH

    # 1) 候选元数据（q 走 FTS；无 q 全量；一次查询）
    metas = (store.search_meta(q, limit=_KB_LIST_MAX) if q
             else store.list_meta(limit=_KB_LIST_MAX))

    # 2) 三次批量取数 → DOI map（避免逐条查询）
    scores = {s["doi"]: s for s in list_scores()}
    kb_rows: dict[str, dict] = {}
    for r in kb_status():
        key, _kind = dir_to_key(r["dir"], store)  # T6：md5 目录经 map 关联 DOI
        if key:
            kb_rows[key] = r
    jobs: dict[str, dict] = {}          # doi → {compiled:list, queued:list, error:str}
    # 注：形参 compile_status 遮蔽同名门面函数，这里直接走 store.list_jobs()（等价）
    for j in store.list_jobs():
        doi = j.get("paper_doi") or ""
        if not doi:
            continue
        agg = jobs.setdefault(doi, {"compiled": [], "queued": [], "error": "",
                                    "failed_level": ""})
        st, lv = (j.get("status") or ""), ((j.get("level") or "").upper())
        if st == "done" and lv in _KB_LEVELS and lv not in agg["compiled"]:
            agg["compiled"].append(lv)
        elif st in ("queued", "compiling", "pending") and lv in _KB_LEVELS \
                and lv not in agg["queued"]:
            agg["queued"].append(lv)
        if st == "failed" and (j.get("error") or "") and not agg["error"]:
            agg["error"] = j["error"]   # list_jobs 按 started_at DESC → 首个=最近失败
            agg["failed_level"] = lv

    # 3) 逐条组装 + 过滤
    jl = (journal or "").strip().lower()
    cs = (compile_status or "").strip().lower()
    items: list[dict] = []
    matched: set[str] = set()   # P0-A：已被 bib 元数据覆盖的 key（磁盘独有项只看剩下的）
    for m in metas:
        # P0-B：条目键 = DOI（有则用）否则 rid —— 无 DOI 资料（中文文献/书/学位论文）
        # 也能出现在列表里，且同一 rid 的两次写入不会互相覆盖。
        mkey = (m.doi or "").strip() or (m.rid or "")
        s = scores.get(mkey) or {}
        value = float(s.get("score") or 0.0)
        if value < score_min:
            continue
        ai_ok = bool(((s.get("parts") or {}).get("ai_value") or {}).get("available"))
        jname = m.journal or ""
        if jl and jl not in jname.lower() \
                and jl not in (m.journal_override or "").lower():
            continue
        agg = jobs.get(mkey) or {}
        compiled = [lv for lv in _KB_LEVELS if lv in agg.get("compiled", [])]
        queued = [lv for lv in _KB_LEVELS if lv in agg.get("queued", [])]
        has_error = bool(agg.get("error", ""))
        if cs == "done" and not compiled:
            continue
        if cs == "none" and compiled:
            continue
        if cs == "failed" and not has_error:
            continue
        if cs in ("l1", "l2", "l3") and cs.upper() not in compiled:
            continue
        # "可升级L3"筛选（方案A）：L2已编译、L3未编译、价值分>=阈值、AI评分可用
        if cs == "l2":
            if ("L2" not in compiled or "L3" in compiled
                    or value < _L3_TH or not ai_ok):
                continue
        m_quartile = getattr(m, "quartile", "") or ""
        if quartile and m_quartile not in [q.strip() for q in quartile.split(",")]:
            continue
        m_year = int(m.year) if m.year and str(m.year).isdigit() else 0
        if year_from and m_year < year_from:
            continue
        if year_to and m_year > year_to:
            continue
        m_if = float(getattr(m, "impact_factor", 0.0) or 0.0)
        if min_if and m_if < min_if:
            continue
        krow = kb_rows.get(mkey) or {}
        if krow:
            matched.add(mkey)
        # 2026-09-19：默认过滤 kb 目录已删除的幽灵记录（文件不在磁盘但数据库有记录）
        if on_disk_only and not krow:
            continue
        items.append({
            "doi": m.doi,
            "rid": m.rid or mkey,
            "dir": krow.get("dir", ""),
            "title": m.title or "",
            "journal": jname,
            "year": m.year or "",
            "value_score": value,
            "level": s.get("level") or "L1",
            "in_kb": bool(krow),
            "on_disk": bool(krow),      # P0-A：磁盘为准——False = 记录在但目录已被删
            "source": "meta",
            "source_files": {
                "source_pdf": bool(krow.get("source.pdf")),
                "en_md": bool(krow.get("en.md")),
                "document_json": bool(krow.get("document.json")),
                "images": bool(krow.get("images")),
            },
            "compiled": compiled,
            "queued": queued,
            "last_error": agg.get("error", ""),
            "last_failed_level": agg.get("failed_level", ""),
            "l3_eligible": ("L2" in compiled and "L3" not in compiled
                            and value >= _L3_TH and ai_ok),
            "impact_factor": getattr(m, "impact_factor", 0.0) or 0.0,
            "quartile": getattr(m, "quartile", "") or "",
            "paper_rank": getattr(m, "paper_rank", 0.0) or 0.0,
            "library_citations": getattr(m, "library_citations", 0) or 0,
        })

    # 3b) P0-A：磁盘有产物、bib 元数据里没有目录（磁盘为准，否则"有文献但列表看不到"）
    for key, krow in kb_rows.items():
        if key in matched:
            continue
        dirname = krow.get("dir", "")
        if not dirname:
            continue
        levels = _levels_from_status(krow)
        if cs == "done" and not levels:
            continue
        if cs == "none" and levels:
            continue
        if cs in ("l1", "l2", "l3") and cs.upper() not in levels:
            continue
        skey = scores.get(key) or {}
        value = float(skey.get("score") or 0.0)
        if value < score_min:
            continue
        ai_ok = bool(((skey.get("parts") or {}).get("ai_value") or {}).get("available"))
        # "可升级L3"筛选（方案A，与主分支同口径）：L2已编译、L3未编译、分>=阈值、AI评分可用
        if cs == "l2" and ("L2" not in levels or "L3" in levels
                           or value < _L3_TH or not ai_ok):
            continue
        agg = jobs.get(key) or {}
        if cs == "failed" and not agg.get("error", ""):
            continue
        items.append({
            "doi": key if _looks_like_doi(key) else "",
            "rid": key,
            "dir": dirname,
            "title": _kb_dir_title(store.roots.kb_dir / dirname) or dirname,
            "journal": "",
            "year": "",
            "value_score": value,
            "level": skey.get("level") or "L1",
            "in_kb": True,
            "on_disk": True,
            "source": "disk",
            "source_files": {
                "source_pdf": bool(krow.get("source.pdf")),
                "en_md": bool(krow.get("en.md")),
                "document_json": bool(krow.get("document.json")),
                "images": bool(krow.get("images")),
            },
            "compiled": levels,
            "queued": [],
            "last_error": agg.get("error", ""),
            "last_failed_level": agg.get("failed_level", ""),
            "l3_eligible": ("L2" in levels and "L3" not in levels
                            and value >= _L3_TH and ai_ok),
            "kind": _kind_of_dir(dirname, key, store),
            "impact_factor": 0.0,
            "quartile": "",
            "paper_rank": 0.0,
            "library_citations": 0,
        })

    # 3c) P0-B step3：类型（kind）+ 附件数 + 类型/附件过滤（F1/A2；服务端过滤）
    counts = _attachment_counts(store)
    for it in items:
        r = it.get("rid") or it.get("doi") or ""
        k = _kind_of_rid(r, store)
        n = _attachment_count_for(r, it.get("dir", ""), store, counts)
        it["kind"] = k
        it["attachments"] = n
        it["has_attachment"] = n > 0
    items = _filter_kind(items, kind, has_attachment)

    # 4) 排序（value 降序 / year 降序 / title 升序；未知回落 value）
    if sort not in ("value", "year", "title"):
        sort = "value"

    def _y(item: dict) -> int:
        try:
            return int(item["year"]) or 0
        except (TypeError, ValueError):
            return 0

    if sort == "title":
        items.sort(key=lambda x: (x["title"].lower(),))
    elif sort == "year":
        items.sort(key=lambda x: (-_y(x), x["title"].lower()))
    else:
        items.sort(key=lambda x: (-x["value_score"], -_y(x), x["title"].lower()))

    # 4.5) 回收站过滤（2026-09-12 用户需求：**回收站的文献不再在知识库显示**）。
    #      磁盘分支天然看不到（目录已移到 `.trash/`），但 `papers_meta` 分支仍会列出，
    #      故这里按回收站登记的资源键过滤一次。
    try:
        trashed = store.trash_keys()
        if trashed:
            items = [it for it in items
                     if (it.get("rid") or "") not in trashed
                     and (it.get("doi") or "") not in trashed]
    except Exception as e:  # noqa: BLE001 - 过滤失败按"无回收站"处理（不影响列表可用）
        logger.warning("回收站过滤失败: %s", e)

    # 5) 分页（排序后切片）
    page = max(1, page)
    page_size = min(200, max(1, page_size))
    start = (page - 1) * page_size
    return {"items": items[start:start + page_size],
            "total": len(items), "page": page, "page_size": page_size}


# ---------------------------------------------------------------- 类型与附件（P0-B step3）

def _kind_of_dir(dirname: str, rid: str = "", store=None) -> str:
    """目录名/键 → 资源类型（显式 kind 列优先；否则由 RID/目录名前缀推导）。"""
    from .layout import kind_from_rid

    keys = [k for k in (rid, dirname) if k]
    for k in keys:
        if k.startswith(("book__", "thesis__", "std__", "patent__", "chapter__",
                         "si__", "review__", "note__")):
            return kind_from_rid(k)
    if store is not None and dirname:
        try:
            m = store.get_meta(dirname) or store.get_meta(rid)
        except Exception:  # noqa: BLE001 - 元数据缺失按前缀推导
            m = None
        if m is not None:
            k = (getattr(m, "kind", "") or "").strip().lower()
            if k:
                return k
    return kind_from_rid(keys[0] if keys else "")


def _kind_of_rid(rid: str, store=None) -> str:
    """RID → 类型：papers_meta.kind 优先（显式），否则 RID 前缀推导。"""
    r = (rid or "").strip()
    if not r:
        return ""
    if store is not None:
        try:
            stored = store.get_kind_by_rid(r)
        except Exception:  # noqa: BLE001
            stored = ""
        if stored:
            return stored
    return _kind_of_dir(r, r)


def _attachment_counts(store) -> list[tuple[str, int]]:
    """扫一次磁盘统计各资源的附件数：[(资源目录名, 文件数)]。

    只扫两层（资源目录/attachments/<kind>），不递归 stat 每个文件——用于列表徽标。
    """
    out: list[tuple[str, int]] = []
    lib = Path(store.roots.library_dir)
    if not lib.is_dir():
        return out
    for d in lib.iterdir():
        if not d.is_dir() or d.name.startswith("."):
            continue
        base = d / "attachments"
        if not base.is_dir():
            continue
        n = 0
        for kind_dir in base.iterdir():
            if not kind_dir.is_dir():
                continue
            for f in kind_dir.iterdir():
                if f.is_file() and not f.name.startswith("."):
                    n += 1
        if n:
            out.append((d.name, n))
    return out


def _attachment_count_for(rid: str, dirname: str, store, counts) -> int:
    """该资源（RID 或键）的附件数：磁盘目录名优先，其次独立根 attachments/<RID>。"""
    from . import attachments as att

    want = {rid}
    for cand in att.library_candidates(store.roots, rid):
        want.add(cand.name)
    if dirname:
        want.add(dirname)
    for name, n in counts:
        if name in want:
            return n
    # 无父资源的独立根：attachments/<RID>/**
    try:
        root = Path(store.roots.library_dir).parent / "attachments"
        target = root / (rid or "nd-untitled")
        if target.is_dir():
            return sum(1 for f in target.rglob("*") if f.is_file())
    except OSError:
        pass
    return 0


def _filter_kind(items: list[dict], kind: str, has_attachment: bool) -> list[dict]:
    """按类型/附件过滤（F1/A2）。

    kind 语义：空或 `all` = 不过滤；`none` = 无编号资料（RID 前缀 `nd-`）；
    其余为具体类型（paper/thesis/book/chapter/patent/standard/report/note/…）。
    """
    k = (kind or "").strip().lower()
    out = items
    if k and k != "all":
        if k == "none":
            out = [it for it in out if _is_no_identifier(it.get("rid") or it.get("doi") or "")]
        else:
            out = [it for it in out if (it.get("kind") or "paper") == k]
    if has_attachment:
        out = [it for it in out if int(it.get("attachments") or 0) > 0]
    return out


def _is_no_identifier(rid: str) -> bool:
    """无编号资料判定（`nd-…` / 旧 md5 目录名）。"""
    r = (rid or "").strip()
    return r.startswith("nd-") or is_md5_dir(r)


def kind_options() -> dict:
    """类型清单 + 计数（F1 芯片；服务端算，前端不再全量过滤）。"""
    store = _need_store()
    counts: dict[str, int] = {}
    total = 0
    no_id = 0
    seen: set[str] = set()
    for m in store.list_meta(limit=_KB_LIST_MAX):
        rid = (m.rid or "").strip() or ("doi-" + (m.doi or ""))
        k = _kind_of_rid(rid, store) or "paper"
        counts[k] = counts.get(k, 0) + 1
        total += 1
        seen.add(rid)
        if _is_no_identifier(rid):
            no_id += 1
    # P0-A 磁盘为准：kb 目录独有（bib 元数据未导入）的资源也要进计数，
    # 否则芯片说"0 篇"而列表里却有东西（同一屏自相矛盾）。
    # ⚠️ 去重必须跨键形态：papers_meta.rid 是 `doi-10.1002_adma…`（RID），
    #    kb 目录名是 `10.1002_adma…`（doi_to_dirname 产物）——不归一化会同一篇算两次。
    seen_dirs = set()
    for rid0 in seen:
        if rid0.startswith("doi-"):
            seen_dirs.add(rid0[4:])
        if is_doi(rid0):
            seen_dirs.add(doi_to_dirname(rid0))
    for row in kb_status():
        key, _t = dir_to_key(row["dir"], store)
        if not key:
            continue
        doi_key = dirname_to_doi(row["dir"]) or ""
        if key in seen or row["dir"] in seen_dirs \
                or (doi_key and ("doi-" + doi_key) in seen):
            continue
        seen.add(key)
        k = _kind_of_dir(row["dir"], key, store) or "paper"
        counts[k] = counts.get(k, 0) + 1
        total += 1
        if _is_no_identifier(key):
            no_id += 1
    att_rows = _attachment_counts(store)
    att = len(att_rows)                       # 有附件的**资源数**（芯片点下去筛出的条数）
    att_files = sum(n for _name, n in att_rows)   # 附件文件总数（提示用）
    labels = {"paper": "论文", "thesis": "学位论文", "book": "书", "chapter": "章节",
              "patent": "专利", "standard": "标准", "report": "报告", "note": "笔记"}
    order = ["paper", "thesis", "book", "chapter", "patent", "standard", "report", "note"]
    chips = [{"kind": "all", "label": "全部", "count": total}]
    for k in order:
        if counts.get(k):
            chips.append({"kind": k, "label": labels.get(k, k), "count": counts[k]})
    for k, n in sorted(counts.items()):
        if k not in order:
            chips.append({"kind": k, "label": labels.get(k, k), "count": n})
    if no_id:
        chips.append({"kind": "none", "label": "无编号", "count": no_id})
    chips.append({"kind": "has_attachment", "label": "有附件", "count": att})
    return {"chips": chips, "counts": counts, "total": total,
            "attachment_files": att_files}


# ---------------------------------------------------------------- 依附资料门面（T2/T3）

def kb_attachments(rid: str) -> dict:
    """列某资源的附件清单（A3）：相对路径/大小/修改时间/是否已索引。"""
    from . import attachments as att

    store = _need_store()
    out = att.list_attachments(store.roots, rid)
    indexed = store.indexed_attachment_paths((rid or "").strip())
    for f in out.get("files", []):
        f["indexed"] = f"attachments/{f['path']}" in indexed
    out["kind"] = _kind_of_rid((rid or "").strip(), store)
    out["indexed_count"] = sum(1 for f in out.get("files", []) if f.get("indexed"))
    return out


def kb_attachment_import(rid: str, kind: str, filename: str, data: bytes,
                         index: bool = True) -> dict:
    """轻量导入一份附件（F5/A5）：落位 + 本地抽文本 + 索引进 notes_fts（0 API 成本）。"""
    from . import attachments as att

    store = _need_store()
    return att.import_attachment(store.roots, rid, kind, filename, data,
                                 store=store, index=index)


def kb_attachment_read(rid: str, rel_path: str, max_chars: int = 20000,
                       offset: int = 0) -> dict:
    """读附件文本片段（A4；**白名单路径校验**，只允许该资源 attachments 内的相对路径）。

    `offset` 支持长附件续读（配合返回的 `next_offset`）。
    """
    from . import attachments as att

    store = _need_store()
    return att.read_attachment(store.roots, rid, rel_path, max_chars=max_chars,
                               offset=offset)


def kb_attachment_path(rid: str, rel_path: str):
    """附件真实路径（下载/预览用；越权返回 None）。"""
    from . import attachments as att

    store = _need_store()
    return att.resolve_attachment(store.roots, rid, rel_path)


def kb_attachment_reindex() -> dict:
    """全量重扫附件文本入索引（也可由 reindex_from_kb 触发）。"""
    store = _need_store()
    return {"attachments_indexed": store.reindex_attachments()}


# ---------------------------------------------------------------- 磁盘↔数据库对账（P0-A）

def reconcile() -> dict:
    """磁盘 ↔ 数据库对账（P0-A「磁盘为准」）。

    用户会在资源管理器里手删/手拷文件，DB 只是派生索引——本函数列出两侧不一致项，
    供 UI 显示"发现 N 处不一致 → 一键处理"，避免"文件没了列表还在 / 记录没了目录还在"。

    返回：
        {
          "summary": {orphan_dir, ghost_row, disk_dirs, meta_rows, jobs},
          "orphan_dir": [{dir, key, title, compiled}],        # 盘有产物、bib 无元数据
          "ghost_row":  [{key, kind, title|level}],           # 库有记录、盘无目录
        }
    kind：meta_without_dir（bib 元数据无目录）/ job_without_dir（编译任务指向不存在目录）。
    """
    store = _need_store()
    disk = kb_status()
    disk_by_key: dict[str, str] = {}
    for row in disk:
        key, _kind = dir_to_key(row["dir"], store)
        if key:
            disk_by_key[key] = row["dir"]

    # P0-B：键 = DOI（有则用）否则 rid（无 DOI 资料不会互相覆盖）
    metas = {((m.doi or "").strip() or (m.rid or "")): m
             for m in store.list_meta(limit=_KB_LIST_MAX)}

    orphan_dir = []
    for row in disk:
        key, _kind = dir_to_key(row["dir"], store)
        if key and key in metas:
            continue
        orphan_dir.append({
            "dir": row["dir"],
            "key": key or row["dir"],
            "doi": key if _looks_like_doi(key or "") else "",
            "title": _kb_dir_title(store.roots.kb_dir / row["dir"]) or row["dir"],
            "compiled": _levels_from_status(row),
        })

    ghost_row: list[dict] = []
    seen: set[str] = set()
    for key, m in metas.items():
        if key in disk_by_key:
            continue
        seen.add(key)
        ghost_row.append({"key": key, "kind": "meta_without_dir",
                          "title": m.title or "", "year": m.year or ""})
    for j in store.list_jobs():
        doi = (j.get("paper_doi") or "").strip()
        if not doi or doi in disk_by_key or doi in seen:
            continue
        seen.add(doi)
        ghost_row.append({"key": doi, "kind": "job_without_dir",
                          "level": j.get("level") or "", "status": j.get("status") or ""})

    return {
        "summary": {
            "orphan_dir": len(orphan_dir),
            "ghost_row": len(ghost_row),
            "disk_dirs": len(disk),
            "meta_rows": len(metas),
        },
        "orphan_dir": orphan_dir,
        "ghost_row": ghost_row,
    }


# ---------------------------------------------------------------- 索引 / 补编（P3，卡帕西结构标准）

# ---------------------------------------------------------------- 回收站（2026-09-12 用户需求）
# "改成移除知识库即可，或者说是移除到回收站，编译产物这些都统一保留，只是不再会被知识库
#  检索；回收站这些文献在主界面添加一个恢复按钮；在回收站的文献不再知识库内显示。"

def _trash_title(store, key: str) -> str:
    """回收站条目的展示标题：优先元数据标题，其次目录名，最后键本身。"""
    try:
        m = store.get_meta(key)
        if m is not None and (m.title or "").strip():
            return m.title.strip()
    except Exception:  # noqa: BLE001 - 元数据缺失不算错误
        pass
    return key


def kb_trash_move(key: str, note: str = "") -> dict:
    """把一篇资源**移出知识库到回收站**（可逆）。

    - 磁盘产物**全部保留**：`knowledge_base/<目录>` 原样搬到 `knowledge_base/.trash/<目录>`
      （`.` 前缀目录本来就被所有知识库扫描排除，故显示上立刻消失）；
    - 退出检索：删该键在 `notes_fts`/`fulltext_fts` 的行（**含附件行**——整篇退出知识库；
      与 `index_notes` 的"只删非附件行"不同，那是防洗掉用户附件，这里是刻意整篇摘除）；
    - **不动 `papers_meta`**：元数据/价值分/关联保留，恢复后原样回来；
    - 登记 `kb_trash`（kb_list 据此过滤 meta 分支）。
    """
    store = _need_store()
    from .resource import resource_key, resources_dir

    k = resource_key(store, key) or (key or "").strip()
    if not k:
        raise ValueError("无效的资源键")
    if k in store.trash_keys():
        return {"status": "already_trashed", "key": k}

    title = _trash_title(store, k)
    kb_dir = resources_dir(store.roots.kb_dir, k, store)
    if kb_dir is None:
        # 磁盘上没有该目录（只有 meta 行 / 目录已被删）→ 仅登记"已移除"，不报错
        store.trash_add(k, dirname="", title=title, note=note or "no_dir")
        return {"status": "trashed_without_dir", "key": k, "title": title}

    dirname = kb_dir.name
    trash_root = store.roots.kb_dir / ".trash"
    trash_root.mkdir(parents=True, exist_ok=True)
    dest = trash_root / dirname
    if dest.exists():
        dest = trash_root / f"{dirname}__{datetime.now().strftime('%Y%m%d%H%M%S')}"
    shutil.move(str(kb_dir), str(dest))
    # 索引行的键是**裸 DOI**（dir_to_key 产物），而 kb_trash/papers_meta 用 **rid** ——
    # 三种写法都要覆盖，否则删不干净（实测踩到）。
    cands = {k, (key or "").strip(), dirname}
    try:
        from .doi import dirname_to_doi, is_doi

        for c in list(cands):
            if not c:
                continue
            back = dirname_to_doi(c)
            if back and is_doi(back):
                cands.add(back)
            if is_doi(c):
                cands.add(c)
    except Exception:  # noqa: BLE001 - 键归一化失败就按原样删
        pass
    removed = store.delete_index_for(sorted(x for x in cands if x))
    store.trash_add(k, dirname=dirname, title=title, note=note)
    logger.info("移出知识库到回收站: key=%s dir=%s → %s（索引 %s）", k, dirname, dest, removed)
    return {"status": "trashed", "key": k, "title": title,
            "dirname": dirname, "trash_dir": str(dest), **removed}


def kb_trash_restore(key: str) -> dict:
    """把回收站里的资源**恢复回知识库**（原样搬回 + 重建该篇索引与附件索引）。"""
    store = _need_store()
    from .resource import resource_key

    k = resource_key(store, key) or (key or "").strip()
    row = next((r for r in store.trash_list() if r["key"] == k), None)
    if row is None:
        raise ValueError(f"该资源不在回收站: {k}")

    dirname = (row.get("dirname") or "").strip()
    restored_to = ""
    if dirname:
        trash_root = store.roots.kb_dir / ".trash"
        dest = store.roots.kb_dir / dirname
        cands = sorted(trash_root.glob(dirname + "*")) if trash_root.is_dir() else []
        if dest.exists():
            # 设计口径（2026-09-19 用户确认）：知识库**已有**同名文献则不恢复——
            # 回收站并非为恢复而设，冲突时由用户自行到回收站文件夹手动挑选要覆盖的文件。
            trash_dir = str(cands[0]) if cands else str(trash_root / dirname)
            return {"status": "conflict", "key": k, "dirname": dirname,
                    "kb_dir": str(dest), "trash_dir": trash_dir,
                    "message": f"知识库已存在同名文献，未执行恢复。如需合并请手动选择要覆盖的文件：{trash_dir}"}
        if not cands:
            raise ValueError(f"回收站里找不到目录 {dirname}（可能被手工删除）")
        shutil.move(str(cands[0]), str(dest))
        restored_to = str(dest)
    store.trash_remove(k)
    # 重建索引（连附件一起）——`reindex_from_kb` 已跳过 `.trash/`
    reindexed = {}
    try:
        reindexed = store.reindex_from_kb()
    except Exception as e:  # noqa: BLE001 - 索引失败不影响恢复本身
        logger.warning("恢复后重建索引失败（可稍后手动重建）: %s", e)
    try:
        regenerate_index()
    except Exception as e:  # noqa: BLE001
        logger.warning("恢复后重建 _index.md 失败: %s", e)
    logger.info("从回收站恢复: key=%s → %s", k, restored_to or "(无目录)")
    return {"status": "restored", "key": k, "kb_dir": restored_to, "reindexed": reindexed}


def kb_trash_list() -> dict:
    """回收站清单（主界面「恢复」按钮的数据源）。"""
    store = _need_store()
    out = []
    for r in store.trash_list():
        k = r["key"]
        item = {"key": k, "dirname": r.get("dirname") or "",
                "title": r.get("title") or _trash_title(store, k),
                "trashed_at": r.get("trashed_at") or "", "note": r.get("note") or ""}
        try:
            m = store.get_meta(k)
            if m is not None:
                item["journal"] = m.journal or ""
                item["year"] = m.year or ""
                item["doi"] = m.doi or ""
        except Exception:  # noqa: BLE001
            pass
        out.append(item)
    return {"items": out, "total": len(out)}


def kb_trash_delete(key: str) -> dict:
    """彻底删除回收站里的**单篇**资源（不可恢复）：删 .trash 目录 + 移除 kb_trash 登记。

    2026-09-19（用户需求：回收站管理操作缺失）补全。**仅**作用于 knowledge_base/.trash/
    下该资源的目录，绝不触碰主知识库/library/papers_meta（文献库记录与解析产物保留）。
    """
    store = _need_store()
    from .resource import resource_key

    k = resource_key(store, key) or (key or "").strip()
    row = next((r for r in store.trash_list() if r["key"] == k), None)
    if row is None:
        raise ValueError(f"该资源不在回收站: {k}")
    dirname = (row.get("dirname") or "").strip()
    removed_dirs: list[str] = []
    if dirname:
        trash_root = store.roots.kb_dir / ".trash"
        if trash_root.is_dir():
            # 移入时可能加了 "__时间戳" 后缀（同名冲突），glob 前缀全覆盖
            for d in sorted(trash_root.glob(dirname + "*")):
                if d.is_dir():
                    shutil.rmtree(str(d), ignore_errors=True)
                    removed_dirs.append(d.name)
    store.trash_remove(k)
    logger.info("彻底删除回收站资源: key=%s dirs=%s", k, removed_dirs)
    return {"status": "deleted", "key": k, "removed_dirs": removed_dirs}


def kb_trash_empty() -> dict:
    """清空回收站（彻底删除全部，不可恢复）：删 .trash 下所有目录 + 清空 kb_trash 登记。

    仅作用于 knowledge_base/.trash/；主知识库/library/papers_meta 不受影响。
    """
    store = _need_store()
    keys = [r["key"] for r in store.trash_list()]
    trash_root = store.roots.kb_dir / ".trash"
    removed_dirs = 0
    if trash_root.is_dir():
        for d in list(trash_root.iterdir()):
            if d.is_dir():
                shutil.rmtree(str(d), ignore_errors=True)
                removed_dirs += 1
            elif d.is_file():
                try:
                    d.unlink()
                except OSError:
                    pass
    for k in keys:
        store.trash_remove(k)
    logger.info("清空回收站: 登记 %d 项, 目录 %d 个", len(keys), removed_dirs)
    return {"status": "emptied", "removed_keys": len(keys), "removed_dirs": removed_dirs}


def cleanup_stale_records() -> dict:
    """清理幽灵记录：kb 目录已删除但数据库仍有元数据/编译任务的条目。

    扫描 papers_meta + compile_jobs，删除 kb 目录不存在的条目。
    返回 {removed_meta: int, removed_jobs: int, kept: int}。
    """
    store = _need_store()
    kb = store.roots.kb_dir
    # 1) 收集磁盘上实际存在的 kb 目录键
    disk_keys: set[str] = set()
    if kb.exists():
        for d in kb.iterdir():
            if d.is_dir() and not d.name.startswith(("_", ".")):
                key, _ = dir_to_key(d.name, store)
                if key:
                    disk_keys.add(key)
    # 2) 清理 papers_meta 中 kb 目录不存在的条目
    removed_meta = 0
    kept = 0
    for m in store.list_meta(limit=10000):
        mkey = (m.doi or "").strip() or (m.rid or "")
        if mkey in disk_keys:
            kept += 1
            continue
        store.delete_meta(mkey)
        removed_meta += 1
    # 3) 清理 compile_jobs 中对应条目
    removed_jobs = 0
    for j in store.list_jobs():
        doi = j.get("paper_doi") or ""
        if doi and doi not in disk_keys:
            store.delete_job(doi, j.get("level", ""))
            removed_jobs += 1
    logger.info("清理幽灵记录: 删除元数据 %d 条, 编译任务 %d 条, 保留 %d 条",
                removed_meta, removed_jobs, kept)
    return {"removed_meta": removed_meta, "removed_jobs": removed_jobs, "kept": kept}


def regenerate_index() -> dict:
    """扫描 knowledge_base/ 重建总索引 _index.md（标题/DOI/年份/期刊/编译状态/链接）。
    编译/导入/同步后自动调用（文件系统即索引，卡帕西）。返回索引到的文献数。
    """
    store = _need_store()
    kb = store.roots.kb_dir
    if not kb.exists():
        return {"papers": 0, "path": None}
    rows = []
    for d in sorted(kb.iterdir()):
        if not d.is_dir() or d.name.startswith("_") or d.name == ".obsidian":
            continue
        doi, _kind = dir_to_key(d.name, store)  # T6：md5 目录也进索引（DOI 列显示 DOI 或 md5）
        if not doi:
            continue
        meta = store.get_meta(doi)
        note = (d / "_note.md").exists()
        wiki = (d / "_wiki.md").exists()
        relations = (d / "_relations.md").exists()
        level = ("L3" if relations else "L2" if wiki else "L1" if note else "未编译")
        links = []
        if note:
            links.append(f"[[{d.name}/_note|笔记]]")
        if wiki:
            links.append(f"[[{d.name}/_wiki|深度]]")
        if relations:
            links.append(f"[[{d.name}/_relations|\u5173\u7cfb]]")
        title = ((meta.title if meta else "") or d.name).replace("|", "｜")
        rows.append({
            "title": title, "doi": doi,
            "year": ((meta.year if meta else "") or "—").replace("|", "｜"),
            "journal": ((meta.journal if meta else "") or "—").replace("|", "｜"),
            "level": level, "links": " ".join(links),
        })
    lines = [
        "# 📚 文献知识库索引", "",
        "> 自动生成（paperkb.regenerate_index）——编译/导入/同步后自动更新。",
        "> Obsidian 打开本目录即可；`#doi/xxx` 或 `#ai-note` 标签搜索。", "",
        "| 标题 | DOI | 年份 | 期刊 | 编译 | 链接 |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(f"| {r['title']} | {r['doi']} | {r['year']} | {r['journal']} | {r['level']} | {r['links']} |")
    p = kb / "_index.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"papers": len(rows), "path": str(p)}


def sync_lit_meta(lit_db_path: str | Path = "") -> dict:
    """从 paperlit 的 lit.db 同步文献计量数据到 papers_meta（门面入口）。

    lit_db_path 缺省时按 data_dir/literature/lit.db 推导。
    """
    store = _need_store()
    if not lit_db_path:
        lit_db_path = store.roots.data_dir / "literature" / "lit.db"
    else:
        lit_db_path = Path(lit_db_path)
    result = store.sync_lit_meta(lit_db_path)
    logger.info("paperlit 元数据同步: %s", result)
    return result


def fill_journal_meta_batch() -> dict:
    """批量补全缺失的 IF/分区：对 papers_meta 中 impact_factor 或 quartile 为空的记录，
    从 journals.db 按 ISSN/期刊名查找并写入。返回 {total, filled, skipped}。"""
    store = _need_store()
    with store._conn() as conn:
        rows = conn.execute(
            "SELECT rid, doi FROM papers_meta"
            " WHERE impact_factor = 0 OR impact_factor IS NULL"
            " OR quartile = '' OR quartile IS NULL"
        ).fetchall()
    filled = 0
    for r in rows:
        key = r["doi"] or r["rid"]
        if store.fill_journal_meta(key):
            filled += 1
    return {"total": len(rows), "filled": filled, "skipped": len(rows) - filled}


def compile_backfill() -> dict:
    """为 knowledge_base/ 中未完成 L1 编译的文献入队（补齐编译结构标准）。

    跳过：已有 done L1 的、kb 无 document.json 的（无法编译）、无 bib 元数据的
    （需先 bib 导入/WOS 补元数据，编译器依赖权威元数据）。
    返回 {queued:[], skipped_done:[], missing_document:[], missing_meta:[], failed:[]}。
    """
    store = _need_store()
    c = _need_compiler()
    kb = store.roots.kb_dir
    queued, skipped_done, missing_doc, missing_meta, failed = [], [], [], [], []
    if kb.exists():
        for d in sorted(kb.iterdir()):
            if not d.is_dir() or d.name.startswith("_") or d.name == ".obsidian":
                continue
            key, kind = dir_to_key(d.name, store)
            if kind == "md5" and is_md5_dir(key):
                continue  # T6：纯 md5 文献（无 DOI）无法编译（编译依赖 bib 元数据）
            doi = key
            if not doi:
                continue
            job = store.get_job(doi, "L1")
            if job and job.get("status") == "done":
                skipped_done.append(doi)
                continue
            if not (d / "document.json").exists():
                missing_doc.append(doi)
                continue
            try:
                c.queue(doi, "L1")
                queued.append(doi)
            except Exception as e:  # noqa: BLE001 - 无元数据等逐篇容错
                msg = str(e)
                if "元数据" in msg or "bib" in msg.lower():
                    missing_meta.append(doi)
                else:
                    failed.append({"doi": doi, "error": msg})
    return {"queued": queued, "skipped_done": skipped_done,
            "missing_document": missing_doc, "missing_meta": missing_meta,
            "failed": failed}


def compile_batch(dois: list[str], action: str = "retry") -> dict:
    """批量编译（直接执行，不走队列）。

    Args:
        dois: 目标文献 DOI 列表
        action: "retry"=重试失败的编译 / "l3"=执行 L3 跨文献分析

    Returns:
        {"success": N, "failed": N, "skipped": N,
         "results": [{"doi": ..., "status": "done"|"failed"|"skipped", "error": ...}]}
    """
    c = _need_compiler()
    results: list[dict] = []
    success = failed = skipped = 0

    for doi in dois:
        try:
            if action == "l3":
                r = c.compile(doi, "L3")
            else:
                r = c.compile(doi, "L1", force=True)
            status = r.get("status", "done")
            if status == "skipped_existing":
                skipped += 1
                results.append({"doi": doi, "status": "skipped", "error": ""})
            else:
                success += 1
                results.append({"doi": doi, "status": "done", "error": ""})
        except Exception as e:  # noqa: BLE001
            failed += 1
            results.append({"doi": doi, "status": "failed", "error": str(e)})

    if success > 0:
        try:
            regenerate_index()
        except Exception:  # noqa: BLE001
            pass

    return {"success": success, "failed": failed, "skipped": skipped,
            "results": results}


def kb_dois() -> set[str]:
    """知识库（knowledge_base/）中所有文献的 DOI/键集合（轻量，供图谱 in_kb 标记）。

    与 ``stats()`` 不同：不计算编译状态/缺失清单，仅扫描 kb 目录映射键，开销小。
    无 DOI 的资料（书/学位论文/中文文献）返回其 rid 键。
    """
    store = _need_store()
    kb = store.roots.kb_dir
    keys: set[str] = set()
    if kb.exists():
        for d in kb.iterdir():
            if not d.is_dir() or d.name.startswith("_") or d.name == ".obsidian":
                continue
            key, _kind = dir_to_key(d.name, store)
            if key:
                keys.add(key)
    return keys


def stats() -> dict:
    """知识库统计/总览（P4 首页统计面板数据源）。

    规模（元数据/库文献/kb 纳入）+ 编译完成度（L1/L2/L3/队列）+ 缺失清单
    （未纳入 kb / 未编译 / 缺 bib 元数据）。
    """
    store = _need_store()
    kb = store.roots.kb_dir
    lib = store.roots.library_dir
    all_dois = store.all_dois()

    kb_rows = []
    kb_papers = L1 = L2 = L3 = 0
    if kb.exists():
        for d in sorted(kb.iterdir()):
            if not d.is_dir() or d.name.startswith("_") or d.name == ".obsidian":
                continue
            key, _kind = dir_to_key(d.name, store)  # T6：md5 目录计入 kb 统计
            if not key:
                continue
            kb_papers += 1
            has_note = (d / "_note.md").exists()
            has_wiki = (d / "_wiki.md").exists()
            has_relations = (d / "_relations.md").exists()
            L1 += has_note
            L2 += has_wiki
            L3 += has_relations
            kb_rows.append({"doi": key, "dir": d.name, "note": has_note,
                            "wiki": has_wiki, "relations": has_relations})

    library_papers = 0
    lib_dois: set[str] = set()
    if lib.exists():
        for d in lib.iterdir():
            if d.is_dir() and not d.name.startswith("."):
                key, _kind = dir_to_key(d.name, store)  # T6：md5 目录计入 library 统计
                if key:
                    library_papers += 1
                    lib_dois.add(key)

    jobs = compile_status()
    queued = sum(1 for j in jobs if j.get("status") in ("queued", "pending"))
    processing = sum(1 for j in jobs if j.get("status") == "compiling")
    failed = sum(1 for j in jobs if j.get("status") == "failed")

    kb_dois = {r["doi"] for r in kb_rows}
    return {
        "scale": {"meta_count": len(all_dois), "library_papers": library_papers,
                  "kb_papers": kb_papers},
        "compile": {"l1_done": L1, "l2_done": L2, "l3_done": L3,
                    "queued": queued, "processing": processing, "failed": failed},
        "missing": {"not_in_kb": sorted(lib_dois - kb_dois),
                    "uncompiled": sorted(r["doi"] for r in kb_rows if not r["note"]),
                    "missing_meta": missing_dois()},
        "kb_papers_detail": kb_rows,
    }


# ---------------------------------------------------------------- 状态

def status() -> dict:
    store = _need_store()
    return {
        "initialized": True,
        "meta_count": len(store.all_dois()),
        "vector_impl": _settings.vector_impl,
        "fts_enabled": _settings.fts_enabled,
        "rrf_k": _settings.rrf_k,
        "rerank_enabled": _settings.rerank_enabled,
        "reranker_model": _settings.reranker_model,
        "db": str(store.db_path),
    }


# ---------------------------------------------------------------- 向量检索

def kb_vector_search(query: str, top_k: int = 20,
                     exclude_doi: str = "",
                     with_snippet: bool = True) -> list[dict]:
    """知识库编译结果向量相似度搜索（块级）。

    需 vector_impl=kb + SILICONFLOW_API_KEY；不满足时返回空列表。

    Args:
        query: 查询文本
        top_k: 返回数量
        exclude_doi: 排除的 DOI（L3 概念层搜索时排除自身）
        with_snippet: 附带"命中块原文"（从产物文件按偏移切回）——检索注入用，
            保证"命中段 = 注入段"；L3 候选发现不需要，可传 False 省 I/O。
    """
    if _settings.vector_impl != "kb":
        return []
    import os
    api_key = os.environ.get("SILICONFLOW_API_KEY", "").strip()
    if not api_key:
        return []
    from .vector import get_kb_vector_index
    store = _need_store()
    idx = get_kb_vector_index(store.roots, api_key=api_key)
    return idx.search(query, top_k=top_k, exclude_doi=exclude_doi,
                      with_snippet=with_snippet, store=store)


def kb_vector_search_by_concepts(concept_names: list[str],
                                 top_k: int = 20,
                                 exclude_doi: str = "") -> list[dict]:
    """混合检索：概念倒排 + 向量相似度。

    Args:
        concept_names: 查询概念列表
        top_k: 返回数量
        exclude_doi: 排除的 DOI
    """
    if _settings.vector_impl != "kb":
        return []
    import os
    api_key = os.environ.get("SILICONFLOW_API_KEY", "").strip()
    if not api_key:
        return []
    from .vector import get_kb_vector_index
    store = _need_store()
    idx = get_kb_vector_index(store.roots, api_key=api_key)
    return idx.search_by_concepts(concept_names, top_k=top_k, store=store,
                                  exclude_doi=exclude_doi)


def _index_paper_from_disk(idx, store, doi: str, folder, force: bool = False) -> int:
    """把某篇 kb 目录的编译产物喂给向量索引（内容未变则只刷偏移/小节）。

    重建（`rebuild_kb_vector_index`）与死信重试（`kb_index_retry`）共用这段口径，
    避免"重建认得 _relations.md、重试漏了它"这类分叉（2026-09-21 审计踩过）。
    """
    note_text = ""
    title = ""
    note_path = folder / "_note.md"
    if note_path.exists():
        note_text = note_path.read_text(encoding="utf-8", errors="replace")
        for line in note_text.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break
    wiki_path = folder / "_wiki.md"
    wiki_text = (wiki_path.read_text(encoding="utf-8", errors="replace")
                 if wiki_path.exists() else "")
    relations_path = folder / "_relations.md"
    relations_text = (relations_path.read_text(encoding="utf-8", errors="replace")
                      if relations_path.exists() else "")
    concepts = []
    try:
        concepts = store.concepts_for_doi(doi)
    except Exception:  # noqa: BLE001 - 概念缺失不影响正文索引
        pass
    idx.index_paper(doi, note_text=note_text, wiki_text=wiki_text,
                    relations_text=relations_text, concepts=concepts,
                    title=title, force=force, folder=folder)
    return idx.count_for(doi)


def _folder_for_doi(roots, doi: str):
    """定位某篇的 kb 目录（DOI 目录名 → 原样目录名 → frontmatter DOI 扫描）。"""
    import re

    from .doi import doi_to_dirname

    for cand in (roots.kb_dir / doi_to_dirname(doi), roots.kb_dir / doi):
        if (cand / "_note.md").is_file():
            return cand
    for note in roots.kb_dir.rglob("_note.md"):
        try:
            text = note.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = re.search(r"^doi:\s*(.+)$", text, re.MULTILINE)
        if m and m.group(1).strip() == doi:
            return note.parent
    return None


def _kb_index(api_key: str = ""):
    """取向量索引实例（单例数据层；api_key 缺省读环境变量）。"""
    import os

    from .vector import get_kb_vector_index

    store = _need_store()
    key = api_key or os.environ.get("SILICONFLOW_API_KEY", "").strip()
    return get_kb_vector_index(store.roots, api_key=key), store


def kb_index_status() -> dict:
    """索引健康快照（只读，不调 embedding）：体积 / 段数 / 垃圾行 / 死信 / 模型指纹。"""
    store = _need_store()
    from .db import get_index_store

    ist = get_index_store(store.roots)
    h = ist.health()
    h["model"] = ist.get_meta("model")
    h["dim"] = ist.get_meta("dim")
    h["legacy_files_present"] = ist.legacy_files_present()
    h["dead_letters_detail"] = ist.dead_letters(limit=50)
    return h


def kb_index_scan() -> dict:
    """对账：摘除产物已不在磁盘的幽灵块 + 重算篇级统计（零 embedding 成本）。"""
    store = _need_store()
    from .db import get_index_store

    ist = get_index_store(store.roots)
    idx, _ = _kb_index()
    pruned = idx.prune_unreadable()
    ist.refresh_papers_state()
    h = ist.health()
    return {"pruned": pruned, "live_passages": h["live_passages"],
            "garbage_rows": h["garbage_rows"], "missing_segments": h["missing_segments"]}


def kb_index_compact() -> dict:
    """压实段文件（回收刷新/删除产生的垃圾行）。"""
    idx, _ = _kb_index()
    return idx.compact()


def kb_index_retry(limit: int = 20) -> dict:
    """重试到期的死信（重新索引该篇）。成功由 index_paper 自动结案。"""
    store = _need_store()
    from .db import get_index_store

    ist = get_index_store(store.roots)
    due = ist.due_dead_letters(limit=max(1, min(200, limit)))
    if not due:
        return {"retried": 0, "resolved": 0, "skipped": 0}
    idx, _ = _kb_index()
    retried = resolved = skipped = 0
    for row in due:
        doi, op = row.get("doi") or "", row.get("op") or ""
        if op != "vector_index" or not doi:
            ist.resolve_dead_letter(row["id"])
            skipped += 1
            continue
        folder = _folder_for_doi(store.roots, doi)
        if folder is None:
            ist.resolve_dead_letter(row["id"])   # 目录已不在：无从重试，结案
            skipped += 1
            continue
        retried += 1
        try:
            _index_paper_from_disk(idx, store, doi, folder, force=True)
        except Exception:  # noqa: BLE001 - 失败保持未结案，等下次退避重试
            continue
        if idx.count_for(doi) > 0:
            resolved += 1
    return {"retried": retried, "resolved": resolved, "skipped": skipped}


def rebuild_kb_vector_index(force: bool = False,
                            progress_cb=None) -> dict:
    """批量为所有已编译文献构建/重建 KB 向量索引。

    Args:
        force: 是否强制重建（忽略已索引的）
        progress_cb: 进度回调 (indexed: int, total: int, doi: str)

    Returns:
        {"indexed": 有向量的篇数, "total": 扫描篇数, "skipped": 无向量篇数,
         "pruned": 摘除的幽灵块数（产物已不在磁盘）}
    """
    import os
    api_key = os.environ.get("SILICONFLOW_API_KEY", "").strip()
    if not api_key:
        return {"indexed": 0, "total": 0, "error": "SILICONFLOW_API_KEY 未配置"}

    store = _need_store()
    roots = store.roots
    kb_dir = roots.kb_dir

    # 扫描所有已编译文献（有 _note.md 的目录）
    compiled = []
    for note_path in kb_dir.rglob("_note.md"):
        folder = note_path.parent
        # 从 frontmatter 提取真实 DOI（目录名可能是 doi_to_dirname 格式）
        doi = ""
        try:
            text = note_path.read_text(encoding="utf-8", errors="replace")
            import re
            m = re.search(r"^doi:\s*(.+)$", text, re.MULTILINE)
            if m:
                doi = m.group(1).strip()
        except Exception:
            pass
        if not doi:
            doi = folder.name  # 兜底用目录名
        compiled.append((doi, folder))

    if not compiled:
        return {"indexed": 0, "total": 0, "skipped": 0}

    from .vector import get_kb_vector_index
    idx = get_kb_vector_index(roots, api_key=api_key)

    indexed = 0
    skipped = 0
    total = len(compiled)

    for i, (doi, folder) in enumerate(compiled):
        # 计数按"该篇最终有无向量"算：内容未变时 index_paper 返回 0（不重复嵌），
        # 但该篇**已索引**，不该记成 skipped。
        if _index_paper_from_disk(idx, store, doi, folder, force=force) > 0:
            indexed += 1
        else:
            skipped += 1

        if progress_cb:
            progress_cb(i + 1, total, doi)

    # 对账：产物目录已被外部删除的块要摘掉（否则语义检索召回"读不回原文"的幽灵条目）
    pruned = idx.prune_unreadable()
    return {"indexed": indexed, "total": total, "skipped": skipped,
            "pruned": pruned}
