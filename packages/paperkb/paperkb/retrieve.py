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
MAX_NEIGHBOR_DOCS = 3          # 引用邻域最多补几篇
SNIPPET_CHARS = 1200           # 单条注入片段上限（片段级，小节扩展后约 1.2k 字）
RERANK_MIN_POOL = 12           # 候选池下限（少于它就没什么可重排的）
# 注入时"读整份产物"的条数（note/wiki/relations 是 1~3KB，整份比片段更能保住数值/条件）
FULL_PRODUCT_TOP = 3
FULL_PRODUCT_CHARS = 3000      # 单份产物注入上限（超出按边界截断）
_PRODUCT_FILES = {"_note.md", "_wiki.md", "_relations.md"}

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
           prefer_dois: list[str] | None = None,
           rerank: bool | None = None) -> list[dict]:
    """多路召回 → RRF 融合 → 交叉编码器精排 → 去重排序。

    返回 [{"doi","file","snippet","source","rrf","rerank_score"[,"score"]}]。

    融合口径（2026-09-21 起）：各路召回只贡献**排名**，用 RRF（`Σ w/(k+rank)`，
    k=60）合并——跨通道的余弦/BM25 分数本就不可比，硬编码权重（旧实现的
    1.0/0.9/0.6）只是掩盖问题。精排用 bge-reranker-v2-m3（~¥0.001/次），
    失败或无 key 时静默跳过，退回 RRF 顺序。
    """
    channels: list[tuple[str, float, list[dict]]] = []

    # 1) notes_fts 主召回（编译产物）
    notes = [{"doi": r["doi"], "file": r["file"], "snippet": r.get("snippet") or ""}
             for r in store.search_notes(query, limit=top_k * 2)]
    channels.append(("notes", 1.0, notes))
    # 2) 向量语义召回（编译产物分块，命中段即注入段；vector_impl=kb 才生效）
    channels.append(("vector", 1.0, _vector_recall(query, top_k * 2)))
    # 3) meta_fts 元数据召回（文献级）
    channels.append(("meta", 0.5, [
        {"doi": m.doi, "file": "_meta",
         "snippet": f"{m.title} — {boundary_trim(m.abstract or '', 150)}"}
        for m in store.search_meta(query, limit=top_k * 2)]))
    # 4) 引用邻域：命中文献的 引用/被引 文献的笔记（跨文献关联）
    hit_dois = [it["doi"] for ch in channels for it in ch[2]][:top_k]
    channels.append(("neighbor", 0.4, _neighbor_items(store, query, hit_dois)))
    # 5) 卡片按需读取（命中文献）
    channels.append(("cards", 0.5, _card_items(roots, hit_dois)))
    # 6) 全文兜底：附件镜像行（复合键 `<rid>::attachments/…`）**默认召回**；
    #    正文全文（纯 doi 键）仍是可选开关行为（include_fulltext）。
    channels.append(("fulltext", 0.6, _fulltext_items(
        store, query, include_fulltext, top_k)))

    fused = _rrf_fuse(channels, k=_rrf_k())            # 排名倒数融合 + 去重
    pool = fused[: max(RERANK_MIN_POOL, top_k * 3)]
    if _rerank_on(rerank) and len(pool) > 1:
        _rerank_items(query, pool)
        pool.sort(key=lambda x: (-x.get("rerank_score", float("-inf")), -x["rrf"]))
    return pool[:top_k]


def _rrf_k() -> int:
    try:
        from .api import _settings

        return int(getattr(_settings, "rrf_k", 60) or 60)
    except Exception:  # noqa: BLE001
        return 60


def _rrf_fuse(channels: list[tuple[str, float, list[dict]]],
              k: int = 60) -> list[dict]:
    """RRF 融合：`score(d) = Σ_ch w_ch / (k + rank_ch(d))`（rank 从 1 起）。

    同一 (doi, file) 在多路命中就累加——这正是 RRF 的用意：**多路都召回的更可信**。
    保留首次出现的 channel 作为 `source`（可观测性），片段取"信息量更大的那个"
    （长度优先：小节扩展 > FTS 窗口 > 标题）。输出按 RRF 降序。
    """
    best: dict[tuple[str, str], dict] = {}
    for name, weight, items in channels:
        for rank, it in enumerate(items, 1):
            key = (it["doi"], it.get("file") or "")
            score = weight / (k + rank)
            cur = best.get(key)
            if cur is None:
                best[key] = {**it, "source": name, "rrf": score, "channels": [name]}
                continue
            cur["rrf"] += score
            cur["channels"].append(name)
            other = it.get("snippet") or ""
            if len(other) > len(cur.get("snippet") or ""):
                cur["snippet"] = other
            if name == "vector":        # 向量片段是"命中块/小节"，优先作为注入文本
                cur["source"] = "vector"
    return sorted(best.values(), key=lambda x: -x["rrf"])


def _rerank_on(rerank: bool | None) -> bool:
    """是否做二阶段重排：显式参数 > KbSettings.rerank_enabled（且必须有 key）。"""
    try:
        from .api import _settings

        enabled = bool(getattr(_settings, "rerank_enabled", True))
    except Exception:  # noqa: BLE001
        enabled = True
    enabled = enabled if rerank is None else bool(rerank)
    return enabled and bool(_rerank_key())


def _rerank_key() -> str:
    import os

    return (os.environ.get("SILICONFLOW_RERANK_API_KEY", "").strip()
            or os.environ.get("SILICONFLOW_API_KEY", "").strip())


def _rerank_items(query: str, items: list[dict]) -> None:
    """就地把 bge-reranker-v2-m3 分数写进 `rerank_score`（失败静默跳过）。"""
    from .api import _settings

    docs = [(it.get("snippet") or "").strip() for it in items]
    if not any(docs):
        return
    try:
        from paperlit.vector.reranker import rerank as _rr

        scored = _rr(query=query, documents=docs,
                     model=getattr(_settings, "reranker_model",
                                   "BAAI/bge-reranker-v2-m3"),
                     api_key=_rerank_key(), top_n=len(docs))
    except Exception as e:  # noqa: BLE001 - 重排是加分项，失败不影响召回
        logger.warning("重排失败（退回 RRF 顺序）: %s", e)
        return
    for idx, score in scored:
        if 0 <= idx < len(items):
            items[idx]["rerank_score"] = float(score)


def _neighbor_items(store: KBStore, query: str, hit_dois: list[str]) -> list[dict]:
    out: list[dict] = []
    if not hit_dois:
        return out
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
                out.append({"doi": r["doi"], "file": r["file"],
                            "snippet": r.get("snippet") or ""})
                break
    return out


def _card_items(roots: Roots, hit_dois: list[str]) -> list[dict]:
    from .cards import read_cards_for_compile

    out: list[dict] = []
    for doi in hit_dois:
        ctx = read_cards_for_compile(roots.kb_dir, doi, limit_chars=600)
        if ctx:
            out.append({"doi": doi, "file": "cards",
                        "snippet": boundary_trim(ctx, 200)})
    return out


def _fulltext_items(store: KBStore, query: str, include_fulltext: bool,
                    top_k: int) -> list[dict]:
    out: list[dict] = []
    for r in store.search_fulltext(
            query, limit=top_k * 4 if include_fulltext else top_k * 2):
        key = r.get("doi") or ""
        rid, sep, rel = key.partition(KBStore.ATTACH_FT_SEP)
        if sep and rel.startswith("attachments/"):
            out.append({"doi": rid, "file": rel, "snippet": r.get("snippet") or ""})
        elif include_fulltext and not sep:
            out.append({"doi": key, "file": "_fulltext",
                        "snippet": r.get("snippet") or ""})
    return out


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
                  budget_chars: int = BUDGET_CHARS, *,
                  full_products: int = FULL_PRODUCT_TOP) -> str:
    """按预算组装注入上下文：**编译产物整份注入优先**，其余按片段补。

    2026-09-21 重写（两处缺陷）：
    - 旧实现按 `source` 分两组（notes/cards 与 meta/neighbor/fulltext）——**向量一路
      `source="vector"` 两组都不在 ⇒ 整条被丢掉**。向量接进召回后这是实打实的证据损失
      （实测一次问答 5 条命中里 4 条 source=vector，全丢）。
    - 旧实现对产物类虽读整份，但**先按 source 判身份**：同一份产物被向量命中时只能拿到
      片段（≤1200 字）。实测后果：`_note.md` 1625 字里「研究结果」的数值落在偏移
      603~1100，片段在 570 字处断掉，模型只能答"数值被截断"（而产物里明明有）。

    现在按 `file` 判身份：note/wiki/relations 一律 (doi,file) 去重后取前
    `full_products` 条**读整份产物**（≤3000 字/份，边界截断），卡片按 type 读，
    其余（meta/neighbor/fulltext）用命中片段。
    """
    seen: set[tuple[str, str]] = set()
    products: list[dict] = []
    cards: list[dict] = []
    others: list[dict] = []
    for it in items:
        key = ((it.get("doi") or ""), (it.get("file") or ""))
        if key in seen:
            continue
        seen.add(key)
        if key[1] in _PRODUCT_FILES:
            products.append(it)
        elif key[1] == "cards":
            cards.append(it)
        else:
            others.append(it)

    parts: list[str] = []
    used = 0
    for it in products[:max(0, full_products)]:
        content = store.notes_content(it["doi"], it["file"]) or ""
        if not content:
            content = it.get("snippet") or ""
        block = f"### [{it['doi']}] {it['file']}\n{boundary_trim(content, FULL_PRODUCT_CHARS)}"
        if used + len(block) > budget_chars:
            break
        parts.append(block)
        used += len(block)
    for it in cards:
        from .cards import read_cards_for_compile

        content = read_cards_for_compile(roots.kb_dir, it["doi"], limit_chars=1500)
        block = f"### [{it['doi']}] 卡片\n{boundary_trim(content, 1500)}"
        if not content or used + len(block) > budget_chars:
            continue
        parts.append(block)
        used += len(block)
    for it in products[max(0, full_products):] + others:
        block = (f"### [{it['doi']}] ({it.get('source') or ''})\n"
                 f"{it.get('snippet') or ''}")
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
