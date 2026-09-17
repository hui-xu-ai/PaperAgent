#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: skill/src/paperparse/core/image_extract.py
功能: S4 图片提取（大图保留策略）：从原始 PDF 按图片 bbox 高清渲染整图（默认 300 DPI），
      不采用解析器拆分的子图；sha256 去重；过滤图标/装饰小图与页眉页脚区域；
      图注关联（同页图下方最近的 caption 块）
对外接口: extract_figures
版本: v1.0.0 (2026-08-19)
版本历史:
  v1.0.0 初始版本
  v1.1.0 (2026-09-12) extract_figures_caption_driven 重写为"内容聚类 + 文本避让"：
         位图不再限 25% 页宽门槛、矢量图元参与聚类、宽文本行硬避让，
         根治"裁剪框混入正文"（实测 10 篇 65 图，污染 19 → 0）
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pymupdf

from paperparse.core.pymupdf_fallback import render_clip
from paperparse.middleware.errors import PaperError
from paperparse.middleware.schema import Figure, TextBlock

__all__ = ["extract_figures", "extract_figures_from_skeleton",
           "extract_figures_caption_driven"]

MIN_FIGURE_SIZE_PT = 60.0    # 小于该边长视为图标/装饰（可配）
HEADER_FRAC = 0.08
FOOTER_FRAC = 0.93
CAPTION_MAX_CHARS = 500
_SKEL_NOISE = frozenset({"header", "footer", "page_number", "page_footnote", "aside_text"})
# 图注锚点（Figure N. / Fig. N.）与摘要图（Graphical abstract）
_FIG_CAP_RE = re.compile(r"^(fig(?:ure)?\.?\s*\d+)\b", re.IGNORECASE)
_GA_RE = re.compile(r"^graphical\s+abstract\b", re.IGNORECASE)
# P14-M4：图注编号提取（"Fig. 1." / "Fig. 2 h." / "Table 2." / "Scheme 3." → 编号）
_FIG_NUM_RE = re.compile(r"^(fig(?:ure)?|table|scheme)\.?\s*(\d+)", re.IGNORECASE)
# 大图判定：边长 ≥ 页宽 25%（cej 大图 511/595 ≈ 86%；页眉 logo ~60pt 远小于）
_FIG_MIN_FRAC = 0.25


def extract_figures_from_skeleton(pdf_path: str | Path, skeleton, out_dir: str | Path,
                                  dpi: int = 300,
                                  min_size: float = MIN_FIGURE_SIZE_PT,
                                  blocks: list[TextBlock] | None = None) -> list[Figure]:
    """[全局] 骨架驱动图片提取（R10）：MinerU content_list 的 figure/chart/image 角色
    bbox → 按页坐标缩放 → 渲染整图。解决 get_image_info 漏矢量图（cej 41 图仅提取 2）。

    坐标缩放：content_list 页宽（该页 max x1）→ PDF 页宽（pymupdf rect.width）。
    去重：sha256 + 嵌套/重叠过滤（面积降序，重叠>60% 跳过小者）。
    图注：骨架条目 text（MinerU chart 类型常含题注）；为空回退 blocks 本地 caption。

    参数:
        pdf_path: 原始 PDF
        skeleton: LayoutSkeleton（items 含 role/bbox/page/type/text）
        out_dir: images/ 输出目录
        dpi: 渲染分辨率
        min_size: 最小边长（pt）
        blocks: 本地文本块（可选，图注锚点补充）
    返回:
        Figure 列表（文件已写入 out_dir）
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception as exc:
        raise PaperError("PAPER-0002", stage="S4", path=str(pdf_path),
                         detail={"reason": f"pymupdf 打开失败: {exc}"}) from exc
    # 坐标缩放：content_list 页宽（该页 max x1）→ PDF 页宽
    cl_w: dict[int, float] = {}
    for it in skeleton.items:
        cl_w[it.page] = max(cl_w.get(it.page, 0.0), float(it.bbox[2]))
    pdf_w = {i + 1: doc[i].rect.width for i in range(doc.page_count)}
    pdf_h = {i + 1: doc[i].rect.height for i in range(doc.page_count)}

    def _to_pdf(it) -> tuple:
        s = pdf_w.get(it.page, 595.0) / max(cl_w.get(it.page, 1.0), 1.0)
        return tuple(v * s for v in it.bbox)

    # 1) 图注锚点（Figure N. / Graphical abstract）——本地 caption 块 + 骨架 caption 角色
    anchors: list[dict] = []
    _seen: set[tuple] = set()
    for src in ([b for b in (blocks or []) if b.kind == "caption"],
                [it for it in skeleton.items if it.role == "caption"]):
        for x in src:
            t = (x.text or "").strip()
            if not (_FIG_CAP_RE.match(t) or _GA_RE.match(t)):
                continue
            key = (x.page, round(x.bbox[1], 1))
            if key in _seen:
                continue
            _seen.add(key)
            anchors.append({"page": x.page, "bbox": tuple(x.bbox), "text": t,
                            "is_ga": bool(_GA_RE.match(t))})

    # 2) 拆分图池（MinerU 把大图拆成多个小条目 → 按图注合并回大图）
    pool = [{"it": it, "bbox": _to_pdf(it)} for it in skeleton.items
            if it.role == "figure" and it.type not in _SKEL_NOISE]

    figures: list[Figure] = []
    seen_hashes: set[str] = set()
    used: set[int] = set()

    def _render(page: int, bbox: tuple, caption: str, is_ga: bool) -> bool:
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        if w < min_size or h < min_size:
            return False
        try:
            data = render_clip(pdf_path, page, bbox, dpi=dpi)
        except PaperError:
            return False
        sha = hashlib.sha256(data).hexdigest()
        if sha in seen_hashes:
            return False
        seen_hashes.add(sha)
        fig_id = "F%03d" % (len(figures) + 1)
        fname = "%s.png" % fig_id
        (out / fname).write_bytes(data)
        figures.append(Figure(fig_id=fig_id, file="images/%s" % fname,
                              caption=(caption[:CAPTION_MAX_CHARS] if caption else None),
                              page=page, bbox=bbox, sha256=sha, abstract=is_ga))
        return True

    def _closest_cluster(zone: list, cap_y: float, page_h: float) -> list:
        """[局部] 题注上方条目按 y 聚类：若跨度异常（混入上一图），取与题注最近的连续簇"""
        zone = sorted(zone, key=lambda p: p["bbox"][1])
        if len(zone) <= 1:
            return zone
        if zone[-1]["bbox"][3] - zone[0]["bbox"][1] <= page_h * 0.55:
            return zone
        # 找最大 gap 切分，取 y 接近题注的下半簇
        gaps = [(zone[i + 1]["bbox"][1] - zone[i]["bbox"][3], i)
                for i in range(len(zone) - 1)]
        _, gi = max(gaps, key=lambda g: g[0])
        return zone[gi + 1:]

    try:
        # 3) 图注驱动：合并题注上方（或下方）的拆分条目为一张大图
        #    双栏页按栏匹配（题注 x 分栏 → 条目 column 同侧，防同页两题注竞争条目）
        def _col(x0: float, page: int) -> int:
            return 0 if x0 < pdf_w.get(page, 595.0) / 2 else 1

        for a in anchors:
            cap_col = _col(a["bbox"][0], a["page"])
            if a["is_ga"]:
                zone = [p for i, p in enumerate(pool) if i not in used
                        and p["it"].page == a["page"] and p["bbox"][3] <= a["bbox"][1]]
            else:
                def _same_zone(p) -> bool:
                    return (p["it"].page == a["page"]
                            and p["it"].column == cap_col)
                above = [i for i, p in enumerate(pool) if i not in used
                         and _same_zone(p) and p["bbox"][3] <= a["bbox"][1]]
                below = [i for i, p in enumerate(pool) if i not in used
                         and _same_zone(p) and p["bbox"][1] >= a["bbox"][3]]
                idxs = above or below
                if not idxs:
                    # 栏匹配失败回退：同页任意列（单栏页/条目列未知）
                    above = [i for i, p in enumerate(pool) if i not in used
                             and p["it"].page == a["page"] and p["bbox"][3] <= a["bbox"][1]]
                    below = [i for i, p in enumerate(pool) if i not in used
                             and p["it"].page == a["page"] and p["bbox"][1] >= a["bbox"][3]]
                    idxs = above or below
                zone = [pool[i] for i in idxs]
            if not zone:
                continue
            zone = _closest_cluster(zone, a["bbox"][1], pdf_h.get(a["page"], 800.0))
            # P13：跨栏合并——cej 等双栏版式的大图常被 MinerU 按栏拆成左右两半
            # （同 y 区间、不同 column）；图注驱动按栏匹配只取一半，另一半残留成
            # 重复小图（实测 6 图注 → 11 图）。把同页 y 区间**重叠**（或间隙 < 页高 3%）
            # 的未用条目并入 zone，渲染为一张跨栏整图。
            zy0 = min(p["bbox"][1] for p in zone)
            zy1 = max(p["bbox"][3] for p in zone)
            for i, p in enumerate(pool):
                if i in used or p in zone:
                    continue
                if p["it"].page != a["page"]:
                    continue
                if p["bbox"][3] <= zy0 and zy0 - p["bbox"][3] > pdf_h.get(a["page"], 800.0) * 0.03:
                    continue
                if p["bbox"][1] >= zy1 and p["bbox"][1] - zy1 > pdf_h.get(a["page"], 800.0) * 0.03:
                    continue
                zone.append(p)   # y 重叠或紧邻 → 同一大图的跨栏/相邻部分
            bbox = (min(p["bbox"][0] for p in zone), min(p["bbox"][1] for p in zone),
                    max(p["bbox"][2] for p in zone), max(p["bbox"][3] for p in zone))
            if _render(a["page"], bbox, a["text"], a["is_ga"]):
                used.update(i for i, p in enumerate(pool) if p in zone)
        # 4) 未归属的首页顶部大条目 → 摘要图（无 Figure 题注的 graphical abstract）
        for i, p in enumerate(pool):
            if i in used:
                continue
            bbox = p["bbox"]
            ph = pdf_h.get(p["it"].page, 800.0)
            if p["it"].page == 1 and bbox[1] < ph * 0.6 and bbox[3] < ph * 0.75 \
                    and bbox[3] - bbox[1] >= min_size:
                if _render(1, bbox, "Graphical abstract", True):
                    used.add(i)
        # 5) P13 兜底：图注缺失时大图不丢——未归属 figure 条目按页内 y 间隙聚类，
        #    每组渲染一张整图（MinerU 拆分的子图重新聚合；cej Fig.3/4 曾因图注被
        #    判重误杀而整体丢失）。簇内间隙 > 页高 8% 视为不同图。
        remaining = [p for i, p in enumerate(pool) if i not in used]
        by_page: dict[int, list] = {}
        for p in remaining:
            by_page.setdefault(p["it"].page, []).append(p)
        for page, zone in by_page.items():
            zone.sort(key=lambda p: p["bbox"][1])
            groups: list[list] = []
            cur: list = []
            for p in zone:
                if cur and p["bbox"][1] - cur[-1]["bbox"][3] > pdf_h.get(page, 800.0) * 0.08:
                    groups.append(cur)
                    cur = []
                cur.append(p)
            if cur:
                groups.append(cur)
            for g in groups:
                bbox = (min(p["bbox"][0] for p in g), min(p["bbox"][1] for p in g),
                        max(p["bbox"][2] for p in g), max(p["bbox"][3] for p in g))
                if _render(page, bbox, None, False):
                    used.update(i for i, p in enumerate(pool) if p in g)
    finally:
        doc.close()
    return figures

MIN_FIGURE_SIZE_PT = 60.0    # 小于该边长视为图标/装饰（可配）
HEADER_FRAC = 0.08
FOOTER_FRAC = 0.93
CAPTION_MAX_CHARS = 500


def _find_caption(blocks: list[TextBlock], page: int,
                  fig_bbox: tuple) -> str | None:
    """[局部] 图注匹配：同页 caption 行中，优先下方最近者，其次上方最近者
    （部分版式图注在图上方；双向匹配解决"图注上方无图引用"问题）"""
    caps = [b for b in blocks if b.page == page and b.kind == "caption"]
    if not caps:
        return None
    below = [b for b in caps if b.bbox[1] >= fig_bbox[3]]
    if below:
        below.sort(key=lambda b: b.bbox[1])
        return below[0].text.strip()[:CAPTION_MAX_CHARS]
    above = [b for b in caps if b.bbox[3] <= fig_bbox[1]]
    if above:
        above.sort(key=lambda b: b.bbox[3], reverse=True)
        return above[0].text.strip()[:CAPTION_MAX_CHARS]
    # 同页无 caption：取其他页最近的（一般不会发生）
    others = sorted(blocks, key=lambda b: abs(b.bbox[1] - fig_bbox[1]))
    for b in others:
        if b.kind == "caption":
            return b.text.strip()[:CAPTION_MAX_CHARS]
    return None


def extract_figures(pdf_path: str | Path, blocks: list[TextBlock],
                    out_dir: str | Path, dpi: int = 300,
                    min_size: float = MIN_FIGURE_SIZE_PT) -> list[Figure]:
    """[全局] 提取全部大图（渲染自原始 PDF，保留整图）

    参数:
        pdf_path: 原始 PDF 路径
        blocks: 文本块（用于图注关联；可为空列表）
        out_dir: 图片输出目录（images/）
        dpi: 渲染分辨率（默认 300）
        min_size: 最小边长（pt），小于视为装饰
    返回:
        Figure 列表（文件已写入 out_dir）
    报错:
        PaperError PAPER-0030（渲染失败）
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception as exc:
        raise PaperError("PAPER-0002", stage="S4", path=str(pdf_path),
                         detail={"reason": f"pymupdf 打开失败: {exc}"}) from exc

    figures: list[Figure] = []
    seen_hashes: set[str] = set()
    try:
        for pno in range(doc.page_count):
            page = doc[pno]
            page_h = page.rect.height
            try:
                infos = page.get_image_info(xrefs=True)
            except Exception:
                infos = []
            for info in infos:
                try:
                    bbox = tuple(float(v) for v in info["bbox"])
                except (KeyError, TypeError, ValueError):
                    continue
                w = bbox[2] - bbox[0]
                h = bbox[3] - bbox[1]
                if w < min_size or h < min_size:
                    continue
                # 页眉/页脚区域（期刊 logo、页眉图）跳过
                if bbox[1] < page_h * HEADER_FRAC or bbox[3] > page_h * FOOTER_FRAC:
                    continue
                try:
                    data = render_clip(pdf_path, pno + 1, bbox, dpi=dpi)
                except PaperError:
                    continue
                sha = hashlib.sha256(data).hexdigest()
                if sha in seen_hashes:            # 去重（同一图多处引用）
                    continue
                seen_hashes.add(sha)
                fig_id = "F%03d" % (len(figures) + 1)
                fname = "%s.png" % fig_id
                (out / fname).write_bytes(data)
                figures.append(Figure(
                    fig_id=fig_id, file="images/%s" % fname,
                    caption=_find_caption(blocks, pno + 1, bbox),
                    page=pno + 1, bbox=bbox, sha256=sha))
    finally:
        doc.close()
    return figures


# ---------- P14-M4 v2：图注驱动提取（内容聚类 + 文本避让） ----------

_PAD = 1.5                   # 裁剪框外扩（pt）
_CLUSTER_GAP_FRAC = 0.12     # 图元聚类垂直间隙上限（× 页高）≈97pt @810：
                             # 图与图注之间常夹着坐标轴标签/分图号（jcp 实测 58pt
                             # 间隙），阈值太小会整篇丢图；跨过正文由文本障碍行拦住
_OBSTACLE_W_FRAC = 0.45      # "宽文本行"判据（× 该页正文栏宽）——正文障碍
_OBSTACLE_OVL = 0.30         # 障碍行与簇/前沿的水平重叠比例阈值
_CONTAM_FRAC = 0.30          # 文本行落入裁剪框面积比例 → 判"混入正文"
_TRIM_KEEP_FRAC = 0.5        # 避让收缩后至少保留原框高度的比例
_DEDUP_FRAC = 0.60           # 与已产出图的重叠比例 → 重复图跳过
_BESIDE_Y_OVL = 0.30         # 图注与图体的垂直重叠判据（相对较矮者）
_BESIDE_BAND_FRAC = 0.18     # 图注纵向带外扩（× 页高）：骨架常把图注段拆成单行，
                             # 不扩带会漏掉"图注在图侧、图体上下跨多行"的版式
_BESIDE_X_GAP_FRAC = 0.25    # 图注与图体的水平间隙上限（× 页宽）
_SEED_MIN_AREA_FRAC = 0.004  # "并排种子图元"最小面积（× 页面积），滤行内小位图
_RECT_GAP_H_FRAC = 0.10      # 区域生长水平间隙上限（× 页宽）
# ---- ★2026-09-17 多分图截断修复（用户实测：ncomms Figure 4/5 只剩下半张，Figure 1 5 块 panel 只剩 2 块）----
# 根因：`_CLUSTER_GAP_FRAC` 是**页高**比例（782pt 页 ⇒ 93.9pt），而多分图 panel 间隙可达 139.9pt
#（页高 17.9%）⇒ 生长在 panel a 前停住，产物只剩离图注最近的那一块（实测 trace 见
# `.dsh-memory/project/FINDING-FIGURE-TRUNCATION-20260917.md`）。
# 修法（保守，仅在"间隙 > gap_max"的放宽分支生效，常规并簇行为零改动）：
_LOOSE_GAP_MUL = 2.5         # 放宽分支的间隙上限 = gap_max × 该系数
_LOOSE_COL_OVL = 0.55        # 放宽并簇要求**同栏**（x 重叠 ≥ 该比例，相对较窄者）
_LOOSE_H_RATIO = (0.5, 2.0)  # 放宽并簇要求与当前簇高度相当（倍数区间）
_LOOSE_CONTAIN = 0.70        # ★列包含判据：候选**宽度的 ≥70% 须落在本图注的 x 范围**内。
                             # 为什么：并排图（ncomms p5：Figure 4 左栏 / Figure 5 右栏，图注同 y 带）
                             # 若无此判据会把左右两栏并成一张；整幅页眉横线也由此被挡。
_LOOSE_TOP_SAFE_FRAC = 0.05  # ★页眉安全带（× 页高）：图的搜索区从该线以下开始。
                             # 依据（用户判据②）：页眉与图之间隔着整条正文/页边空白（空白大），
                             # 而图内 panel 间空白很小——ncomms 实测 5.7～18.2pt vs 页内图间 98pt。
_LOOSE_ASPECT_MAX = 20.0     # （**未启用**，留档）极扁横条排除：页眉分隔线 2540:1
_LOOSE_X_TOL = 3.0           # 越出图注栏界的容差（pt）
_LOOSE_MAX_H_FRAC = 0.60     # 簇高上限（× 页高）——防一路吃到页眉


def _inter_area(a: tuple, b: tuple) -> float:
    """[局部] 两 bbox 交叠面积"""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def _collect_graphics(page) -> list[tuple]:
    """[局部] 页面图形图元 bbox：位图（任意尺寸）+ 矢量绘制 rect。

    v2 不再用"≥ 页宽 25%"的位图门槛——PNAS/ncomms 的图常由十几个小位图
    与矢量图元拼成，门槛会把真图判成"无位图"→ 回落整栏渲染 → 正文混入。
    过滤：< 4pt 的碎片、≥ 80% 页面积的背景块。
    """
    page_area = max(1e-6, page.rect.width * page.rect.height)
    prims: list[tuple] = []
    try:
        infos = page.get_image_info(xrefs=True)
    except Exception:
        infos = []
    for info in infos:
        try:
            b = tuple(float(v) for v in info["bbox"])
        except (KeyError, TypeError, ValueError):
            continue
        if b[2] - b[0] < 3 or b[3] - b[1] < 3:
            continue
        if (b[2] - b[0]) * (b[3] - b[1]) >= page_area * 0.92:
            continue
        prims.append(b)
    try:
        draws = page.get_drawings()
    except Exception:
        draws = []
    for d in draws:
        try:
            r = d["rect"]
            b = (float(r.x0), float(r.y0), float(r.x1), float(r.y1))
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        w, h = b[2] - b[0], b[3] - b[1]
        if max(w, h) < 4:
            continue
        if w * h >= page_area * 0.8:          # 整页背景/裁剪框
            continue
        prims.append(b)
    return prims


def _grow_cluster(prims: list[tuple], obstacles: list[tuple], span: tuple,
                  start: float, gap_max: float, down: bool = False,
                  skip: set | None = None, page_h: float | None = None,
                  zone_top: float | None = None):
    """[局部] 从 start 沿方向生长图形簇（down=False 向上 / True 向下）。

    - 只并入与当前簇水平重叠 ≥15%（相对较窄者）的图元；
    - 与前沿垂直间隙 ≤ gap_max；
    - **前沿与候选图元之间夹着宽文本行（正文/标题/图注）→ 立即停止**：
      这是"图片裁到正文"的根治点（旧的"整栏渲染兜底"会一路吃到页眉）。
    - ★2026-09-17 多分图修复：间隙 > gap_max 时**还有一次放宽机会**，但必须同时满足
      「同栏（x 重叠 ≥ `_LOOSE_COL_OVL`）」「不越出图注栏界 `span`（容差 `_LOOSE_X_TOL`）」
      「与当前簇高度相当」「并后簇高 ≤ `_LOOSE_MAX_H_FRAC`×页高（`page_h` 给出时）」——
      即"同一张图的上下两个 panel"。缺 `page_h` 时自动跳过高度护栏（保持旧调用可用）。
    skip：已被别的图占用的图元下标。
    返回 (bbox | None, 用掉的图元下标集合)
    """
    x0, x1 = span
    frontier = start
    used: set[int] = set()
    skip = skip or set()
    cluster = None
    while True:
        best = None
        for i, p in enumerate(prims):
            if i in used or i in skip:
                continue
            if down:
                near = p[1]
                if near < frontier - 0.5:
                    continue
                gap = near - frontier
            else:
                near = p[3]
                if near > frontier + 0.5:
                    continue
                gap = frontier - near
            if not down and zone_top is not None and p[1] < zone_top - 0.5:
                # ★图注屏障（用户判据①：每张大图之间必有图注 / 页眉不能并进图）：
                # 向上生长时，候选不得越过「本栏上方最近一条图注的下沿」与「页眉安全带」。
                # 必须在这一层判（而不是只在放宽分支里）——否则页眉横线等"距离很近"的
                # 装饰图元走普通分支就被并进来了。
                continue
            if gap > gap_max:
                # ---- 放宽分支：仅"同一张多分图"才放行（★2026-09-17）----
                # ★量尺修正：普通规则的 `gap` 从**起始前沿**（=图注那一侧）算起，它把
                # "图注到最远那块 panel 的总距离"当成两个图元之间的距离。多分图（如 NC
                # Figure 1：panel a 距图注 273pt，但距下方 panel 仅 10pt）会被这条误杀
                # ——实测差 10.6pt 就卡住。故放宽分支改用**与当前簇沿的距离**作量尺：
                # 这才是"是不是同一张图的两块"该看的距离。起始前沿仍是普通分支的量尺（不变）。
                c_top = cluster[1] if cluster else start
                c_bot = cluster[3] if cluster else start
                eff_gap = (p[1] - c_bot) if down else (c_top - p[3])
                lov = min(x1, p[2]) - max(x0, p[0])
                lwmin = min(x1 - x0, p[2] - p[0])
                if lov <= 0 or (lwmin > 0 and lov / lwmin < _LOOSE_COL_OVL):
                    continue                       # 不同栏（左栏图 vs 右栏图）
                if p[0] < x0 - _LOOSE_X_TOL or p[2] > x1 + _LOOSE_X_TOL:
                    continue                       # 越出图注栏界（页眉横线/整幅装饰）
                # ★列包含：候选宽度须 ≥_LOOSE_CONTAIN 落在本图注 x 范围（并排图各归各栏）
                _cov = (min(x1, p[2]) - max(x0, p[0])) / max(1e-6, p[2] - p[0])
                if _cov < _LOOSE_CONTAIN:
                    continue
                if eff_gap > gap_max * _LOOSE_GAP_MUL:
                    continue                       # 离当前簇太远，不是同一张图
                if page_h:
                    ny0 = min(cluster[1], p[1]) if cluster else p[1]
                    ny1 = max(cluster[3], p[3]) if cluster else p[3]
                    if (ny1 - ny0) > page_h * _LOOSE_MAX_H_FRAC:
                        continue                   # 并后过高 → 会吃到页眉
                if cluster is not None:
                    # ★2026-09-17：**不再用"候选/簇高度比"**。它会把"簇已高 + 上方还有一块
                    # 全幅宽 panel"误杀（ncomms Figure 1 的 panel a 实测比 2.57 > 2.0，差 0.57
                    # 倍就丢掉整块 panel）。改由下面两条语义判据承担"不是同一张图"的拦截：
                    #   ① `zone_top` 图注屏障（用户判据①：两张大图之间必有图注）；
                    #   ② `_LOOSE_CONTAIN` 列包含 ≥70%（并排图各归各栏）。
                    # 这两条比"高度比"更接近版式事实，且实测零副作用（F002/F003/F006 不变）。
                    pass
            else:
                ov = min(x1, p[2]) - max(x0, p[0])
                wmin = min(x1 - x0, p[2] - p[0])
                if ov <= 0 or (wmin > 0 and ov / wmin < 0.15):
                    continue
            if best is None or gap < best[0]:
                best = (gap, i, p)
        if best is None:
            break
        _, i, p = best
        nx0, nx1 = min(x0, p[0]), max(x1, p[2])
        y_lo, y_hi = (frontier, p[1]) if down else (p[3], frontier)
        if any(o[1] >= y_lo - 1 and o[3] <= y_hi + 1
               and (min(nx1, o[2]) - max(nx0, o[0])) > 0
               and (min(nx1, o[2]) - max(nx0, o[0])) / max(1e-6, o[2] - o[0])
               >= _OBSTACLE_OVL
               for o in obstacles):
            break
        used.add(i)
        cluster = p if cluster is None else (
            min(cluster[0], p[0]), min(cluster[1], p[1]),
            max(cluster[2], p[2]), max(cluster[3], p[3]))
        frontier = p[1] if down else p[3]
        x0, x1 = nx0, nx1
    return cluster, used


def _joined(p: tuple, reg: tuple, gap_v: float, gap_h: float) -> bool:
    """[局部] 图元 p 与区域 reg 是否相邻可并（垂直近且水平重叠 / 水平近且垂直重叠）"""
    ovh = min(p[2], reg[2]) - max(p[0], reg[0])
    wmin = min(p[2] - p[0], reg[2] - reg[0])
    if (max(0.0, max(p[1] - reg[3], reg[1] - p[3])) <= gap_v
            and wmin > 0 and ovh / wmin >= 0.15):
        return True
    ovv = min(p[3], reg[3]) - max(p[1], reg[1])
    hmin = min(p[3] - p[1], reg[3] - reg[1])
    return (max(0.0, max(p[0] - reg[2], reg[0] - p[2])) <= gap_h
            and hmin > 0 and ovv / hmin >= 0.30)


def _grow_rect(prims: list[tuple], obstacles: list[tuple], region: tuple,
               skip: set, gap_v: float, gap_h: float):
    """[局部] 矩形区域生长：把与区域相邻的图元不断并入（多分图/并排图注版式）。

    垂直方向的并入仍受宽文本行阻挡（不跨正文）；水平并入只看几何相邻
    （图注在图右侧栏、图体在左中区时，中间隔着的是版心空白而非正文）。
    返回 (region, 新并入下标集合)
    """
    x0, y0, x1, y1 = region
    added: set[int] = set()
    changed = True
    while changed:
        changed = False
        for i, p in enumerate(prims):
            if i in skip or i in added:
                continue
            if (p[0] >= x0 - 0.5 and p[1] >= y0 - 0.5
                    and p[2] <= x1 + 0.5 and p[3] <= y1 + 0.5):
                continue                  # 已在区域内（种子本体），不重复计入
            if not _joined(p, (x0, y0, x1, y1), gap_v, gap_h):
                continue
            if p[3] < y0:                 # 向上并入：跨过宽文本行则不许
                lo, hi = p[3], y0
            elif p[1] > y1:               # 向下并入
                lo, hi = y1, p[1]
            else:
                lo = hi = None
            if lo is not None and any(
                    o[1] >= lo - 1 and o[3] <= hi + 1
                    and (min(x1, o[2]) - max(x0, o[0])) > 0
                    and (min(x1, o[2]) - max(x0, o[0])) / max(1e-6, o[2] - o[0])
                    >= _OBSTACLE_OVL
                    for o in obstacles):
                continue
            added.add(i)
            changed = True
            x0, y0 = min(x0, p[0]), min(y0, p[1])
            x1, y1 = max(x1, p[2]), max(y1, p[3])
    return (x0, y0, x1, y1), added


def _beside_seeds(prims: list[tuple], cap_box: tuple, band: tuple, pw: float, ph: float,
                  skip: set):
    """[局部] 图注**左右相邻**的图元（JAP 版式：图注排在右侧栏、图体在左/中区）。

    条件：与图注垂直带 band 重叠 ≥30%（相对较矮者）、水平间隙 ≤ 25% 页宽、
    图元本身"够大"（面积 ≥ 0.4% 页面积，滤掉正文里行内公式的小位图）。
    band 是图注纵向带（图注段常被骨架拆成单行，需上下外扩，见 _BESIDE_BAND_FRAC）。
    返回 (seed bbox | None, 下标集合)
    """
    hits = []
    for i, p in enumerate(prims):
        if i in skip:
            continue
        ov = min(band[1], p[3]) - max(band[0], p[1])
        hmin = min(band[1] - band[0], p[3] - p[1])
        if ov <= 0 or hmin <= 0 or ov / hmin < _BESIDE_Y_OVL:
            continue
        if p[3] <= band[0] or p[1] >= band[1]:
            continue
        gap = cap_box[0] - p[2] if p[2] <= cap_box[0] else p[0] - cap_box[2]
        if gap < 0 or gap > pw * _BESIDE_X_GAP_FRAC:
            continue
        if (p[2] - p[0]) * (p[3] - p[1]) < _SEED_MIN_AREA_FRAC * pw * ph:
            continue
        hits.append((gap, i, p))
    if not hits:
        return None, set()
    hits.sort(key=lambda t: t[0])
    base = hits[0][0]
    sel = [t for t in hits if t[0] <= base + pw * 0.08]
    idx = {i for _, i, _ in sel}
    box = (min(p[0] for _, _, p in sel), min(p[1] for _, _, p in sel),
           max(p[2] for _, _, p in sel), max(p[3] for _, _, p in sel))
    return box, idx


def _obstacle_by_page(lines: list, page_widths: dict) -> dict:
    """[局部] 每页"宽文本行" bbox（宽度 ≥ 该页栏宽 45% 的正文/标题/图注行）。

    栏宽取该页 body/heading 行宽的最大值（双栏排版≈栏宽，全宽行更大）。
    这是图片裁剪的**避让障碍**判据，也是"是否混入正文"的验收口径（测试同源）。
    """
    by_page: dict[int, list] = {}
    for ln in lines:
        if getattr(ln, "bbox", None):
            by_page.setdefault(ln.page, []).append(ln)
    out: dict[int, list] = {}
    for pno, lns in by_page.items():
        widths = [ln.bbox[2] - ln.bbox[0] for ln in lns
                  if ln.kind in ("body", "heading") and (ln.bbox[2] - ln.bbox[0]) > 10]
        col_w = max(widths) if widths else page_widths.get(pno, 595.0) * 0.8
        out[pno] = [tuple(float(v) for v in ln.bbox) for ln in lns
                    if (ln.bbox[2] - ln.bbox[0]) >= col_w * _OBSTACLE_W_FRAC
                    and (ln.bbox[3] - ln.bbox[1]) > 2]
    return out


def _text_lines_inside(box: tuple, obstacles: list[tuple],
                       frac: float = _CONTAM_FRAC) -> list[tuple]:
    """[局部] 面积 ≥frac 落在 box 内的宽文本行（= 混入正文的证据）"""
    hits = []
    for o in obstacles:
        ov = _inter_area(o, box)
        if ov <= 0:
            continue
        a = max(1e-6, (o[2] - o[0]) * (o[3] - o[1]))
        if ov / a >= frac:
            hits.append(o)
    return hits


def _trim_by_text(box: tuple, obstacles: list[tuple], min_size: float):
    """[局部] 裁剪框避开宽文本行：按行在框内偏上/偏下收缩边界。

    - 最多迭代 3 轮；收缩后仍有行残留 → 返回 None（该锚点判"无可靠图"）；
    - **保底**：收缩后高度不得低于原框一半（正文行横穿中段时宁可弃图，
      也不把一张图切成半张）。
    """
    x0, y0, x1, y1 = box
    h_orig = y1 - y0
    for _ in range(3):
        changed = False
        for o in _text_lines_inside((x0, y0, x1, y1), obstacles):
            if (o[3] - y0) <= (y1 - o[1]):
                ny0 = o[3] + _PAD
                if ny0 > y0:
                    y0, changed = ny0, True
            else:
                ny1 = o[1] - _PAD
                if ny1 < y1:
                    y1, changed = ny1, True
        if not changed:
            break
    if x1 - x0 < min_size or y1 - y0 < min_size:
        return None
    if h_orig > 0 and (y1 - y0) < h_orig * _TRIM_KEEP_FRAC:
        return None
    if _text_lines_inside((x0, y0, x1, y1), obstacles):
        return None
    return (x0, y0, x1, y1)


_PANEL_PREFIX_RE = re.compile(r"^(?:\(\s*[a-hA-H]\s*\)|\[\s*[a-hA-H]\s*\])\s*")


def _match_caption_label(text: str):
    """[局部] 图注行取编号：`_FIG_NUM_RE` ＋ **括号面板标签容错**。

    行首可能残留子图面板标签（"(c) Fig. 12. …" / "[b] Figure 7 | …"）——只剥
    **带括号**的（与 p14 `_LEAD_PANEL_RE` 同口径：裸字母不可安全剥离，会吃掉
    图注正文首字）；剥后只认 figure/fig 编号，table/scheme 一概返回 None。
    """
    s = (text or "").lstrip()
    n = 0
    while n < 3:
        p = _PANEL_PREFIX_RE.match(s)
        if not p or not _FIG_NUM_RE.match(s[p.end():]):
            break
        s, n = s[p.end():], n + 1
    m = _FIG_NUM_RE.match(s)
    if m and not m.group(1).lower().startswith("fig"):
        m = None
    return m


def extract_figures_caption_driven(pdf_path: str | Path, local_skeleton, out_dir: str | Path,
                                   dpi: int = 300,
                                   min_size: float = MIN_FIGURE_SIZE_PT,
                                   diag: dict | None = None) -> list[Figure]:
    """[全局] P14-M4 图注驱动大图提取（**纯本地**，不用 API figure 结果）：

    v2（2026-09-12，修"裁剪框混入正文"）：
      1. **锚点精化**：骨架 caption 段按 `_FIG_NUM_RE` 取编号；锚点不再无条件
         出图——必须在其上方（表注为下方）找到真实图形内容，否则判"无图"
         （PNAS 正文句 "Fig. 2 A illustrates…" 曾被骨架误标 caption → 旧版
         把整栏正文渲染成图）；
      2. **内容聚类**：位图（不限尺寸）+ 矢量 rect 作为图元，从图注向上按
         垂直间隙 ≤ 3% 页高、水平重叠 ≥ 15% 生长成簇 → 裁剪框 = 图元并集
         （矢量图/多小图拼图不再回落"整栏渲染"）；
      3. **文本避让**：生长遇到宽文本行（宽度 ≥ 栏宽 45% 的正文/标题/图注行）
         即停；成簇后再按落入框内的宽文本行收缩边界，收缩不掉则弃图；
      4. 图元/裁剪框去重（sha256 + 重叠 ≥60%）；纯本地、无 API 调用。
    v3（2026-09-17，修"并排图丢一张"）：
      骨架会把**同一 y 带的左右栏图注并成一个 caption 段**（NC p5：段 1 行 =
      "Figure 5 | …"、段 2 行 = "Figure 4 | …"，段 bbox 43.6→548.8 横跨分栏缝；
      判据 = 图注不跨栏且中间留白 ⇒ 并排两张小图）。旧版只认段首行 → Figure 4
      无锚点 → 5 张图里缺一张。改为**段内按行识别图注起始行**、每张图注独立成
      锚点（盒 = 该组行并集），故并排图各按自己栏宽生长、互不越缝。

    返回 Figure 列表（fig_id/file/caption/page/bbox/sha256）；diag 可选传入
    dict，回填 {"anchors":[...], "counts":{...}} 供取证脚本核算。
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception as exc:
        raise PaperError("PAPER-0002", stage="S4", path=str(pdf_path),
                         detail={"reason": f"pymupdf 打开失败: {exc}"}) from exc

    # 1) 图注锚点（本地骨架 caption 段）：(page, box, text, 编号, 是否表注)
    #    v3：**段内可含多张并排图注**——骨架把同一 y 带的左右栏图注并成一个段
    #    （ncomms p5：段 1 行 = "Figure 5 | …"，段 2 行 = "b Figure 4 | …"；
    #    段 bbox 43.6→548.8 横跨分栏缝）。旧版只认段首行 → Figure 4 无锚点 →
    #    永远不出图。改为按行识别"图注起始行"，每张图注独立成锚点。
    anchors: list[dict] = []
    for p in getattr(local_skeleton, "paragraphs", []) or []:
        if getattr(p, "kind", "") != "caption" or not getattr(p, "start_line", None):
            continue
        plines = [ln for ln in (getattr(p, "lines", None) or []) if getattr(ln, "bbox", None)]
        if not plines:
            continue
        groups: list[list] = []
        cur: list | None = None
        for ln in plines:
            m = _match_caption_label(getattr(ln, "text", "") or "")
            if m:
                cur = [(ln, int(m.group(2)), m.group(1).lower().startswith("table"))]
                groups.append(cur)
            elif cur is not None:
                cur.append((ln, cur[0][1], cur[0][2]))   # 续行随其起始行同组
        for g in groups:
            box = (min(ln.bbox[0] for ln, _, _ in g), min(ln.bbox[1] for ln, _, _ in g),
                   max(ln.bbox[2] for ln, _, _ in g), max(ln.bbox[3] for ln, _, _ in g))
            text = "\n".join((getattr(ln, "text", "") or "").strip() for ln, _, _ in g).strip()
            anchors.append({"page": plines[0].page, "box": box,
                            "text": text[:CAPTION_MAX_CHARS], "num": g[0][1],
                            "table": g[0][2]})
    anchors.sort(key=lambda a: (a["page"], a["box"][1]))

    # ---- ★2026-09-17 图注屏障（用户判据①）：本图上限 = **本栏上方最近一条图注的下沿**。
    # 依据："每张大图之间必有图注（图注在图下）"⇒ 同栏再往上的图注之下，绝不属于本图。
    # 这修掉了"簇已很高 + 上方还有一块全幅宽 panel"被高度比判据误杀的问题
    # （ncomms Figure 1：panel a 在 y50.8..142.2，与下方 4 块同属一张图，此前一直被丢）。
    # 同时叠加**页眉安全带**（用户判据②：页眉与图之间隔着整条正文/页边空白，空白很大，
    # 而图内 panel 间空白很小——实测 5.7~18.2pt vs 页内图间 98pt）。
    page_h_map = {i + 1: doc[i].rect.height for i in range(doc.page_count)}
    anchor_zone: list[float] = []
    for a in anchors:
        ph = page_h_map.get(a["page"], 782.0)
        zt = ph * _LOOSE_TOP_SAFE_FRAC
        ax0, _, ax1, _ = a["box"][0], a["box"][1], a["box"][2], a["box"][3]
        for b in anchors:
            if b is a or b["page"] != a["page"] or b["box"][3] > a["box"][1] + 0.5:
                continue                      # 只算**在锚点上方**的图注
            ov = min(ax1, b["box"][2]) - max(ax0, b["box"][0])
            wmin = min(ax1 - ax0, b["box"][2] - b["box"][0])
            if wmin > 0 and ov / wmin >= 0.5:  # 同栏
                zt = max(zt, b["box"][3])
        anchor_zone.append(zt)

    # 2) 页面几何 + 文本障碍行（骨架行中的"宽行"= 正文/标题/图注，用于避让）
    page_widths = {i + 1: doc[i].rect.width for i in range(doc.page_count)}
    page_heights = {i + 1: doc[i].rect.height for i in range(doc.page_count)}
    obstacle_by_page = _obstacle_by_page(
        getattr(local_skeleton, "lines", None) or [], page_widths)

    # 3) 每页图形图元（位图 + 矢量）
    graphics_by_page: dict[int, list] = {}
    for pno in range(doc.page_count):
        graphics_by_page[pno + 1] = _collect_graphics(doc[pno])

    figures: list[Figure] = []
    seen_hashes: set[str] = set()
    used_prims: dict[int, set] = {}
    emitted_boxes: dict[int, list] = {}

    def _emit(page: int, bbox: tuple, caption: str, num: int) -> bool:
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        if w < min_size or h < min_size:
            return False
        try:
            data = render_clip(pdf_path, page, bbox, dpi=dpi)
        except PaperError:
            return False
        sha = hashlib.sha256(data).hexdigest()
        if sha in seen_hashes:
            return False
        seen_hashes.add(sha)
        fig_id = "F%03d" % (len(figures) + 1)
        (out / ("%s.png" % fig_id)).write_bytes(data)
        figures.append(Figure(fig_id=fig_id, file="images/%s.png" % fig_id,
                              caption=caption or None, page=page, bbox=bbox,
                              sha256=sha))
        emitted_boxes.setdefault(page, []).append(bbox)
        return True

    recs: list[dict] = []
    ctx: dict[int, dict] = {}
    for _ai, a in enumerate(anchors):
        page = a["page"]
        rec = {"page": page, "num": a["num"], "caption": a["text"][:60],
               "kind": "table" if a["table"] else "figure",
               "action": "no_graphic", "bbox": None, "cluster": None, "via": None,
               "zone_top": round(anchor_zone[_ai], 1)}
        recs.append(rec)
        ctx[id(rec)] = {"a": a, "rec": rec,
                        "pw": page_widths.get(page, 595.0),
                        "ph": page_h_map.get(page, 800.0),
                        "prims": graphics_by_page.get(page, []),
                        "obstacles": obstacle_by_page.get(page, []),
                        "zone_top": anchor_zone[_ai]}

    def _accept(c: dict, box: tuple | None) -> bool:
        """裁剪框 → 补边 → 尺寸/重复闸门 → 文本避让 → 渲染落盘"""
        a, rec = c["a"], c["rec"]
        page, pw, ph = a["page"], c["pw"], c["ph"]
        if box is None:
            return False
        box = (max(0.0, box[0] - _PAD), max(0.0, box[1] - _PAD),
               min(pw, box[2] + _PAD), min(ph, box[3] + _PAD))
        if box[2] - box[0] < min_size or box[3] - box[1] < min_size:
            rec["action"] = "too_small"
            return False
        if any(_inter_area(box, b) / max(1e-6, min(
                (box[2] - box[0]) * (box[3] - box[1]),
                (b[2] - b[0]) * (b[3] - b[1]))) >= _DEDUP_FRAC
               for b in emitted_boxes.get(page, [])):
            rec["action"] = "duplicate"
            return False
        trimmed = _trim_by_text(box, c["obstacles"], min_size)
        if trimmed is None:
            rec["action"] = "text_contaminated"
            return False
        if not _emit(page, trimmed, a["text"], a["num"]):
            rec["action"] = "render_failed"
            return False
        rec["action"] = "emit"
        rec["bbox"] = [round(v, 1) for v in trimmed]
        return True

    try:
        # ---- 第一遍：图注上方（用户规则；表注向下找表体）----
        for c in ctx.values():
            a, rec = c["a"], c["rec"]
            page, pw, ph = a["page"], c["pw"], c["ph"]
            cap_box, prims = a["box"], c["prims"]
            gap_max = ph * _CLUSTER_GAP_FRAC
            sides = [True, False] if a["table"] else [False]
            for down in sides:
                start = cap_box[3] if down else cap_box[1]
                cluster, used = _grow_cluster(
                    prims, c["obstacles"], (cap_box[0], cap_box[2]), start,
                    gap_max, down=down, skip=used_prims.get(page, set()),
                    page_h=ph, zone_top=c.get("zone_top"))
                if cluster is None:
                    continue
                rec["cluster"] = [round(v, 1) for v in cluster]
                if _accept(c, cluster):
                    used_prims.setdefault(page, set()).update(used)
                    rec["via"] = "below" if down else "above"
                    break
        # ---- 第二遍：并排图注（JAP 版式）与反向补漏 ----
        for c in ctx.values():
            a, rec = c["a"], c["rec"]
            if rec["action"] == "emit":
                continue
            page, pw, ph = a["page"], c["pw"], c["ph"]
            cap_box, prims, obstacles = a["box"], c["prims"], c["obstacles"]
            gap_max = ph * _CLUSTER_GAP_FRAC
            done = False
            # 2a) 图注左右相邻的图体 → 矩形区域生长（并排分图）
            band = (cap_box[1] - ph * _BESIDE_BAND_FRAC,
                    cap_box[3] + ph * _BESIDE_BAND_FRAC)
            seed, seed_idx = _beside_seeds(
                prims, cap_box, band, pw, ph, used_prims.get(page, set()))
            if seed is not None:
                grown, added = _grow_rect(
                    prims, obstacles, seed, seed_idx, gap_max,
                    pw * _RECT_GAP_H_FRAC)
                rec["cluster"] = [round(v, 1) for v in grown]
                if _accept(c, grown):
                    used_prims.setdefault(page, set()).update(seed_idx | added)
                    rec["via"] = "beside"
                    done = True
            if done:
                continue
            # 2b) 反向（图注在图上方 / 表注在上方的反向版式）
            down = not a["table"]
            cluster, used = _grow_cluster(
                prims, obstacles, (cap_box[0], cap_box[2]),
                cap_box[3] if down else cap_box[1], gap_max, down=down,
                skip=used_prims.get(page, set()), page_h=ph,
                zone_top=c.get("zone_top"))
            if cluster is None:
                continue
            rec["cluster"] = [round(v, 1) for v in cluster]
            if _accept(c, cluster):
                used_prims.setdefault(page, set()).update(used)
                rec["via"] = "reverse"
    finally:
        doc.close()
    if diag is not None:
        diag["anchors"] = recs
        counts: dict[str, int] = {}
        for r in recs:
            counts[r["action"]] = counts.get(r["action"], 0) + 1
        diag["counts"] = counts
        diag["figures"] = len(figures)
    return figures
