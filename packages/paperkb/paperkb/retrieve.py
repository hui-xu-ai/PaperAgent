# -*- coding: utf-8 -*-
"""检索编排（KB-DESIGN v0.6 §7：Karpathy——主要检索编译产物，降检索成本）。

多路召回（budget 受控）：
  notes_fts（编译产物，主）→ 向量语义（编译产物分块，可选）→ meta_fts（元数据补充）
  → 引用邻域（citations）→ 卡片按需
问答注入预算 ≤8k token：笔记优先 + 元数据补充 + 全文兜底（可选）。
答案带 [[DOI]] 引用；不回灌（飞轮 _qa 由用户一键，防噪声）。

2026-09-21 审计修复：
- **向量一路接进来**（此前 `kb_vector_search` 只有 api 门面导出、无任何调用方 ⇒
  "RAG 混合检索"实际只有 FTS 一路）；
- 各片段截断一律走 `textseg.boundary_trim`（旧 `content[:4000]` 会切在句子/公式中间）；
- 向量命中给的是**命中块原文**（`with_snippet`），不是"文件开头 4000 字"。
"""
from __future__ import annotations

import logging

from .config import Roots
from .db import KBStore
from .journals import JournalsDB
from .llm import get_llm
from .textseg import boundary_trim

logger = logging.getLogger(__name__)

# 注入预算（字符近似 token；英文 ~4 字符/token，中文 ~2）
BUDGET_CHARS = 16_000          # ≈4-8k token
NOTES_SHARE = 0.75             # 笔记 6k token 份额
MAX_NOTES_PER_DOC = 2          # 每篇最多取几个产物文件
MAX_NEIGHBOR_DOCS = 3          # 引用邻域最多补几篇
SNIPPET_CHARS = 1200           # 单条注入片段上限（片段级，命中块约 900 字）
NOTES_QUOTA = 2 / 3            # notes_fts 主路占 top_k 的名额比例（余量给其他通道）

# 向量命中 → 注入条目用的"文件名"（与 notes_fts 的 filename 对齐，便于跨路去重）
_VEC_FILE = {"note": "_note.md", "wiki": "_wiki.md", "relations": "_relations.md",
             "concepts": "_concepts"}

SYSTEM_PROMPT = (
    "你是文献知识库助手。回答基于知识库中的编译笔记与元数据。"
    "引用来源用 [[DOI]] 标注；不确定的内容明确说明，不编造。"
)


def _vector_recall(query: str, top_k: int) -> list[dict]:
    """向量语义召回（编译产物分块；命中块原文即注入片段）。

    不可用（vector_impl≠kb / 无 API key / 无常量索引）时静默返回 []——FTS 仍是主路。
    每个 DOI 只取最高分块（避免同一篇的多个块挤占名额）；`title` 块不参与注入
    （内容只有标题，作为证据太弱）。
    """
    try:
        from . import api

        hits = api.kb_vector_search(query, top_k=max(6, top_k * 3),
                                    with_snippet=True)
    except Exception as e:  # noqa: BLE001 - 向量不可用不影响检索
        logger.info("向量召回不可用: %s", e)
        return []

    best: dict[str, dict] = {}
    for h in hits or []:
        ptype = (h.get("passage_type") or "").strip()
        doi = (h.get("doi") or "").strip()
        snip = (h.get("snippet") or "").strip()
        if ptype == "title" or not doi or not snip:
            continue
        score = float(h.get("score") or 0.0)
        if doi not in best or score > best[doi]["score"]:
            best[doi] = {"doi": doi,
                         "file": _VEC_FILE.get(ptype, "_note.md"),
                         "snippet": snip, "score": score}
    return sorted(best.values(), key=lambda x: -x["score"])[:top_k]


def recall(store: KBStore, roots: Roots, query: str, *,
           top_k: int = 8, include_fulltext: bool = False,
           prefer_dois: list[str] | None = None) -> list[dict]:
    """多路召回 → 合并去重排序。返回 [{"doi","file","snippet","source","score"}]。"""
    from .db import _fts_query

    items: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def _add(doi: str, file: str, snippet: str, source: str, score: float) -> None:
        key = (doi, file)
        if key in seen:
            return
        seen.add(key)
        items.append({"doi": doi, "file": file, "snippet": snippet,
                      "source": source, "score": score})

    # 1) notes_fts 主召回（编译产物）
    for r in store.search_notes(query, limit=top_k * 2):
        _add(r["doi"], r["file"], r.get("snippet") or "", "notes", 1.0)
    # 1b) 向量语义召回（编译产物分块，命中段即注入段；vector_impl=kb 才生效）
    for v in _vector_recall(query, top_k):
        _add(v["doi"], v["file"], v["snippet"], "vector", 0.9)
    # 2) meta_fts 元数据召回（文献级）
    for m in store.search_meta(query, limit=top_k * 2):
        _add(m.doi, "_meta",
             f"{m.title} — {boundary_trim(m.abstract or '', 150)}", "meta", 0.6)
    # 3) 引用邻域：命中文献的 引用/被引 文献的笔记（跨文献关联）
    hit_dois = [it["doi"] for it in items][:top_k]
    if hit_dois:
        neighbor_dois: list[str] = []
        for doi in hit_dois[:MAX_NEIGHBOR_DOCS]:
            rel = store.citations_for(doi)
            for c in rel.get("cited", [])[:8]:
                if c["doi"] not in neighbor_dois:
                    neighbor_dois.append(c["doi"])
            for c in rel.get("citing", [])[:8]:
                if c["doi"] not in neighbor_dois:
                    neighbor_dois.append(c["doi"])
        for ndoi in neighbor_dois[:MAX_NEIGHBOR_DOCS]:
            if ndoi == hit_dois[0]:
                continue
            for r in store.search_notes(query, limit=5):
                if r["doi"] == ndoi:
                    _add(r["doi"], r["file"], r.get("snippet") or "", "neighbor", 0.4)
                    break
    # 4) 卡片按需读取（命中文献）
    from .cards import list_cards, read_cards_for_compile

    for doi in hit_dois[:top_k]:
        card_ctx = read_cards_for_compile(roots.kb_dir, doi, limit_chars=600)
        if card_ctx:
            _add(doi, "cards", boundary_trim(card_ctx, 200), "cards", 0.5)
    # 5) 全文兜底：附件镜像行（复合键 `<rid>::attachments/…`）**默认召回**；
    #    正文全文（纯 doi 键）仍是可选开关行为（include_fulltext）。
    ft_rows = store.search_fulltext(query, limit=top_k * 4 if include_fulltext else top_k * 2)
    for r in ft_rows:
        key = r.get("doi") or ""
        rid, sep, rel = key.partition(KBStore.ATTACH_FT_SEP)
        if sep and rel.startswith("attachments/"):
            _add(rid, rel, r.get("snippet") or "", "fulltext", 0.5)
        elif include_fulltext and not sep:
            _add(key, "_fulltext", r.get("snippet") or "", "fulltext", 0.3)

    # 通道配额：主路 notes_fts（整文件命中）最多占 ~2/3 名额，其余留给向量/元数据等
    # ——纯按 score 排序时 notes(1.0) 会把 top_k 全吃满，语义一路即使命中也不出场
    # （实测 7 篇库里 top_k=6 全是 notes）。其余通道为空时用 notes 补齐，不缩水。
    primary = [it for it in items if it["source"] == "notes"]
    others = [it for it in items if it["source"] != "notes"]
    keep_notes = max(1, int(top_k * NOTES_QUOTA))
    out = primary[:keep_notes] + others
    if len(out) < top_k:
        out += primary[keep_notes:]
    out.sort(key=lambda x: -x["score"])
    return out[:top_k]


def recall_paper(store: KBStore, roots: Roots, doi: str, query: str, *,
                 top_k: int = 4) -> list[dict]:
    """单篇编译笔记召回（Q5 阶段2）：只取该 DOI 的编译产物（_note/_wiki/_relations），
    按 query 相关性排序。返回 [] = 未编译或该篇无命中（上层回退全文片段）。"""
    from .doi import normalize_doi

    doi = normalize_doi(doi)
    if not doi:
        return []
    items: list[dict] = []
    for r in store.search_notes(query, limit=top_k, doi=doi):
        items.append({"doi": r["doi"], "file": r["file"],
                      "snippet": r.get("snippet") or "", "source": "notes",
                      "score": 1.0})
    return items


def paper_notes_all(store: KBStore, roots: Roots, doi: str, *,
                    max_files: int = 2, max_chars: int = 4000) -> list[dict]:
    """该篇**全部已编译笔记**（按相关性之外的口径：按文件名顺序取前 N 份，逐份截断）。

    与 `recall_paper` 的区别（为什么需要它，2026-09-12 用户反馈）：
    `recall_paper` 是**检索**——问句匹配不上就返回 []，这本身没错；但在"**单篇**文献会话"里
    目标文档是**已知的**，唯一要决定的是"给哪些内容"。中文问句在 FTS（无空格整句被当短语）
    与 LIKE 兜底上都可能 0 命中（实测用户三个中文问题全部 0 命中）⇒ 笔记明明编译好了却
    一份都进不了上下文，模型只能拿题名+章节索引泛泛而谈。
    所以单篇会话直接用这个函数兜底：笔记通常 1~3 份、每份 1~4KB，成本可控。
    """
    from .doi import normalize_doi

    doi = normalize_doi(doi)
    if not doi:
        return []
    out: list[dict] = []
    try:
        files = store.notes_files(doi)[:max(1, max_files)]
    except Exception:  # noqa: BLE001 - 读取失败按无笔记处理
        return []
    for f in files:
        try:
            content = store.notes_content(doi, f) or ""
        except Exception:  # noqa: BLE001
            content = ""
        if content:
            out.append({"doi": doi, "file": f, "snippet": boundary_trim(content, max_chars),
                        "source": "notes", "score": 0.5})
    return out


def paper_compiled_files(store: KBStore, doi: str) -> list[str]:
    """该篇已编译产物文件名（用于会话提示注入编译状态）；无则空。"""
    from .doi import normalize_doi

    doi = normalize_doi(doi)
    if not doi:
        return []
    return store.notes_files(doi)


def build_context(items: list[dict], store: KBStore, roots: Roots,
                  budget_chars: int = BUDGET_CHARS) -> str:
    """按预算组装注入上下文（笔记优先，元数据/邻域补充）。"""
    parts: list[str] = []
    used = 0
    # 笔记类（_note/_wiki/_relations/cards）优先
    note_items = [it for it in items if it["source"] in ("notes", "cards")]
    other_items = [it for it in items if it["source"] in ("meta", "neighbor", "fulltext")]
    for it in note_items[:MAX_NOTES_PER_DOC * 4]:
        content = store.notes_content(it["doi"], it["file"]) if it["file"] != "cards" else ""
        if it["file"] == "cards":
            from .cards import read_cards_for_compile
            content = read_cards_for_compile(roots.kb_dir, it["doi"], limit_chars=1500)
        if not content:
            content = it.get("snippet") or ""
        block = f"### [{it['doi']}] {it['file']}\n{boundary_trim(content, 4000)}"
        if used + len(block) > int(budget_chars * NOTES_SHARE):
            break
        parts.append(block)
        used += len(block)
    for it in other_items:
        block = f"### [{it['doi']}] ({it['source']})\n{it.get('snippet') or ''}"
        if used + len(block) > budget_chars:
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts)


def answer(query: str, *, top_k: int = 8, include_fulltext: bool = False,
           budget_chars: int = BUDGET_CHARS) -> dict:
    """问答编排：多路召回 → 组装上下文（≤预算）→ LLM 回答（带引用）。

    上下文只含编译产物/元数据片段；翻译/编译会话历史不进入（D20 隔离）。
    """
    from . import api

    store = api._need_store()  # noqa: SLF001
    roots = store.roots
    items = recall(store, roots, query, top_k=top_k,
                   include_fulltext=include_fulltext)
    if not items:
        return {"answer": "知识库中未检索到相关内容。", "items": []}
    ctx = build_context(items, store, roots, budget_chars)
    prompt = (SYSTEM_PROMPT +
              "\n\n## 知识库片段（引用来源用 [[DOI]] 标注）\n" + ctx +
              "\n\n## 问题\n" + query +
              "\n\n请基于以上片段回答（中文），不确定的明确说明。")
    llm = get_llm()
    reply = llm.complete(prompt, context="ask")
    return {"answer": reply.strip(), "items": items, "context_chars": len(ctx)}
