# -*- coding: utf-8 -*-
"""价值评分（编译选择性依据，KB-DESIGN v0.6 §6.2）。

value_score = 0.35×IF档 + 0.25×被引 + 0.2×主题匹配 + 0.1×⭐ + 0.1×年份
- 归一化到 0-5；缺失字段权重等比放大（不惩罚无 IF/无被引文献）
- 等级门槛：L3 ≥ 4.0 或 ⭐；L2 ≥ 2.5；L1 全做（L1 由编译队列保证，此处只管 L2/L3）
"""
from __future__ import annotations

import math
from datetime import datetime

WEIGHTS = {"if": 0.35, "cited": 0.25, "topic": 0.2, "star": 0.1, "year": 0.1}
L3_THRESHOLD = 4.0
L2_THRESHOLD = 2.5

_QUARTILE_MAP = {"Q1": 4, "Q2": 3, "Q3": 2, "Q4": 1}


def _if_rank(journal_info: dict | None) -> tuple[float, bool]:
    """返回 (IF 档分 0-5, 是否可用)。Q1=4..Q4=1；无 Quartile 用 JIF 分档；中科院 1 区 +1。"""
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
    """log 缩放：316 次被引 → 1.0；无 bib（数据缺失）→ 不可用。"""
    if not has_bib:
        return 0.0, False
    return min(1.0, math.log10(max(1, int(times_cited or 0)) + 1) / 2.5), True


def _topic_match(meta, topics: list[str]) -> tuple[float, bool]:
    """主题匹配：命中任一偏好主题（标题/关键词/摘要子串，大小写不敏感）→ 1.0。"""
    if not topics:
        return 0.0, False
    hay = " ".join([meta.title or "", " ".join(meta.keywords or []),
                    (meta.abstract or "")[:500]]).lower()
    hits = sum(1 for t in topics if t and t.lower() in hay)
    return min(1.0, float(hits)), True


def _year_weight(year: str, current_year: int) -> tuple[float, bool]:
    """近 3 年 = 1.0；3-10 年 = 0.7；>10 年 = 0.4；无年份 = 不可用。"""
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
                preferred_topics: list | None = None,
                starred: list | None = None,
                has_bib: bool = True,
                current_year: int | None = None) -> dict:
    """计算价值分与编译等级。

    返回：{score(0-5), level(L1/L2/L3), parts:{if,cited,topic,star,year,available}}
    """
    topics = list(preferred_topics or [])
    starred_set = set(starred or [])
    cy = current_year or datetime.now().year

    if_score, if_ok = _if_rank(journal_info)
    cited, cited_ok = _cited_norm(getattr(meta, "times_cited", 0), has_bib)
    topic, topic_ok = _topic_match(meta, topics)
    star = 1.0 if meta.doi in starred_set else 0.0
    year_w, year_ok = _year_weight(getattr(meta, "year", ""), cy)

    parts = {
        "if": {"value": round(if_score, 2), "available": if_ok},
        "cited": {"value": round(cited, 2), "available": cited_ok},
        "topic": {"value": round(topic, 2), "available": topic_ok},
        "star": {"value": star, "available": True},
        "year": {"value": round(year_w, 2), "available": year_ok},
    }
    available_w = sum(w for k, w in WEIGHTS.items()
                      if parts[k]["available"] or k == "star")
    if available_w <= 0:
        return {"score": 0.0, "level": "L1", "parts": parts}
    norm = sum(WEIGHTS[k] * parts[k]["value"] / (5.0 if k == "if" else 1.0)
               for k in WEIGHTS if parts[k]["available"] or k == "star")
    score5 = norm / available_w * 5.0
    score5 = round(min(5.0, max(0.0, score5)), 2)
    level = "L3" if (score5 >= L3_THRESHOLD or star) else (
        "L2" if score5 >= L2_THRESHOLD else "L1")
    return {"score": score5, "level": level, "parts": parts}
