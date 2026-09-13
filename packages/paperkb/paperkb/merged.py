# -*- coding: utf-8 -*-
"""智谱专用「单次调用」：把 L1 + L2 + L3 + 全文翻译合并成**一次** LLM 请求。

为什么（2026-09-13 实测，见 `.dsh-memory/project/NOTES-GLM-CACHE-20260913.md` §9）：
智谱隐式缓存在"相邻调用间隔 < ~120s"时**完全不命中**，而本应用一次流转的 4 个步骤都在
15–120s 内连着发 ⇒ 全文前缀（~19k token）被重复计价 4 次。合并成单次请求后输入 4×19k → 1×19k
（省 ~75%），且不再依赖供应商的缓存时效窗口。

设计约束（**不复制第二套判据**）：
- 四个子任务文本**直接复用**既有构造点 `compile._prompt_l1/_prompt_l2/_prompt_l3` 与
  `translate.pipeline._translate_task`（在 `TASK_MARK` 处取后缀），任务描述改动自动跟随；
- 译文回填复用 `translate.pipeline._apply_translations`（同样的清洗/公式回填）；
- 解析失败 / 截断 / 覆盖不足 ⇒ 由调用方回退到既有分步路径（本模块只负责"构造 + 解析 + 判定"）。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# 合并任务的输出信封（模型必须只回这个 JSON）
_MERGED_OUTPUT_SPEC = (
    "## 输出要求（只输出一个 JSON 对象，不要任何解释、不要 Markdown 代码围栏）\n"
    "{\n"
    '  "l1": { …第 ① 项要求的 JSON 对象… },\n'
    '  "l2_md": "第 ② 项的 Markdown 全文（用 JSON 字符串转义，\\n 换行）",\n'
    '  "l3": { …第 ③ 项要求的 JSON 对象… },\n'
    '  "translations": [ {"para_id": "P001", "zh": "第 ④ 项的译文"} ]\n'
    "}\n"
    "四个键都必须出现；translations 必须覆盖上面列出的**每一个** para_id，一个都不能少。"
)


def _task_of(prompt: str) -> str:
    """从带 marker 的完整 prompt 里取出**任务部分**（与 with_task/split_task 同一分界）。"""
    from .context import split_task

    return split_task(prompt)[1]


def merged_task(meta: dict, doc, journal_meta: str = "", l1_ctx: str = "",
                l2_ctx: str = "", target_ids: list[str] | None = None) -> str:
    """构造合并调用的 **user 消息**（system 侧仍是共享全文前缀，由调用方 with_task 拼）。

    `target_ids`：要翻译的 para_id 清单（**与 run_translate 同一判据**：只有模型看得到原文、
    非标题、text_en 非空的段落才要求翻译）。
    """
    from .compile import _prompt_l1, _prompt_l2, _prompt_l3
    from .translate.pipeline import _translate_task

    ids = [str(x) for x in (target_ids or []) if x]
    # 第 ②③ 项不携带"上一级摘要"（合并调用里 L1/L2 与它们同一次产出，尚无文本可带）；
    # 第 ② 项也不重复内联"章节片段"（正文已在 system 里，这里再塞一遍等于把 user 撑到 6.6k）。
    # 做法：传入一个**无正文段落**的同源 PaperDoc（任务文本仍来自既有构造点，不复制第二套判据）。
    _L1_PLACEHOLDER = "(同一次调用内，请以上面 ① 的产出为准)"
    try:
        _doc_task_only = type(doc)(doi=getattr(doc, "doi", ""), title=getattr(doc, "title", ""))
        _doc_task_only.sections = list(getattr(doc, "sections", []) or [])
    except Exception:  # noqa: BLE001 - 构造不出来就退回原 doc（只是 user 会长一点）
        _doc_task_only = doc
    parts = [
        "## 合并任务（一次完成，严格按顺序思考，只在最后输出一个 JSON）",
        "以上方论文全文为唯一依据，依次完成下面四项。**第 ②③ 项要引用第 ① 项已产出的内容**"
        "（不要重复全景，只补充细节）；第 ④ 项只翻译列出的段落。\n",
        "### ① L1 核心笔记（JSON）",
        _task_of(_prompt_l1(meta, doc, journal_meta)),
        "\n### ② L2 详细笔记（Markdown）",
        _task_of(_prompt_l2(meta, _doc_task_only, _L1_PLACEHOLDER)),
        "\n### ③ L3 深度知识卡（JSON）",
        _task_of(_prompt_l3(meta, doc, _L1_PLACEHOLDER, _L1_PLACEHOLDER)),
        "\n### ④ 全文逐段翻译（JSON）",
        _task_of(_translate_task(ids)),
        "\n" + _MERGED_OUTPUT_SPEC,
    ]
    return "\n".join(parts)


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


def _strip_fence(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    return t.strip()


def parse_merged(raw: str) -> dict[str, Any]:
    """解析合并输出 → `{"l1": dict|None, "l2_md": str, "l3": dict|None, "translations": list}`。

    宽容策略：整体 JSON 解析失败时，退化为"按键抓取片段"（模型偶尔会输出四段独立 JSON）；
    四项各自独立判定，调用方按"缺什么补什么"决定回退范围。
    """
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
        trs = obj.get("translations")
        if isinstance(trs, list):
            out["translations"] = [x for x in trs if isinstance(x, dict)]
        if out["l1"] or out["l2_md"] or out["l3"] or out["translations"]:
            return out
    # 退化：分别找 "l1"/"translations" 段
    logger.warning("合并输出整体解析失败，尝试分段抓取（len=%d）", len(text))
    m = re.search(r'"l1"\s*:\s*', text)
    if m:
        sub = _balanced_json(text[m.end():])
        if isinstance(sub, dict) and sub.get("one_liner"):
            out["l1"] = sub
    m = re.search(r'"translations"\s*:\s*\[', text)
    if m:
        seg = text[m.end() - 1:]
        depth = 0
        for i, ch in enumerate(seg):
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    try:
                        arr = json.loads(seg[:i + 1])
                        if isinstance(arr, list):
                            out["translations"] = [x for x in arr if isinstance(x, dict)]
                    except json.JSONDecodeError:
                        pass
                    break
    return out


def translation_coverage(parsed: dict, target_ids: list[str]) -> float:
    """译文覆盖率（按 para_id 计，去重后）。"""
    ids = {str(x) for x in (target_ids or []) if x}
    if not ids:
        return 1.0
    got = {str(t.get("para_id")) for t in (parsed.get("translations") or [])
           if str(t.get("para_id") or "") in ids and (t.get("zh") or "").strip()}
    return len(got) / len(ids)
