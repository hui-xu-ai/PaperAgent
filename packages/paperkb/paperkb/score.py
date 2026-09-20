# -*- coding: utf-8 -*-
"""价值评分（编译选择性依据，KB-DESIGN v0.6 §6.2）。

value_score = 0.20×IF档 + 0.15×被引 + 0.25×AI价值 + 0.15×主题 + 0.10×年份
            + 0.10×PaperRank + 0.05×库内被引
- 归一化到 0-5；缺失字段权重等比放大（不惩罚无 IF/无被引文献）
- 等级门槛：L2 ≥ 2.5；L3 ≥ 4.0 且 ai_value 可用；L1 全做
- AI 价值评分由 L1 编译时产出（零额外成本）；主题评分同步产出
- paper_rank / library_citations 由 sync_lit_meta 从 paperlit 同步
"""
from __future__ import annotations

import math
from datetime import datetime

WEIGHTS = {"if": 0.20, "cited": 0.15, "ai_value": 0.25, "topic": 0.15, "year": 0.10,
           "paper_rank": 0.10, "lib_cited": 0.05}
L2_THRESHOLD = 2.5
L3_THRESHOLD = 4.0

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


def _ai_value(score: float | None) -> tuple[float, bool]:
    """AI 价值评分（0-5）：由 L1+L2 编译时产出。未评分（None）→ 不可用。"""
    if score is None:
        return 0.0, False
    return float(min(5.0, max(0.0, score))), True


def _topic_score(score: float | None) -> tuple[float, bool]:
    """主题匹配评分（0-1）：AI 基于用户主题表打分。未评分（None）→ 不可用。"""
    if score is None:
        return 0.0, False
    return float(min(1.0, max(0.0, score))), True


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


def _paper_rank_norm(rank: float | None) -> tuple[float, bool]:
    """PaperRank 归一化到 0-5。经验分布：>0.01 = 高影响力，0.001-0.01 = 中等，<0.001 = 低。"""
    if rank is None or rank <= 0:
        return 0.0, False
    if rank >= 0.01:
        return 5.0, True
    if rank >= 0.001:
        return 3.0 + 2.0 * (rank - 0.001) / 0.009, True
    if rank >= 0.0001:
        return 1.0 + 2.0 * (rank - 0.0001) / 0.0009, True
    return 1.0, True


def _lib_cited_norm(lib_citations: int | None) -> tuple[float, bool]:
    """库内被引归一化到 0-5。log 缩放：10 次库内被引 → 满分。"""
    if lib_citations is None or lib_citations <= 0:
        return 0.0, False
    return min(5.0, math.log10(max(1, lib_citations) + 1) / 1.0 * 5.0), True


def value_score(meta, journal_info: dict | None = None,
                has_bib: bool = True,
                current_year: int | None = None) -> dict:
    """计算价值分与编译等级。

    返回：{score(0-5), level(L1/L2/L3), parts:{if,cited,ai_value,topic,year,paper_rank,lib_cited,available}}
    AI 评分从 meta 对象的 ai_value_score / topic_score 属性读取（由编译产出写入）。
    paper_rank / library_citations 从 meta 对象读取（由 sync_lit_meta 同步）。
    """
    cy = current_year or datetime.now().year

    if_score, if_ok = _if_rank(journal_info)
    cited, cited_ok = _cited_norm(getattr(meta, "times_cited", 0), has_bib)
    ai_val, ai_ok = _ai_value(getattr(meta, "ai_value_score", None))
    topic_val, topic_ok = _topic_score(getattr(meta, "topic_score", None))
    year_w, year_ok = _year_weight(getattr(meta, "year", ""), cy)
    pr_val, pr_ok = _paper_rank_norm(getattr(meta, "paper_rank", None))
    lc_val, lc_ok = _lib_cited_norm(getattr(meta, "library_citations", None))

    parts = {
        "if": {"value": round(if_score, 2), "available": if_ok},
        "cited": {"value": round(cited, 2), "available": cited_ok},
        "ai_value": {"value": round(ai_val, 2), "available": ai_ok},
        "topic": {"value": round(topic_val, 2), "available": topic_ok},
        "year": {"value": round(year_w, 2), "available": year_ok},
        "paper_rank": {"value": round(pr_val, 2), "available": pr_ok},
        "lib_cited": {"value": round(lc_val, 2), "available": lc_ok},
    }
    available_w = sum(w for k, w in WEIGHTS.items() if parts[k]["available"])
    if available_w <= 0:
        return {"score": 0.0, "level": "L1", "parts": parts}
    norm = sum(WEIGHTS[k] * parts[k]["value"] / (5.0 if k in ("if", "ai_value", "paper_rank", "lib_cited") else 1.0)
               for k in WEIGHTS if parts[k]["available"])
    score5 = norm / available_w * 5.0
    score5 = round(min(5.0, max(0.0, score5)), 2)
    # L2 门槛：分数达标 **且** AI 评分必须可用（AI 没读过全文的文章不应触发深度编译）
    ai_available = parts["ai_value"]["available"]
    level = "L2" if (score5 >= L2_THRESHOLD and ai_available) else "L1"
    return {"score": score5, "level": level, "parts": parts}
