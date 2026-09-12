#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: skill/src/paperparse/core/ai_review.py
功能: AI 审查学习闭环（P-ENHANCE R05 / S7.5）：
      - build_review_items：重建待审清单（latex 语法异常 / mid-join 词中粘连候选 /
        低置信规则命中；每项含精确 target 与上下文）
      - review_items：清单 → AIProvider 批量判断（JSON 输出）→ 应用 fix / 规则候选
      - mine_rule：AI 确认的修复 → learned 级规则候选（confidence 0.7 起步）
      - apply_corrections：人工反馈（feedback/corrections.json）→ user 级规则
      AI 只改确认项；原文 before 保留于审计；规则候选入库前校验。
对外接口: build_review_items / review_items / mine_rule / apply_corrections / parse_decisions
版本: v0.1.0 (2026-08-22)
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from paperparse.core.rule_engine import apply_spacing_local, RuleReport
from paperparse.core.rule_library import RULE_CATEGORIES, add_rule
from paperparse.llm.client import get_ai

__all__ = ["build_review_items", "review_items", "mine_rule",
           "apply_corrections", "parse_decisions", "MAX_ITEMS"]

MAX_ITEMS = 50
CTX_CHARS = 300


def _ctx_around(text: str, target: str, width: int = CTX_CHARS) -> str:
    """[局部] target 在 text 中的上下文（前后 width/2 字符）"""
    idx = text.find(target)
    if idx < 0:
        return text[:width]
    lo = max(0, idx - width // 2)
    hi = min(len(text), idx + len(target) + width // 2)
    return text[lo:hi]


def build_review_items(doc: Any, rules_dir: str | Path = None,
                       enable_mid_join: bool = False,
                       min_conf: float = 0.0) -> list[dict]:
    """[全局] 重建待审清单（精确 target，不依赖 rule_report 截断字段）

    来源：
      1) latex 语法异常（check_document_latex：kind/detail/near）
      2) mid-join 词中粘连候选（apply_spacing_local 检测逻辑；**默认关闭**——
         R07 实测 2742 候选全为合法词（reinforced/actuators 等），真实粘连≈0，
         AI 审查 30 项全 ignore；遇真粘连场景可显式启用）
      3) 低置信规则命中（confidence<min_conf 且未 auto_apply 的规则重新匹配）
    """
    from pathlib import Path
    items: list[dict] = []
    n = 0

    def _add(kind, category, para_id, target, context, hint=""):
        nonlocal n
        n += 1
        items.append({"id": n, "kind": kind, "category": category,
                      "para_id": para_id, "target": target,
                      "context": context, "local_hint": hint})

    # 1) latex 语法异常
    try:
        from paperparse.core.latex_check import check_document_latex
        for err in check_document_latex(doc):
            if n >= MAX_ITEMS:
                return items
            near = err.get("near", "") or ""
            _add("latex_syntax", "latex", err.get("para_id"), near[:80], near,
                 "detail: %s" % err.get("detail", ""))
    except Exception:
        pass

    # 2) mid-join 词中粘连候选（复用检测逻辑的候选提取，本地证据可选）
    if enable_mid_join:
        from paperparse.core.rule_engine import find_mid_joins
        for p in doc.paragraphs:
            if n >= MAX_ITEMS:
                return items
            if p.is_heading or not (p.text_en or ""):
                continue
            t = p.text_en
            for w in sorted(find_mid_joins(t)):
                if n >= MAX_ITEMS:
                    return items
                _add("mid_join", "spacing", p.para_id, w, _ctx_around(t, w),
                     "候选词（XofY 型）；本地对照需 pdf 上下文")

    # 3) 低置信规则命中（重新匹配）
    try:
        from paperparse.core.rule_library import load_rules
        from paperparse.core.rule_engine import _ordered_rules
        rd = Path(rules_dir) if rules_dir else None
        for rule in _ordered_rules(load_rules(rd)):
            if rule.get("auto_apply", False) or float(rule.get("confidence", 0)) >= min_conf:
                continue
            if rule.get("pattern_type") != "regex" or not rule.get("enabled", True):
                continue
            try:
                pat = re.compile(rule["pattern"])
            except re.error:
                continue
            for p in doc.paragraphs:
                if n >= MAX_ITEMS:
                    return items
                t = p.text_en or ""
                m = pat.search(t)
                if m:
                    _add("low_conf_rule", rule.get("category", "other"), p.para_id,
                         m.group(0), _ctx_around(t, m.group(0)),
                         "规则 %s: %s" % (rule.get("rule_id"), rule.get("note", "")))
    except Exception:
        pass
    return items


_PROMPT = """你是 PDF 解析结果审查员。下面是从英文论文解析文本中提取的待确认异常清单
（MinerU OCR/LaTeX 识别可能出错）。请逐条判断并输出 JSON 数组（不要其他文字）：

[
 {"index": 1, "action": "fix", "corrected": "替换后的正确文本", "note": "简短理由"},
 {"index": 2, "action": "ignore", "note": "这是合法词，无需修改"}
]

规则：
- 每项必须给出 index 与 action（fix 或 ignore）。
- action=fix 时 corrected 必须是该项 target 的完整替换文本（只改 target 本身）。
- 只处理能确定的错误；不确定或合法文本一律 ignore（宁缺毋滥，防误伤）。
- 单词粘连（如 XofY 应为 "X of Y"）、LaTeX 乱码、明显 OCR 错字可修。
- 输出必须是合法 JSON 数组，最多 %(max_items)d 条。

待审清单：
%(items)s
"""


def parse_decisions(raw: str) -> list[dict]:
    """[全局] 容错解析 AI 输出 JSON（去 fence/前后文本；逐条 try）"""
    text = (raw or "").strip()
    # 去 markdown fence
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE)
    start = text.find("[")
    end = text.rfind("]")
    decisions: list[dict] = []
    if 0 <= start < end:
        try:
            data = json.loads(text[start:end + 1])
        except Exception:
            data = []
        for d in data if isinstance(data, list) else []:
            if isinstance(d, dict) and d.get("index") is not None:
                decisions.append(d)
    return decisions


def review_items(items: list[dict], doc: Any, ai=None,
                 max_items: int = MAX_ITEMS) -> dict:
    """[全局] 待审清单 → AI 判断 → 应用 fix（按 target 精确替换段落）

    返回 {"fixed": N, "candidates": [规则候选], "ignored": N, "raw": AI 原文}
    AI 只改 action=fix 且 corrected 非空的项；target 在段落中精确替换。
    """
    if not items:
        return {"fixed": 0, "candidates": [], "ignored": 0, "raw": ""}
    items = items[:max_items]
    ai = ai or get_ai()
    lines = []
    for it in items:
        lines.append("## %d) [%s/%s] 段 %s" % (it["id"], it["kind"], it["category"],
                                               it["para_id"]))
        lines.append("target: %s" % it["target"])
        lines.append("context: %s" % it["context"][:CTX_CHARS])
        if it.get("local_hint"):
            lines.append("hint: %s" % it["local_hint"])
    prompt = _PROMPT % {"max_items": max_items, "items": "\n".join(lines)}
    try:
        raw = ai.complete(prompt)
    except Exception as exc:
        return {"fixed": 0, "candidates": [], "ignored": len(items),
                "raw": "", "error": str(exc)[:200]}
    decisions = parse_decisions(raw)
    by_id = {it["id"]: it for it in items}
    fixed = 0
    ignored = 0
    candidates: list[dict] = []
    for d in decisions:
        it = by_id.get(int(d.get("index", -1)))
        if it is None:
            continue
        action = str(d.get("action", "ignore"))
        corrected = str(d.get("corrected") or "").strip()
        if action == "fix" and corrected and corrected != it["target"]:
            applied = _replace_target(doc, it["para_id"], it["target"], corrected)
            if applied:
                fixed += 1
                cand = mine_rule(it, corrected, d.get("note", ""))
                if cand:
                    candidates.append(cand)
                continue
        ignored += 1
    return {"fixed": fixed, "candidates": candidates, "ignored": ignored,
            "raw": raw[:2000]}


def _replace_target(doc: Any, para_id: str, target: str, corrected: str) -> bool:
    """[局部] 按 para_id + target 精确替换段落文本（未找到/多义则不替换）"""
    for p in doc.paragraphs:
        if p.para_id != para_id or not (p.text_en or ""):
            continue
        t = p.text_en
        if t.count(target) == 1:
            p.text_en = t.replace(target, corrected)
            return True
        if t.count(target) > 1:
            # 多义：用 corrected 提示校验（AI 给 corrected 可能是全 target 替换）——
            # 保守：不替换（避免误伤），返回 False
            return False
    return False


def mine_rule(item: dict, corrected: str, note: str = "") -> dict | None:
    """[全局] AI 确认的修复 → learned 规则候选（精确替换；confidence 0.7 起步）

    返回规则 dict（未入库；由调用方校验后 add_rule）或 None（目标太短/无意义）。
    """
    target = (item.get("target") or "").strip()
    if len(target) < 3 or target == corrected:
        return None
    category = item.get("category") if item.get("category") in RULE_CATEGORIES else "other"
    if category == "other":
        category = "spacing" if item.get("kind") == "mid_join" else "latex"
    return {
        "category": category,
        "pattern_type": "regex",
        "pattern": re.escape(target),
        "replacement": corrected.replace("\\", "\\\\"),
        "confidence": 0.7,
        "auto_apply": False,
        "source": "ai_review",
        "note": ("AI 审查确认修复: %s" % note)[:120],
        "evidence": [{"paper": "", "before": target, "after": corrected,
                      "review": "ai"}],
    }


def apply_corrections(rules_dir: str | Path, corrections: list[dict],
                      paper: str = "") -> dict:
    """[全局] 人工反馈 → user 级规则（最高优先）

    参数:
        rules_dir: 规则库根
        corrections: corrections.json 的 corrections 列表
            [{para_id?, location, original, corrected, category, note}]
        paper: 论文标识（DOI）
    返回:
        {"added": N, "conflicts": [...]}
    """
    from pathlib import Path
    rd = Path(rules_dir)
    added, conflicts = 0, []
    for c in corrections:
        original = str(c.get("original") or "").strip()
        corrected = str(c.get("corrected") or "").strip()
        if not original or not corrected or original == corrected:
            continue
        cat = str(c.get("category") or "other")
        if cat not in RULE_CATEGORIES:
            cat = "other"
        rule = {
            "category": cat if cat != "other" else "variable",
            "pattern_type": "regex",
            "pattern": re.escape(original),
            "replacement": corrected.replace("\\", "\\\\"),
            "confidence": 1.0,
            "auto_apply": True,
            "source": "user_correction",
            "note": ("用户纠正: %s" % (c.get("note") or ""))[:120],
            "evidence": [{"paper": paper, "before": original, "after": corrected,
                          "review": "user"}],
            "scope": "paper:%s" % paper if paper else "global",
        }
        try:
            add_rule(rd, rule, level="user")
            added += 1
        except ValueError as e:
            conflicts.append({"original": original, "reason": str(e)})
    return {"added": added, "conflicts": conflicts}
