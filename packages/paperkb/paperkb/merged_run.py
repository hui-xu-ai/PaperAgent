# -*- coding: utf-8 -*-
"""合并单次调用的**执行入口**：一次 LLM 请求产出 L1/L2/L3 + 全文译文，并写盘。

调用方（`engine_service`）在"智谱 + 开关打开"时改走这里；失败/覆盖不足抛 `MergedFallback`
由调用方回退既有分步路径（**不原地重写分步逻辑**）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from . import compile as _compile
from .context import context_paragraphs, paper_context_with_math, with_task
from .doc import read_document
from .llm import get_llm
from .merged import merged_task, parse_merged, translation_coverage

logger = logging.getLogger(__name__)

MIN_COVERAGE = 0.9          # 译文覆盖率下限（低于此判失败 → 回退分步）
MIN_L2_CHARS = 20           # 与 compile._l2_output_ok 同口径


class MergedFallback(RuntimeError):
    """合并调用不可用（解析失败/截断/覆盖不足）→ 调用方回退分步路径。"""


def _targets(doc) -> tuple[list[str], dict[str, int]]:
    """待译 para_id 清单（**与 run_translate 同一判据**：模型看得到原文、非标题、text_en 非空）。"""
    allowed = {p.para_id for p in context_paragraphs(doc) if getattr(p, "para_id", "")}
    ids: list[str] = []
    for p in doc.paragraphs:
        pid = getattr(p, "para_id", "") or ""
        if pid and pid in allowed and not p.is_heading and (p.text_en or "").strip():
            ids.append(pid)
    return ids, {pid: i for i, pid in enumerate(ids)}


def merged_compile_and_translate(doi: str, *, context: str = "compile",
                                 max_tokens: int | None = None) -> dict:
    """一次请求完成 L1+L2+L3+翻译；产物与分步路径写同一批文件。

    返回 `{"status": "done", "levels": [...], "translated": n, "targets": n, "coverage": c,
    "calls": 1, "merged": True}`；不可用时抛 `MergedFallback`（调用方回退分步）。
    """
    from . import api as _api

    comp = _api._need_compiler()              # noqa: SLF001 - 与 compile_now 同一取得方式
    doc = comp._doc(doi)                      # noqa: SLF001 - 复用编译器同一取文档口径
    meta = comp._resolve_meta(doi, doc)       # noqa: SLF001
    journal_meta = comp._journal_meta(meta.journal, meta.issn, meta.eissn)  # noqa: SLF001
    target_ids, _idx = _targets(doc)
    if not target_ids:
        raise MergedFallback("无待译段落（targets=0）")

    shared, math_list = paper_context_with_math(doc)
    from .context import CTX_HEADER

    prompt = with_task(CTX_HEADER + shared,
                       merged_task(meta.model_dump(mode="json"), doc, journal_meta,
                                   target_ids=target_ids))
    llm = get_llm()
    raw = llm.complete(prompt, context=context)
    parsed = parse_merged(raw)
    cov = translation_coverage(parsed, target_ids)
    if parsed["l1"] is None:
        raise MergedFallback("合并输出缺 L1（one_liner）")
    if not parsed["l2_md"] or len(parsed["l2_md"]) < MIN_L2_CHARS:
        raise MergedFallback("合并输出缺 L2 详细笔记")
    if parsed["l3"] is None:
        raise MergedFallback("合并输出缺 L3 知识卡")
    if cov < MIN_COVERAGE:
        raise MergedFallback(f"译文覆盖率不足：{cov:.0%} < {MIN_COVERAGE:.0%}")

    # ---- 回填译文（复用分步同一套清洗/公式回填；写回目标 = 编译器取文档的同一路径）
    from .translate.pipeline import _apply_translations

    doc_json = Path(_api.shared_doc_json(doi))
    data: dict[str, Any] = json.loads(doc_json.read_text(encoding="utf-8", errors="replace"))
    paras = data.get("paragraphs") or []
    para_id_to_idx: dict[str, int] = {}
    for i, pp in enumerate(paras):
        pid = pp.get("para_id") or ""
        if pid:
            para_id_to_idx.setdefault(pid, i)
    out_map, rejected = _apply_translations({"translations": parsed["translations"]},
                                            paras, para_id_to_idx, math_list)
    for i, zh in out_map.items():
        paras[i]["text_zh"] = zh
    doc_json.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- 三级笔记（与分步路径同一批产物路径与渲染）
    note = comp._note_path(doi)               # noqa: SLF001
    details = comp._details_path(doi)         # noqa: SLF001
    wiki = comp._wiki_path(doi)               # noqa: SLF001
    note.write_text(_compile._render_note(meta, parsed["l1"], journal_meta), encoding="utf-8")
    details.write_text(parsed["l2_md"].strip() + "\n", encoding="utf-8")
    if parsed["l3"]:
        try:
            wiki.write_text(_compile._render_wiki(meta, parsed["l3"]), encoding="utf-8")
        except Exception as e:  # noqa: BLE001 - 渲染失败不拦整轮（笔记已落盘）
            logger.warning("合并调用：L3 渲染失败（忽略）：%s", e)
    comp._save_ctx(doi, "L1", _compile._ctx_from_l1(parsed["l1"]))       # noqa: SLF001
    for lvl in ("L1", "L2", "L3"):
        comp._mark_done(doi, lvl)                                        # noqa: SLF001
    comp._index_paper_notes(doi)                                         # noqa: SLF001
    return {"status": "done", "doi": doi, "levels": ["L1", "L2", "L3"],
            "translated": len(out_map), "targets": len(target_ids), "rejected": rejected,
            "coverage": round(cov, 3), "calls": 1, "merged": True,
            "concepts": (parsed["l1"] or {}).get("concepts", [])}
