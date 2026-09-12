#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: skill/src/paperparse/core/layout_skeleton.py
功能: MinerU content_list.json → 布局骨架（P-ENHANCE R02 / S1.5）：
      - 类型过滤（剔除 header/footer/page_number/page_footnote/aside_text 噪声）
      - 双栏判定 + (page, column, y0, x0) 阅读序重排
      - 角色标注（title/heading/body/equation/caption/figure/table/reference）
      - 章节树（heading → 下属 items 归属）
      消费方：S3.5 双轨融合的段落顺序断言（修复 full.md 错排/段落被标题劈开）、
      metadata 提取边界（title/abstract）、header/footer 剔除。
对外接口: build_skeleton / LayoutSkeleton / to_dict
版本: v0.1.0 (2026-08-22)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

__all__ = ["build_skeleton", "LayoutSkeleton", "SkeletonItem"]

# 噪声类型（MinerU content_list type 枚举，实测确认）
NOISE_TYPES = frozenset({"header", "footer", "page_number", "page_footnote",
                         "aside_text"})
# 图表类型 → 角色
FIGURE_TYPES = frozenset({"chart", "image", "table"})

_HEADING_RE = re.compile(
    r"^(?:\d+(?:\.\d+)*\.?\s+[A-Z]|"
    r"abstract|introduction|conclusion|results?|discussion|"
    r"materials?\s+and\s+methods|"
    r"references?\s*(?:and\s+notes)?|acknowledg\w+|supplementary\s+materials?|"
    r"conflict\s*of\s*interest|data\s+availability|keywords?)\b",
    re.IGNORECASE)
# 数字编号标题（1. / 2.1. 等）单独判定：不受小写开头限制
_NUM_HEADING_RE = re.compile(r"^\d+(?:\.\d+)*\.?\s+\S")
_CAPTION_RE = re.compile(r"^(?:figure|fig\.?|table|scheme)\s*\d+", re.IGNORECASE)
_MATHY_RE = re.compile(r"^\s*\$.*\$\s*$")
_MAX_HEADING_LEN = 100


@dataclass
class SkeletonItem:
    """[全局] 骨架条目（过滤后、带角色与栏号）"""
    index: int = 0            # 原 content_list 下标
    page: int = 1             # 1-based
    bbox: tuple = (0, 0, 0, 0)
    type: str = "text"
    text_level: int = 0
    text: str = ""
    role: str = "body"
    column: int = 0           # 0=左/单栏, 1=右


@dataclass
class LayoutSkeleton:
    """[全局] 布局骨架：过滤排序后的条目 + 统计 + 章节树"""
    items: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    section_tree: list = field(default_factory=list)
    is_two_column: bool = False

    def to_dict(self) -> dict:
        return {
            "is_two_column": self.is_two_column,
            "stats": self.stats,
            "items": [{
                "index": i.index, "page": i.page, "bbox": list(i.bbox),
                "type": i.type, "text_level": i.text_level,
                "role": i.role, "column": i.column, "text": i.text[:200],
            } for i in self.items],
            "section_tree": self.section_tree,
        }


def _parse_item(raw: dict, idx: int) -> SkeletonItem | None:
    """[局部] content_list 条目 → SkeletonItem（脏条目返回 None）"""
    itype = str(raw.get("type") or "")
    text = str(raw.get("text") or "")
    if not itype and not text:
        return None
    try:
        bbox = tuple(float(v) for v in (raw.get("bbox") or [0, 0, 0, 0])[:4])
    except (TypeError, ValueError):
        return None
    try:
        page = int(raw.get("page_idx", 0)) + 1
    except (TypeError, ValueError):
        page = 1
    try:
        level = int(raw.get("text_level") or 0)
    except (TypeError, ValueError):
        level = 0
    return SkeletonItem(index=idx, page=page, bbox=bbox, type=itype,
                        text_level=level, text=text)


def _detect_columns(items: list[SkeletonItem]) -> tuple[bool, float]:
    """[局部] 双栏判定：对非噪声 text/equation 类条目的 x0 做最大间隙聚类。

    返回 (is_two_column, 分栏阈值 x)；单栏时阈值为 0。
    """
    xs = sorted(i.bbox[0] for i in items if i.type in ("text", "equation") and i.bbox[2] > i.bbox[0])
    if len(xs) < 8:
        return False, 0.0
    width = max(i.bbox[2] for i in items) or 1.0
    # 最大间隙（排除首尾极端）
    gaps = [(xs[i + 1] - xs[i], i) for i in range(len(xs) - 1)]
    gaps.sort(reverse=True)
    gap, gi = gaps[0]
    if gap <= width * 0.25:
        return False, 0.0
    left, right = xs[:gi + 1], xs[gi + 1:]
    if not left or not right:
        return False, 0.0
    mid_left = sum(left) / len(left)
    mid_right = sum(right) / len(right)
    if mid_right - mid_left < width * 0.3:
        return False, 0.0
    thr = (mid_left + mid_right) / 2
    return True, thr


def _assign_role(item: SkeletonItem, is_first_body_item: bool,
                 title_done: bool) -> str:
    """[局部] 角色标注（依据 type/text_level/文本形态）

    标题判定规则：
      - text_level>=2（MinerU 语义）→ heading
      - 数字编号标题（1. / 2.1.）→ heading
      - 已知标题词开头（Abstract/Introduction/...）且 ≤100 字符 → heading
      - 其余（小写正文行如 "method via ..."）→ body（防被标题劈开的续段误判）
    title 只分配一次（page-1 首个条目或 text_level==1）。
    """
    t = item.text.strip()
    if item.type == "ref_text":
        return "reference"
    if item.type in FIGURE_TYPES:
        if _CAPTION_RE.match(t):
            return "caption"
        return "figure"
    if item.type == "equation" or _MATHY_RE.match(t):
        return "equation"
    if item.text_level >= 2:
        # R10：MinerU 偶发误标 L2（小写短语非标题，如 cej "increase of immersion time."）
        # ——小写开头且无数字编号/已知词 → 降级 body
        if len(t) < 60 and re.match(r"^[a-z]", t) and not re.match(r"^\d", t):
            return "body"
        return "heading"
    if item.text_level == 1 and not title_done:
        return "title"
    if len(t) <= _MAX_HEADING_LEN and (_NUM_HEADING_RE.match(t)
                                       or _HEADING_RE.match(t)):
        return "heading"
    if is_first_body_item and item.page == 1 and not title_done and len(t) > 30:
        return "title"
    if _CAPTION_RE.match(t):
        return "caption"
    return "body"


def build_skeleton(content_items: list[dict]) -> LayoutSkeleton:
    """[全局] content_list.json 条目 → LayoutSkeleton

    参数:
        content_items: content_list.json 列表（dict 条目）
    返回:
        LayoutSkeleton（items 已过滤+排序；stats 含类型分布/过滤统计）
    """
    raw_counts: dict[str, int] = {}
    parsed: list[SkeletonItem] = []
    for idx, raw in enumerate(content_items):
        t = str(raw.get("type") or "")
        raw_counts[t] = raw_counts.get(t, 0) + 1
        item = _parse_item(raw, idx)
        if item is None:
            continue
        if item.type in NOISE_TYPES:
            continue
        parsed.append(item)

    is_two, thr = _detect_columns(parsed)
    for it in parsed:
        it.column = 1 if (is_two and it.bbox[0] >= thr) else 0
    # 阅读序：page → column → y0 → x0
    ordered = sorted(parsed, key=lambda i: (i.page, i.column, i.bbox[1], i.bbox[0]))

    roles: dict[str, int] = {}
    title_done = False
    for k, it in enumerate(ordered):
        it.role = _assign_role(it, is_first_body_item=(k == 0), title_done=title_done)
        if it.role == "title":
            title_done = True
        roles[it.role] = roles.get(it.role, 0) + 1

    # 章节树：heading → 下属 items（title/heading 不入节；caption/figure 跟随所在节）
    tree: list[dict] = []
    cur: dict | None = None
    for it in ordered:
        if it.role == "title":
            continue
        if it.role == "heading":
            cur = {"heading": it.text, "level": it.text_level, "items": []}
            tree.append(cur)
        elif cur is not None:
            cur["items"].append(it.index)
    # 章节树 items 用 (page,index) 便于消费方定位
    stats = {
        "raw_items": len(content_items),
        "kept": len(ordered),
        "filtered": len(content_items) - len(parsed),
        "types": raw_counts,
        "roles": roles,
        "two_column": is_two,
        "sections": len(tree),
    }
    sk = LayoutSkeleton(items=ordered, stats=stats, section_tree=tree,
                        is_two_column=is_two)
    return sk


def load_content_list(path: str) -> list[dict]:
    """[全局] 读取 content_list.json（文件或 JSON 文本）"""
    import json
    from pathlib import Path
    p = Path(path)
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
    else:
        data = json.loads(path)
    return data if isinstance(data, list) else []
