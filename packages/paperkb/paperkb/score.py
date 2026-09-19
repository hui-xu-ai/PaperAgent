# -*- coding: utf-8 -*-
"""\u4ef7\u503c\u8bc4\u5206\uff08\u7f16\u8bd1\u9009\u62e9\u6027\u4f9d\u636e\uff0cKB-DESIGN v0.6 \u00a76.2\uff09\u3002

value_score = 0.25\u00d7IF\u6863 + 0.20\u00d7\u88ab\u5f15 + 0.30\u00d7AI\u4ef7\u503c + 0.15\u00d7\u4e3b\u9898 + 0.10\u00d7\u5e74\u4efd
- \u5f52\u4e00\u5316\u5230 0-5\uff1b\u7f3a\u5931\u5b57\u6bb5\u6743\u91cd\u7b49\u6bd4\u653e\u5927\uff08\u4e0d\u60e9\u7f5a\u65e0 IF/\u65e0\u88ab\u5f15\u6587\u732e\uff09
- \u7b49\u7ea7\u95e8\u69db\uff1aL3 \u2265 4.0\uff1bL2 \u2265 2.5\uff1bL1 \u5168\u505a
- AI \u4ef7\u503c\u8bc4\u5206\u7531 L1+L2 \u7f16\u8bd1\u65f6\u4ea7\u51fa\uff08\u96f6\u989d\u5916\u6210\u672c\uff09\uff1b\u4e3b\u9898\u8bc4\u5206\u540c\u6b65\u4ea7\u51fa
"""
from __future__ import annotations

import math
from datetime import datetime

WEIGHTS = {"if": 0.25, "cited": 0.20, "ai_value": 0.30, "topic": 0.15, "year": 0.10}
L3_THRESHOLD = 4.0
L2_THRESHOLD = 2.5

_QUARTILE_MAP = {"Q1": 4, "Q2": 3, "Q3": 2, "Q4": 1}


def _if_rank(journal_info: dict | None) -> tuple[float, bool]:
    """\u8fd4\u56de (IF \u6863\u5206 0-5, \u662f\u5426\u53ef\u7528)\u3002Q1=4..Q4=1\uff1b\u65e0 Quartile \u7528 JIF \u5206\u6863\uff1b\u4e2d\u79d1\u9662 1 \u533a +1\u3002"""
    if not journal_info:
        return 0.0, False
    jcr = journal_info.get("jcr") or {}
    cas = journal_info.get("cas") or {}
    q = (jcr.get("quartile") or "").upper()
    base = _QUARTILE_MAP.get(q)
    if base is None:
        try:
            jif = float(jcr.get("jif") or 0)
        except (TypeError, ValueError):
            jif = 0.0
        base = 4 if jif >= 10 else 3 if jif >= 5 else 2 if jif >= 2 else 1 if jif > 0 else 0
    if base == 0:
        return 0.0, False
    bonus = 1 if str(cas.get("zone") or "") == "1" else 0
    return float(min(5, base + bonus)), True


def _cited_norm(times_cited: int | None, has_bib: bool) -> tuple[float, bool]:
    """log \u7f29\u653e\uff1a316 \u6b21\u88ab\u5f15 \u2192 1.0\uff1b\u65e0 bib\uff08\u6570\u636e\u7f3a\u5931\uff09\u2192 \u4e0d\u53ef\u7528\u3002"""
    if not has_bib:
        return 0.0, False
    return min(1.0, math.log10(max(1, int(times_cited or 0)) + 1) / 2.5), True


def _ai_value(score: float | None) -> tuple[float, bool]:
    """AI \u4ef7\u503c\u8bc4\u5206\uff080-5\uff09\uff1a\u7531 L1+L2 \u7f16\u8bd1\u65f6\u4ea7\u51fa\u3002\u672a\u8bc4\u5206\uff08None\uff09\u2192 \u4e0d\u53ef\u7528\u3002"""
    if score is None:
        return 0.0, False
    return float(min(5.0, max(0.0, score))), True


def _topic_score(score: float | None) -> tuple[float, bool]:
    """\u4e3b\u9898\u5339\u914d\u8bc4\u5206\uff080-1\uff09\uff1aAI \u57fa\u4e8e\u7528\u6237\u4e3b\u9898\u8868\u6253\u5206\u3002\u672a\u8bc4\u5206\uff08None\uff09\u2192 \u4e0d\u53ef\u7528\u3002"""
    if score is None:
        return 0.0, False
    return float(min(1.0, max(0.0, score))), True


def _year_weight(year: str, current_year: int) -> tuple[float, bool]:
    """\u8fd1 3 \u5e74 = 1.0\uff1b3-10 \u5e74 = 0.7\uff1b>10 \u5e74 = 0.4\uff1b\u65e0\u5e74\u4efd = \u4e0d\u53ef\u7528\u3002"""
    try:
        y = int(str(year)[:4])
    except (TypeError, ValueError):
        return 0.0, False
    diff = max(0, current_year - y)
    if diff <= 3:
        return 1.0, True
    if diff <= 10:
        return 0.7, True
    return 0.4, True


def value_score(meta, journal_info: dict | None = None,
                has_bib: bool = True,
                current_year: int | None = None) -> dict:
    """\u8ba1\u7b97\u4ef7\u503c\u5206\u4e0e\u7f16\u8bd1\u7b49\u7ea7\u3002

    \u8fd4\u56de\uff1a{score(0-5), level(L1/L2/L3), parts:{if,cited,ai_value,topic,year,available}}
    AI \u8bc4\u5206\u4ece meta \u5bf9\u8c61\u7684 ai_value_score / topic_score \u5c5e\u6027\u8bfb\u53d6\uff08\u7531\u7f16\u8bd1\u4ea7\u51fa\u5199\u5165\uff09\u3002
    """
    cy = current_year or datetime.now().year

    if_score, if_ok = _if_rank(journal_info)
    cited, cited_ok = _cited_norm(getattr(meta, "times_cited", 0), has_bib)
    ai_val, ai_ok = _ai_value(getattr(meta, "ai_value_score", None))
    topic_val, topic_ok = _topic_score(getattr(meta, "topic_score", None))
    year_w, year_ok = _year_weight(getattr(meta, "year", ""), cy)

    parts = {
        "if": {"value": round(if_score, 2), "available": if_ok},
        "cited": {"value": round(cited, 2), "available": cited_ok},
        "ai_value": {"value": round(ai_val, 2), "available": ai_ok},
        "topic": {"value": round(topic_val, 2), "available": topic_ok},
        "year": {"value": round(year_w, 2), "available": year_ok},
    }
    available_w = sum(w for k, w in WEIGHTS.items() if parts[k]["available"])
    if available_w <= 0:
        return {"score": 0.0, "level": "L1", "parts": parts}
    norm = sum(WEIGHTS[k] * parts[k]["value"] / (5.0 if k in ("if", "ai_value") else 1.0)
               for k in WEIGHTS if parts[k]["available"])
    score5 = norm / available_w * 5.0
    score5 = round(min(5.0, max(0.0, score5)), 2)
    # L3 门槛：分数达标 **且** AI 评分必须可用（AI 没读过全文的文章不应触发深度编译）
    ai_available = parts["ai_value"]["available"]
    level = "L3" if (score5 >= L3_THRESHOLD and ai_available) else (
        "L2" if score5 >= L2_THRESHOLD else "L1")
    return {"score": score5, "level": level, "parts": parts}
