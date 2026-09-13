# -*- coding: utf-8 -*-
"""单次请求内合并多级任务（**对话方式**的支持模块）。

用户决策（2026-09-13）：非 DeepSeek 官方供应商走"连续对话"——一次流转内保持同一条 messages：
  第 1 次：system(全文前缀) + user(笔记任务：按价值分要 L1 就 L1、要 L2 就把 L1+L2 写在同一条 user)
  第 2 次：在同一对话后追加 user(翻译任务)
第 2 次请求的前缀（≈19k）因此能命中缓存（实测第 2/3 轮 cached≈18.7k/20.2k）。

本模块只做**纯函数**：任务文本构造 + 输出解析 + 覆盖判定（不碰网络/磁盘，便于单测）。
约束（**不复制第二套判据**）：任务描述一律取自既有构造点
`compile._prompt_l1/_prompt_l2/_prompt_l3` 与 `translate.pipeline._translate_task`（按 `TASK_MARK` 取后缀）。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_L1_PLACEHOLDER = "(同一次调用内，请以上面 ① 的产出为准)"

# 笔记任务输出信封（L1-only）
_NOTES_SPEC_L1 = (
    "## 输出要求（只输出一个 JSON 对象，不要解释、不要 Markdown 代码围栏）\n"
    "{\n"
    '  "l1": { …① 要求的 JSON 对象… }\n'
    "}\n"
    "l1 必须含 one_liner。"
)

# 笔记任务输出信封（L1 + L2）
_NOTES_SPEC_L1L2 = (
    "## 输出要求（只输出一个 JSON 对象，不要解释、不要 Markdown 代码围栏）\n"
    "{\n"
    '  "l1": { …① 要求的 JSON 对象… },\n'
    '  "l2_md": "② 的 Markdown 全文（JSON 字符串转义，\\n 换行）"\n'
    "}\n"
    "两个键都要出现，l1 必须含 one_liner。"
)

# 「笔记 + 翻译」一次请求的输出信封（兼容入口用）
_MERGED_SPEC = (
    "## 输出要求（只输出一个 JSON 对象，不要解释、不要代码围栏）\n"
    "{\n"
    '  "l1": { …①… },\n  "l2_md": "…②…",\n  "l3": { …③… },\n'
    '  "translations": [ {"para_id": "P001", "zh": "…④…"} ]\n}\n'
    "出现的键必须齐全；translations 必须覆盖上面列出的**每一个** para_id。"
)


def _task_of(prompt: str) -> str:
    """从带 marker 的完整 prompt 里取出**任务部分**（与 with_task/split_task 同一分界）。"""
    from .context import split_task

    return split_task(prompt)[1]


def _doc_task_only(doc):
    """同源但**不带正文段落**的 PaperDoc（任务文本里不再内联章节片段，正文已在 system）。"""
    try:
        d = type(doc)(doi=getattr(doc, "doi", ""), title=getattr(doc, "title", ""))
        d.sections = list(getattr(doc, "sections", []) or [])
        return d
    except Exception:  # noqa: BLE001 - 构造不出来就退回原 doc（user 会长一点，不影响正确性）
        return doc


def notes_task(meta: dict, doc, journal_meta: str = "", levels: tuple[str, ...] = ("L1",)) -> str:
    """"笔记任务"的 user 文本：`levels` 决定是否把 L2 写在同一条 user 里（共用一次思考）。

    - `("L1",)` → 输出 `{"l1": {...}}`
    - `("L1","L2")` → 输出 `{"l1": {...}, "l2_md": "..."}`
    """
    from .compile import _prompt_l1, _prompt_l2

    lv = tuple(levels or ("L1",))
    parts = ["## 本次任务：论文知识编译（严格按顺序，只在最后输出一个 JSON）"]
    if "L2" in lv:
        parts.append("先完成 ①，再**基于 ① 的产出**完成 ②（不要重复全景，只补充章节级细节）。\n")
    parts += ["### ① L1 核心笔记（JSON）", _task_of(_prompt_l1(meta, doc, journal_meta))]
    if "L2" in lv:
        parts += ["\n### ② L2 详细笔记（Markdown）",
                  _task_of(_prompt_l2(meta, _doc_task_only(doc), _L1_PLACEHOLDER))]
    parts.append("\n" + (_NOTES_SPEC_L1L2 if "L2" in lv else _NOTES_SPEC_L1))
    return "\n".join(parts)


def translate_task(target_ids: list[str]) -> str:
    """"翻译任务"的 user 文本（追加到同一对话的最后一条）。"""
    from .translate.pipeline import _translate_task

    return _task_of(_translate_task([str(x) for x in (target_ids or []) if x]))


def merged_task(meta: dict, doc, journal_meta: str = "", l1_ctx: str = "",
                l2_ctx: str = "", target_ids: list[str] | None = None,
                levels: tuple[str, ...] = ("L1", "L2", "L3")) -> str:
    """**兼容入口**（保留 2026-09-13 早先的"笔记+翻译一次请求"形状，非对话供应商兜底用）。"""
    from .compile import _prompt_l1, _prompt_l2, _prompt_l3

    ids = [str(x) for x in (target_ids or []) if x]
    lv = tuple(levels or ("L1",))
    parts = ["## 合并任务（一次完成，严格按顺序思考，只在最后输出一个 JSON）",
             "以上方论文全文为唯一依据，依次完成下面各项。**②③ 要引用 ① 的产出**"
             "（不要重复全景，只补充细节）；翻译项只翻译列出的段落。\n"]
    if "L1" in lv:
        parts += ["### ① L1 核心笔记（JSON）", _task_of(_prompt_l1(meta, doc, journal_meta))]
    if "L2" in lv:
        parts += ["\n### ② L2 详细笔记（Markdown）",
                  _task_of(_prompt_l2(meta, _doc_task_only(doc), _L1_PLACEHOLDER))]
    if "L3" in lv:
        parts += ["\n### ③ L3 深度知识卡（JSON）",
                  _task_of(_prompt_l3(meta, doc, _L1_PLACEHOLDER, _L1_PLACEHOLDER))]
    if ids:
        parts += ["\n### ④ 全文逐段翻译（JSON）", translate_task(ids)]
    parts.append("\n" + _MERGED_SPEC)
    return "\n".join(parts)


# ---------------------------------------------------------------- 解析

def _balanced_json(text: str) -> dict | None:
    """从输出里平衡提取第一个 `{...}` 对象（容忍代码围栏/前后解释文字）。"""
    start = text.find("{")
    while start >= 0:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        return obj if isinstance(obj, dict) else None
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


def _balanced_array(text: str, at: int) -> list | None:
    depth = 0
    for i in range(at, len(text)):
        ch = text[i]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                try:
                    arr = json.loads(text[at:i + 1])
                    return arr if isinstance(arr, list) else None
                except json.JSONDecodeError:
                    return None
    return None


def _strip_fence(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    return t.strip()


def parse_notes(raw: str) -> dict[str, Any]:
    """解析"笔记任务"输出 → `{"l1": dict|None, "l2_md": str}`。"""
    out: dict[str, Any] = {"l1": None, "l2_md": ""}
    text = _strip_fence(raw or "")
    obj = _balanced_json(text)
    if isinstance(obj, dict):
        l1 = obj.get("l1")
        if isinstance(l1, str):
            l1 = _balanced_json(l1)
        if isinstance(l1, dict) and l1.get("one_liner"):
            out["l1"] = l1
        l2 = obj.get("l2_md") or obj.get("l2")
        if isinstance(l2, str):
            out["l2_md"] = _strip_fence(l2)
        if out["l1"] or out["l2_md"]:
            return out
    logger.warning("笔记输出解析失败，尝试单段抓取（len=%d）", len(text))
    m = re.search(r'"l1"\s*:\s*', text)
    if m:
        sub = _balanced_json(text[m.end():])
        if isinstance(sub, dict) and sub.get("one_liner"):
            out["l1"] = sub
    return out


def parse_translations(raw: str) -> list[dict]:
    """解析"翻译任务"输出 → `[{"para_id","zh"}, ...]`（容忍围栏与前后解释）。"""
    text = _strip_fence(raw or "")
    obj = _balanced_json(text)
    if isinstance(obj, dict) and isinstance(obj.get("translations"), list):
        return [x for x in obj["translations"] if isinstance(x, dict)]
    m = re.search(r'"translations"\s*:\s*\[', text)
    if m:
        arr = _balanced_array(text, m.end() - 1)
        if isinstance(arr, list):
            return [x for x in arr if isinstance(x, dict)]
    logger.warning("翻译输出解析失败（len=%d）", len(text))
    return []


def parse_merged(raw: str) -> dict[str, Any]:
    """解析"笔记+翻译一次请求"输出 → `{"l1","l2_md","l3","translations"}`（兼容入口）。"""
    out: dict[str, Any] = {"l1": None, "l2_md": "", "l3": None, "translations": []}
    text = _strip_fence(raw or "")
    obj = _balanced_json(text)
    if isinstance(obj, dict):
        l1 = obj.get("l1")
        if isinstance(l1, str):
            l1 = _balanced_json(l1)
        if isinstance(l1, dict) and l1.get("one_liner"):
            out["l1"] = l1
        l2 = obj.get("l2_md") or obj.get("l2")
        if isinstance(l2, str):
            out["l2_md"] = _strip_fence(l2)
        l3 = obj.get("l3")
        if isinstance(l3, str):
            l3 = _balanced_json(l3)
        if isinstance(l3, dict) and (l3.get("wiki") or l3.get("summary")):
            out["l3"] = l3
        if isinstance(obj.get("translations"), list):
            out["translations"] = [x for x in obj["translations"] if isinstance(x, dict)]
    if out["l1"] is None or not out["l2_md"]:
        notes = parse_notes(raw)
        out["l1"] = out["l1"] or notes["l1"]
        out["l2_md"] = out["l2_md"] or notes["l2_md"]
    if not out["translations"]:
        out["translations"] = parse_translations(raw)
    return out


def translation_coverage(trans: list[dict], target_ids: list[str]) -> float:
    """译文覆盖率（按 para_id 去重计；给回退判据用）。"""
    ids = {str(x) for x in (target_ids or []) if x}
    if not ids:
        return 1.0
    got = {str(t.get("para_id")) for t in (trans or [])
           if str(t.get("para_id") or "") in ids and (t.get("zh") or "").strip()}
    return len(got) / len(ids)
