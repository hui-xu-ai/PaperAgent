#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: skill/src/paperparse/core/layout.py
功能: S2 版面分析：页眉/页脚/页码噪声过滤、双栏列检测（块中心 x 聚类）、
      参考文献区识别、阅读顺序排序（页 → 列 → y → x，与人类阅读一致）
对外接口: analyze / reading_order
版本: v1.0.0 (2026-08-19)
版本历史:
  v1.0.0 初始版本
"""
from __future__ import annotations

from collections import defaultdict
import re
from typing import Optional

from paperparse.middleware.schema import LayoutInfo, TextBlock

__all__ = ["analyze", "reading_order"]

HEADER_FRAC = 0.08      # 顶部区域比例（页眉）
FOOTER_FRAC = 0.955      # 页脚区起点比例（页码区；正文最后一行可到 ~94% 页高，勿误删）
GAP_FRAC = 0.12         # 双栏列间隙阈值（相对页宽）
REFERENCE_HEADING = "references"
REF_MARKER_RE = re.compile(r"^\[\d+\]")


def _page_height(page: int, blocks: list[TextBlock], page_sizes: Optional[dict]) -> float:
    """[局部] 页高：优先 page_sizes，否则取该页块最大 y1"""
    if page_sizes and page in page_sizes and len(page_sizes[page]) > 1:
        return float(page_sizes[page][1])
    ys = [b.bbox[3] for b in blocks if b.page == page]
    return max(ys, default=1.0)


def _is_noise(b: TextBlock, height: float, page: int) -> bool:
    """[局部] 页眉/页脚/页码判断（短文本 + 位置极端；首页顶部不视为页眉——可能是标题）"""
    text = b.text.strip()
    if not text:
        return False
    short = len(text) < 120
    bottom = b.bbox[3] > height * FOOTER_FRAC
    if text.isdigit() and bottom:
        return True                       # 页码
    if page == 1:
        return False                      # 首页顶部/底部可能是标题/版式信息
    return short and (b.bbox[1] < height * HEADER_FRAC or bottom)


def analyze(blocks: list[TextBlock],
            page_sizes: Optional[dict[int, list[float]]] = None) -> LayoutInfo:
    """[全局] 版面分析

    参数:
        blocks: S1 解析产物（TextBlock 列表）
        page_sizes: 页 → [宽, 高]（可选，缺失时用块坐标近似）
    返回:
        LayoutInfo（含噪声块 ID、双栏判定、列阈值、参考文献区页）
    """
    if not blocks:
        return LayoutInfo(page_count=0, note="无文本块")
    page_count = max(b.page for b in blocks)

    # 1) 噪声过滤（页眉/页脚/页码；首页顶部豁免）
    noise_ids: list[str] = []
    for b in blocks:
        h = _page_height(b.page, blocks, page_sizes)
        if _is_noise(b, h, b.page):
            noise_ids.append(b.block_id)

    # 2) 双栏检测：按页聚类 body/正文块中心 x
    columns_per_page: dict[int, int] = {}
    column_thresholds: dict[int, float] = {}
    page_widths: dict[int, float] = {}

    body_by_page: dict[int, list[TextBlock]] = defaultdict(list)
    for b in blocks:
        if b.block_id in noise_ids or b.kind in ("header", "footer"):
            continue
        body_by_page[b.page].append(b)

    for page, pblocks in body_by_page.items():
        if page_sizes and page in page_sizes:
            width = float(page_sizes[page][0])
        else:
            width = max(b.bbox[2] for b in pblocks) or 1.0
        page_widths[page] = width
        # 列检测只用"窄行"（宽 < 0.5×页宽）：全宽行（标题/作者/摘要/页眉）中心值
        # 会填平左右栏间隙，导致双栏误判为单栏（首页典型问题）
        narrow = [b for b in pblocks if (b.bbox[2] - b.bbox[0]) < width * 0.5]
        if len(narrow) < 6:                      # 窄行太少不足以判定
            columns_per_page[page] = 1
            continue
        centers = sorted((b.bbox[0] + b.bbox[2]) / 2 for b in narrow)
        gaps = [(centers[i + 1] - centers[i], i) for i in range(len(centers) - 1)]
        max_gap, idx = max(gaps, key=lambda g: g[0])
        if max_gap > width * GAP_FRAC:
            columns_per_page[page] = 2
            threshold = (centers[idx] + centers[idx + 1]) / 2
            column_thresholds[page] = threshold
        else:
            columns_per_page[page] = 1

    two_col_pages = [p for p, c in columns_per_page.items() if c == 2]
    is_two_column = len(two_col_pages) >= max(1, len(columns_per_page) * 0.5)

    # 3) 参考文献区识别：References 标题块，或一页内 ≥2 个 "[n]" 开头的引用块
    reference_zone_pages: list[int] = []
    for b in blocks:
        t = b.text.strip().lower().rstrip(".").rstrip(":")
        if t.startswith(REFERENCE_HEADING) and len(t) < 40:
            reference_zone_pages.append(b.page)
    ref_marker_pages: dict[int, int] = defaultdict(int)
    for b in blocks:
        if REF_MARKER_RE.match(b.text.strip()):
            ref_marker_pages[b.page] += 1
    for page, n in ref_marker_pages.items():
        if n >= 2:
            reference_zone_pages.append(page)
    reference_zone_pages = sorted(set(reference_zone_pages))

    return LayoutInfo(
        page_count=page_count,
        is_two_column=is_two_column,
        columns_per_page=columns_per_page,
        column_thresholds=column_thresholds,
        page_sizes=dict(page_sizes) if page_sizes else {},
        noise_block_ids=noise_ids,
        reference_zone_pages=reference_zone_pages,
    )


def reading_order(blocks: list[TextBlock], layout: LayoutInfo) -> list[TextBlock]:
    """[全局] 阅读顺序排序（过滤噪声；页 → 列（左→右）→ y0 → x0）

    全宽行（宽 ≥ 0.5×真实页宽，如标题/摘要/跨栏图注）不按列分配——其中心常在
    列间隙处会被错分到右列（首页标题 3 行拆散）；改为按 y 坐标插入窄行流。
    窄行分列用 **x0（左边界）** 而非中心：右栏短行（如标题续行 "Soft Actuator"）
    中心可能落在阈值左侧被错分左栏，而 x0 始终在栏内。

    参数:
        blocks: TextBlock 列表
        layout: analyze() 的产物（含噪声 ID 与列阈值）
    返回:
        已排序的正文块列表（order 字段已赋值；噪声块被排除）
    """
    def column_of(b: TextBlock) -> int:
        if layout.is_two_column and b.page in layout.column_thresholds:
            return 0 if b.bbox[0] < layout.column_thresholds[b.page] else 1
        return 0

    def page_width(b: TextBlock) -> float:
        """[局部] 真实页宽（优先 layout.page_sizes；缺失时回退该页最大块宽）"""
        ps = layout.page_sizes.get(b.page)
        if ps and len(ps) > 0 and ps[0] > 0:
            return float(ps[0])
        return max((x.bbox[2] - x.bbox[0] for x in blocks if x.page == b.page),
                   default=1.0)

    kept = [b for b in blocks if b.block_id not in layout.noise_block_ids]
    narrow = [b for b in kept if (b.bbox[2] - b.bbox[0]) < page_width(b) * 0.5]
    wide = [b for b in kept if (b.bbox[2] - b.bbox[0]) >= page_width(b) * 0.5]

    ordered: list[TextBlock] = sorted(
        narrow, key=lambda b: (b.page, column_of(b), b.bbox[1], b.bbox[0]))
    # 全宽行按 y 插入同页窄行流（y 小于其后窄行 → 插其前；无同页后续窄行 → 插该页末尾）。
    # 修复（R02）：该页在窄行流中完全无块（纯图页/整页宽行，如 Sci Robotics Fig.4 页）时，
    # 原实现 last_same=-1 → insert(0) 把图注提到全文最前——应插入到下一页窄行之前（或末尾）。
    for wb in sorted(wide, key=lambda b: (b.page, b.bbox[1])):
        same_page = [i for i, nb in enumerate(ordered) if nb.page == wb.page]
        if same_page:
            after = next((i for i in same_page if ordered[i].bbox[1] > wb.bbox[1]), None)
            if after is None:
                after = same_page[-1] + 1
            ordered.insert(after, wb)
        else:
            nxt = next((i for i, nb in enumerate(ordered) if nb.page > wb.page),
                       len(ordered))
            ordered.insert(nxt, wb)

    for i, b in enumerate(ordered):
        b.column = column_of(b)
        b.order = i
    return ordered
