# -*- coding: utf-8 -*-
"""对话式一次流转（**非 DeepSeek 官方供应商**）：编译 → 翻译 共用同一条 messages。

用户 2026-09-13 决策：编译（L1，或 L1+L2 写进同一条 user）与紧随其后的翻译**放在同一条对话**里，
让第 2 次请求的前缀命中供应商缓存（实测智谱第 2 轮 cached≈18.7k）；**用户针对文献提问时不受影响**
（提问路径仍是"重新上传全文"的独立请求）。

流程（一个 flow = 一篇文献的一次编译+翻译）：
```
messages = [ system(全文前缀) ]
  ├─ user(笔记任务 notes_task(levels))        → 解析 {"l1":…, "l2_md":…}
  │   产物：_note.md / _details.md（+ ctx、mark_done）
  └─ user(翻译任务 translate_task(ids))       → 解析 {"translations":[…]}（payload 含上一步 assistant 回复）
      产物：library/<rid>/document.json 的 text_zh
```
不可用（解析失败/覆盖不足/LLM 不支持多消息）→ 抛 `ConvoFallback`，由调用方回退既有单发路径。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

from . import compile as _compile
from .context import CTX_HEADER, context_paragraphs, paper_context_with_math, with_task
from .llm import get_llm
from .merged import notes_task, parse_notes, parse_translations, translate_task, translation_coverage

logger = logging.getLogger(__name__)

MIN_COVERAGE = 0.9


class ConvoFallback(RuntimeError):
    """对话式路径不可用 → 调用方回退既有单发/分步实现。"""


def _supports_messages(llm) -> bool:
    return callable(getattr(llm, "chat_messages", None))


def _call(llm, messages: list[dict], context: str) -> str:
    """一次对话请求（要求适配器支持 messages 列表）。"""
    fn: Callable = getattr(llm, "chat_messages")
    return fn(messages, context=context) or ""


def targets_of(doc) -> list[str]:
    """待译 para_id（**与 run_translate 同一判据**：模型看得到原文、非标题、text_en 非空）。"""
    allowed = {p.para_id for p in context_paragraphs(doc) if getattr(p, "para_id", "")}
    return [p.para_id for p in doc.paragraphs
            if (getattr(p, "para_id", "") or "") in allowed
            and not p.is_heading and (p.text_en or "").strip()]


def conversation_compile(key: str, *, levels: tuple[str, ...] = ("L1",),
                         translate: bool = True, context: str = "compile") -> dict:
    """对话式：一次流转内完成"笔记（L1/L1+L2）+ 翻译"。

    `levels`：按价值分判定的级别（含 "L2" 时把 L2 写进同一条 user；"L3" 单独走既有单发路径）。
    返回 `{"status","doi","levels","notes","translated","targets","coverage","calls"}`；
    LLM 不支持多消息 / 解析失败 / 覆盖率不足（且无回退可用）→ `ConvoFallback`。
    """
    from . import api as _api

    llm = get_llm()
    if not _supports_messages(llm):
        raise ConvoFallback("当前 LLM 适配器不支持 messages 列表（无法对话式）")

    comp = _api._need_compiler()               # noqa: SLF001
    doc = comp._doc(key)                       # noqa: SLF001
    meta = comp._resolve_meta(key, doc)        # noqa: SLF001
    journal_meta = comp._journal_meta(meta.journal, meta.issn, meta.eissn)  # noqa: SLF001
    meta_json = meta.model_dump(mode="json")
    block, math_list = paper_context_with_math(doc)
    system = CTX_HEADER + block
    ids = targets_of(doc)
    lv = tuple(levels or ("L1",))
    calls = 0

    # ---------- 第 1 次：笔记（L1 / L1+L2）
    messages: list[dict] = [{"role": "system", "content": system},
                            {"role": "user", "content": notes_task(meta_json, doc, journal_meta, lv)}]
    raw_notes = _call(llm, messages, context)
    calls += 1
    notes = parse_notes(raw_notes)
    if notes["l1"] is None:
        raise ConvoFallback("对话式笔记输出缺 L1（one_liner）")
    want_l2 = "L2" in lv
    if want_l2 and len(notes["l2_md"] or "") < 20:   # 与 compile._l2_output_ok 的长度口径一致
        raise ConvoFallback("对话式笔记输出缺 L2 或过短")

    note_path = comp._note_path(key)            # noqa: SLF001
    note_path.write_text(_compile._render_note(meta, notes["l1"], journal_meta), encoding="utf-8")
    comp._save_ctx(key, "L1", _compile._ctx_from_l1(notes["l1"]))       # noqa: SLF001
    comp._mark_done(key, "L1")                                          # noqa: SLF001
    levels_done = ["L1"]
    if want_l2:
        comp._details_path(key).write_text(notes["l2_md"].strip() + "\n", encoding="utf-8")  # noqa: SLF001
        comp._save_ctx(key, "L2", notes["l2_md"][:4000])                # noqa: SLF001
        comp._mark_done(key, "L2")                                      # noqa: SLF001
        levels_done.append("L2")

    # ---------- 第 2 次：翻译（同一对话，前缀命中缓存）
    out: dict[str, Any] = {"status": "done", "doi": key, "levels": levels_done,
                           "notes": True, "translated": 0, "targets": len(ids),
                           "coverage": None, "calls": calls, "conversation": True}
    if not translate or not ids:
        comp._index_paper_notes(key)                                    # noqa: SLF001
        return out
    messages = messages + [{"role": "assistant", "content": raw_notes},
                           {"role": "user", "content": translate_task(ids)}]
    raw_tr = _call(llm, messages, "translate")
    calls += 1
    trans = parse_translations(raw_tr)
    cov = translation_coverage(trans, ids)
    if cov > 0:
        _write_translations(key, trans, math_list)
    if cov < MIN_COVERAGE:
        logger.warning("对话式翻译覆盖不足：%.0f%%（%d/%d）→ 由调用方回退",
                       cov * 100, int(cov * len(ids)), len(ids))
        out.update({"translated": int(cov * len(ids)), "coverage": round(cov, 3),
                    "calls": calls, "partial": True})
        comp._index_paper_notes(key)                                    # noqa: SLF001
        raise ConvoFallback(f"对话式翻译覆盖不足 {cov:.0%}")
    out.update({"translated": len(ids), "coverage": round(cov, 3), "calls": calls})
    comp._index_paper_notes(key)                                        # noqa: SLF001
    return out


def _write_translations(key: str, trans: list[dict], math_list: list[str]) -> int:
    """把译文写回 document.json（复用分步同一套清洗/公式回填）。"""
    from . import api as _api
    from .translate.pipeline import _apply_translations

    doc_json = Path(_api.shared_doc_json(key))
    data: dict[str, Any] = json.loads(doc_json.read_text(encoding="utf-8", errors="replace"))
    paras = data.get("paragraphs") or []
    idx: dict[str, int] = {}
    for i, pp in enumerate(paras):
        pid = pp.get("para_id") or ""
        if pid:
            idx.setdefault(pid, i)
    out_map, _rejected = _apply_translations({"translations": trans}, paras, idx, math_list)
    for i, zh in out_map.items():
        paras[i]["text_zh"] = zh
    doc_json.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return len(out_map)
