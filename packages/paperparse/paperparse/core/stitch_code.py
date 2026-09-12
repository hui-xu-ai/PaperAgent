#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: skill/src/paperparse/core/stitch_code.py
功能: S3 代码优先拼接（确定性算法，零 token）——"先分类再拼接，拼接后再校验"：
      1) 过滤 meta 行（作者/邮箱/ORCID/DOI/机构/Received 等）与噪声，绝不混入正文
      2) 按阅读顺序（页→列→y→x）合并行 → 段落候选
      3) 续接判定：前块不以终止符结尾 **或 后块以小写字母开头**（跨栏/跨页/跨图句子
         被 caption 或版面打断时也能续接）；连字符断词修复
      4) 图注（caption）遇到未完成句子时延迟插入，不打断拼接
      5) 编号标题/大字号标题 → 章节归属；拼接后做污染剥离与校验（URL/邮箱/页脚模式）
对外接口: stitch
版本: v1.1.0 (2026-08-19)
版本历史:
  v1.1.0 行级重构适配：meta 过滤、caption 延迟、段首小写续接、污染剥离校验
  v1.0.0 初始版本（块级）
"""
from __future__ import annotations

import re
from typing import Optional

from paperparse.core.block_classify import is_contaminated, strip_contamination
from paperparse.middleware.schema import LayoutInfo, Paragraph, StitchResult, TextBlock

__all__ = ["stitch"]

TERMINATORS = ".!?"
CONF_SINGLE = 0.85
CONF_SAME_COLUMN = 0.80
CONF_CROSS = 0.65
CONF_HYPHEN_BONUS = 0.05
CONF_CALIB_BONUS = 0.10
LOW_CONF_THRESHOLD = 0.70
_LOWER_START_RE = re.compile(r"^[a-z]")
_CAPTION_NUM_RE = re.compile(r"^(?:fig(?:ure)?|table|scheme)\.?\s*(\d+)", re.IGNORECASE)
_NUM_HEAD_RE = re.compile(r"^\d+(\.\d+)*\.?\s+[A-Z]")     # 独立编号标题（不并入上一标题）


def _caption_num(text: str) -> str | None:
    """[局部] 图注编号（无编号返回 None）"""
    m = _CAPTION_NUM_RE.match(text.strip())
    return m.group(1) if m else None


def _merge_captions(paragraphs: list[Paragraph]) -> None:
    """[局部] 图注编号分组合并：无编号图注续行（跨列/跨页被打断的续行）并入
    最近（同页优先，回看最多 6 段）的编号图注段，保证图注完整（用户问题 8）"""
    i = 0
    while i < len(paragraphs):
        p = paragraphs[i]
        if p.is_caption and _caption_num(p.text_en) is None:
            for j in range(i - 1, max(-1, i - 7), -1):
                q = paragraphs[j]
                if q.is_caption and _caption_num(q.text_en):
                    q.text_en = (q.text_en + " " + p.text_en).strip()
                    q.source_block_ids.extend(p.source_block_ids)
                    pages = set(q.coords.get("pages", [])) | set(p.coords.get("pages", []))
                    q.coords["pages"] = sorted(pages)
                    paragraphs.pop(i)
                    i -= 1
                    break
        i += 1


_REF_TAG_RE = re.compile(r"\s*\[\s*\d+(?:\s*[–-]\s*\d+)?\s*\]\s*$")   # 尾部引用编号 [61]


def _ends_sentence(text: str) -> bool:
    """[局部] 句子完整性：先剥离尾部引文编号（"[61]" / "[10–13]"），再去除引文括号/引号，
    最后以终止标点结束即完整句（"…Information). [61]"、"…(Figure 1b )." 均为完整句）"""
    t = _REF_TAG_RE.sub("", text.rstrip())
    t = t.rstrip(")]}」』”’'\"")
    return bool(t) and t[-1] in TERMINATORS


def _starts_lower(text: str) -> bool:
    """[局部] 句首小写（说明是句子中段被截断 → 应续接前段）"""
    return bool(_LOWER_START_RE.match(text.strip()))


def _join_blocks(blocks: list[TextBlock]) -> tuple[str, int]:
    """[局部] 合并行文本（连字符直连/空格连接），返回 (文本, 连字符修复次数)"""
    out = ""
    fixes = 0
    for b in blocks:
        bt = b.text.strip()
        if not bt:
            continue
        if out.endswith("-") and not out.endswith("--"):
            out = out[:-1] + bt
            fixes += 1
        else:
            out = (out + " " + bt) if out else bt
    return out, fixes


def _cross(prev: TextBlock, cur: TextBlock) -> bool:
    """[局部] 是否跨栏/跨页（用于置信度）"""
    return prev.page != cur.page or prev.column != cur.column


def _hanging_join(paragraphs: list[Paragraph],
                  stats: dict | None = None,
                  indent_ratio: float = 0.0) -> None:
    """[局部] 段落级悬挂续接（用户问题 9 + 首行缩进规则）：
      A) 段尾非终止符（句子被跨页/跨图/版面打断）→ 后续同章节小写开头的段落合并
      B) 段尾句子**完整** + 后续段**首行顶格（无缩进）** → 合并（仅当文档普遍使用
         首行缩进 indent_ratio>0.3：期刊排版段首缩进、续行顶格，被大图/翻页拆分的
         段落续接部分首行仍顶格；无缩进排版/合成数据不启用防误并）
      合并后置信度上限 0.8 并标记 needs_ai_check（交 AI 校验）
    参数:
        paragraphs: 段落列表（原地修改）
        stats: 统计字典（可选；indent_joins 记录缩进续接次数）
        indent_ratio: 文档段首缩进比例（0~1；<0.3 视为无缩进排版，禁用 B 规则）
    """
    i = 0
    while i < len(paragraphs) - 1:
        p = paragraphs[i]
        if (not p.is_heading and not p.is_caption and p.confidence < 1.0
                and p.text_en):
            j = i + 1
            skipped = 0
            skipped_caption = False
            while j < len(paragraphs):
                q = paragraphs[j]
                if q.is_heading or q.is_caption:
                    skipped_caption = skipped_caption or q.is_caption
                    j += 1
                    continue
                if q.section != p.section:
                    break                       # 跨章节不续接
                tail = p.coords.get("tail_pos") or []
                head = q.coords.get("head_pos") or []
                gap = (bool(tail) and bool(head) and (
                    tail[0] != head[0] or tail[1] != head[1])) or skipped_caption
                # B 规则（缩进续接）：仅限**相邻段**（中间只可隔标题/图注，不可隔正文段）。
                # 期刊排版段首缩进、续行顶格：后块首行**顶格**（first_indent=False）即续行信号，
                # 无论是否跨页/跨栏/同页同列（同页同列如 "…(Figure 2j). The observation of bright
                # spots…" 整段被拆时续接部分首行仍顶格；新段首行必缩进，故不误并）
                indent_join = (indent_ratio > 0.3 and skipped == 0
                               and _ends_sentence(p.text_en)
                               and q.coords.get("first_indent") is False)
                # C 规则（用户确认版）：**仅限 Introduction（前言）第一段**——
                # 段落字数很少（<800，被摘要/脚注挤断的残段）+ 尾行"满行"（最后一个字符
                # 后无空白，x1 贴栏右缘）→ 与相邻下一块合并。其他段落不受此规则影响。
                prev_para = paragraphs[i - 1] if i > 0 else None
                intro_first = bool(prev_para and prev_para.is_heading
                                   and "introduction" in prev_para.text_en.lower())
                short_join = (intro_first and skipped == 0
                              and len(p.text_en) < 800
                              and p.coords.get("tail_full") is True)
                if _starts_lower(q.text_en) or indent_join or short_join:
                    if stats is not None and (indent_join or short_join):
                        stats["indent_joins"] = stats.get("indent_joins", 0) + 1
                    p.text_en = p.text_en + " " + q.text_en
                    p.source_block_ids.extend(q.source_block_ids)
                    p.confidence = round(min(0.8, p.confidence), 3)
                    p.needs_ai_check = True
                    pages = set(p.coords.get("pages", [])) | set(q.coords.get("pages", []))
                    p.coords = {"pages": sorted(pages),
                                "bbox0": p.coords.get("bbox0"),
                                "bbox1": q.coords.get("bbox1"),
                                "first_indent": p.coords.get("first_indent"),
                                "tail_full": q.coords.get("tail_full"),
                                "head_pos": p.coords.get("head_pos"),
                                "tail_pos": q.coords.get("tail_pos")}
                    paragraphs.pop(j)
                    continue                    # 可能还有后续悬挂，继续找
                skipped += 1
                if skipped > 5:
                    break                       # 防误拼：最多跨过 5 个完整段
                j += 1
        i += 1


def stitch(blocks: list[TextBlock], layout: LayoutInfo,
           calibrated_ids: Optional[set[str]] = None) -> StitchResult:
    """[全局] 代码优先拼接：行 → 段落候选（章节归属 + 置信度 + 污染校验）

    参数:
        blocks: 已通过 layout.reading_order 排序的行（含 order/column/kind）
        layout: LayoutInfo（排除参考文献区）
        calibrated_ids: 已被校准 MD 验证的段落 ID 集合（T9 报告回填）
    返回:
        StitchResult（paragraphs 按阅读顺序；stats 含合并/剥离统计）
    """
    ordered = sorted((b for b in blocks if b.order is not None), key=lambda b: b.order)
    if not ordered:
        return StitchResult(stats={"block_count": 0, "paragraph_count": 0})

    ref_pages = set(layout.reference_zone_pages)
    body = [b for b in ordered if b.page not in ref_pages]
    font_of = {b.block_id: b.font_size for b in ordered}

    # 每页每列的栏左缘/右缘——只统计**窄行**（宽 < 0.5 页宽）且排除跨栏行：
    # 全宽/跨栏行（标题、Wiley 首页摘要横跨两栏 x1 达 333-347）会污染栏右缘
    # （vs 左栏正文右缘 291.7），导致"尾行满行"（被挤断）误判
    page_widths = {p: layout.page_sizes[p][0]
                   for p in layout.page_sizes if len(layout.page_sizes[p]) > 0}
    col_left: dict[tuple, float] = {}
    col_right: dict[tuple, float] = {}
    for b in ordered:
        pw = page_widths.get(b.page, 0.0)
        if pw and (b.bbox[2] - b.bbox[0]) >= pw * 0.5:
            continue                       # 全宽行（标题/摘要）不参与栏缘统计
        col = b.column if b.column is not None else 0
        thr = layout.column_thresholds.get(b.page, 0.0)
        if col == 0 and thr and b.bbox[2] > thr + 50:
            continue                       # 左栏内跨栏行（摘要横跨两栏）排除
        key = (b.page, col)
        col_left[key] = min(col_left.get(key, 1e9), b.bbox[0])
        col_right[key] = max(col_right.get(key, 0.0), b.bbox[2])

    def _indented(b: TextBlock) -> bool:
        """[局部] 行是否首行缩进（x0 明显大于栏左缘；标题/图注等全宽行不受影响）"""
        key = (b.page, b.column if b.column is not None else 0)
        left = col_left.get(key)
        return bool(left) and (b.bbox[0] - left) > 5.0

    def _tail_full(b: TextBlock) -> bool:
        """[局部] 行是否"满行"（x1 贴栏右缘，尾部无空白）——段落被版面挤断的信号"""
        key = (b.page, b.column if b.column is not None else 0)
        right = col_right.get(key)
        return bool(right) and (right - b.bbox[2]) < 3.0

    def _indented(b: TextBlock) -> bool:
        """[局部] 行是否首行缩进（x0 明显大于栏左缘；标题/图注等全宽行不受影响）"""
        key = (b.page, b.column if b.column is not None else 0)
        left = col_left.get(key)
        return bool(left) and (b.bbox[0] - left) > 5.0

    paragraphs: list[Paragraph] = []
    section = ""
    cur: list[TextBlock] = []
    pending_captions: list[TextBlock] = []
    order = 0
    stats = {"block_count": len(body), "paragraph_count": 0,
             "cross_column_joins": 0, "cross_page_joins": 0,
             "hyphen_fixes": 0, "sections": 0, "lowercase_joins": 0,
             "contamination_stripped": 0, "contaminated_remaining": 0,
             "indent_joins": 0}

    def title_font_ok(prev_para: Paragraph, cur_block: TextBlock) -> bool:
        """[局部] 标题续行字号校验：与上一标题段首行字号接近（±0.4pt）才视为续行
        （防 "3. Conclusion" 与 "Supporting Information" 等不同标题被误合并）。
        P12F：字号缺失（None）→ 不并入（宁可拆段——cej B0007 'A B S T R A C T' 曾
        因 fs=None 被并进标题段）。"""
        first_id = prev_para.source_block_ids[0] if prev_para.source_block_ids else ""
        prev_fs = font_of.get(first_id)
        return bool(prev_fs and cur_block.font_size
                    and abs(prev_fs - cur_block.font_size) <= 0.4)

    def emit_paragraph(blocks_: list[TextBlock], kind: str = "para",
                       conf_override: float | None = None,
                       section_: str = "") -> None:
        nonlocal order
        if not blocks_:
            return
        text, fixes = _join_blocks(blocks_)
        stats["hyphen_fixes"] += fixes
        cross_col = cross_page = 0
        if len(blocks_) > 1:
            for a, b in zip(blocks_, blocks_[1:]):
                if a.page != b.page:
                    cross_page += 1
                elif a.column != b.column:
                    cross_col += 1
        stats["cross_column_joins"] += cross_col
        stats["cross_page_joins"] += cross_page

        conf = conf_override if conf_override is not None else (
            CONF_SINGLE if len(blocks_) == 1 else (
                CONF_SAME_COLUMN if (cross_col == 0 and cross_page == 0) else CONF_CROSS))
        if conf_override is None:
            conf = min(0.95, conf + fixes * CONF_HYPHEN_BONUS)

        # 拼接后污染剥离与校验（正文中不可能混入 URL/邮箱/页脚标记）
        cleaned = strip_contamination(text)
        if cleaned != text:
            stats["contamination_stripped"] += 1
            text = cleaned
        if is_contaminated(text):
            stats["contaminated_remaining"] += 1
            conf = min(conf, 0.5)

        order += 1
        para_id = "P%03d" % order
        if calibrated_ids and para_id in calibrated_ids:
            conf = min(0.95, conf + CONF_CALIB_BONUS)
            calibrated = True
        else:
            calibrated = False

        paragraphs.append(Paragraph(
            para_id=para_id, order=order, section=section_,
            text_en=text,
            source_block_ids=[b.block_id for b in blocks_],
            confidence=round(conf, 3),
            needs_ai_check=conf < LOW_CONF_THRESHOLD,
            is_calibrated=calibrated,
            is_heading=(kind == "heading"),
            is_caption=(kind == "caption"),
            coords={"pages": sorted({b.page for b in blocks_}),
                    "bbox0": list(blocks_[0].bbox), "bbox1": list(blocks_[-1].bbox),
                    "first_indent": _indented(blocks_[0]),
                    "tail_full": _tail_full(blocks_[-1]),
                    "head_pos": [blocks_[0].page, blocks_[0].column if blocks_[0].column is not None else 0],
                    "tail_pos": [blocks_[-1].page, blocks_[-1].column if blocks_[-1].column is not None else 0]},
        ))

    def flush() -> None:
        nonlocal cur
        if cur:
            emit_paragraph(cur, section_=section)
        cur = []

    def flush_pending() -> None:
        nonlocal pending_captions
        if not pending_captions:
            return
        # 图注内部连续行按"编号"分组：同一编号的行合并为一个图注段，
        # 不同编号（Figure 6. 与 Figure 7.）各自独立成段，绝不互并
        groups: list[list[TextBlock]] = []
        for c in pending_captions:
            num = _caption_num(c.text)
            if groups and num is None and groups[-1]:
                groups[-1].append(c)           # 无编号续行并入当前组
            else:
                groups.append([c])             # 新编号（或无编号孤立行）开新组
        for g in groups:
            merged = " ".join(c.text.strip() for c in g).strip()
            if not merged:
                continue
            # 编号组尝试并入已有的同名编号图注段（图注跨列续行场景）
            target = None
            if _caption_num(g[0].text):
                for q in reversed(paragraphs):
                    if q.is_caption and _caption_num(q.text_en) == _caption_num(g[0].text):
                        target = q
                        break
            if target is not None:
                target.text_en = (target.text_en + " " + merged).strip()
                target.source_block_ids.extend(c.block_id for c in g)
                pages = set(target.coords.get("pages", [])) | {c.page for c in g}
                target.coords["pages"] = sorted(pages)
            else:
                emit_paragraph([g[0]], kind="caption", conf_override=1.0, section_=section)
                paragraphs[-1].text_en = merged
                paragraphs[-1].source_block_ids = [c.block_id for c in g]
        pending_captions = []

    for b in body:
        if not (b.text or "").strip():
            continue                       # P12F：空块（空 figure/占位）不进正文
        if b.kind == "meta":                 # 元信息行（作者/邮箱/ORCID/DOI/机构/Received）
            continue
        if b.kind in ("title", "heading"):
            flush()                          # 先冲刷正文缓冲：标题间不能隔着正文合并
            if (paragraphs and paragraphs[-1].is_heading
                    and not _NUM_HEAD_RE.match(b.text.strip())
                    and title_font_ok(paragraphs[-1], b)):
                # 同一标题的续行（多行标题）→ 并入上一个标题段，不拆成多个标题
                paragraphs[-1].text_en = (paragraphs[-1].text_en + " "
                                          + b.text.strip()).strip()
                paragraphs[-1].source_block_ids.append(b.block_id)
                coords = paragraphs[-1].coords
                coords["bbox1"] = list(b.bbox)
                pages = set(coords.get("pages", [])) | {b.page}
                coords["pages"] = sorted(pages)
                continue
            flush_pending()
            section = b.text.strip()
            stats["sections"] += 1
            emit_paragraph([b], kind="heading", conf_override=1.0, section_=section)
            continue
        if b.kind == "caption":
            # 图注行一律延迟插入（不打断正文句子——句子可能被大图/图注打断后继续）：
            # 正文句未完成时进 pending_captions，完成后 flush_pending 按编号分组插入
            if cur and not _ends_sentence(cur[-1].text):
                pending_captions.append(b)
            else:
                flush()
                flush_pending()
                pending_captions.append(b)
            continue
        # 正文行：仅当前句未完成（不以终止符结尾）时续接
        # （跨页/跨图/被版面打断的句子由段落级悬挂续接 _hanging_join 处理，
        #   避免"完整句 + 小写开头"的跨栏误拼，如 "applications. cability."）
        if cur and not _ends_sentence(cur[-1].text):
            cur.append(b)
        else:
            flush()
            flush_pending()
            cur = [b]
    flush()
    flush_pending()

    _merge_captions(paragraphs)
    # 缩进模式检测：段首缩进比例（>0.3 视为期刊首行缩进排版，启用 B 顶格续接规则）
    _body = [x for x in paragraphs
             if not x.is_heading and not x.is_caption
             and x.coords.get("first_indent") is not None]
    indent_ratio = (sum(1 for x in _body if x.coords.get("first_indent")) / len(_body)
                    if _body else 0.0)
    stats["indent_ratio"] = round(indent_ratio, 3)
    _hanging_join(paragraphs, stats, indent_ratio=indent_ratio)
    stats["paragraph_count"] = len(paragraphs)
    # 段首小写续接统计（跨块合并中任一对前句未完成）
    stats["lowercase_joins"] = stats["cross_column_joins"] + stats["cross_page_joins"]
    low = [p.para_id for p in paragraphs if p.needs_ai_check]
    return StitchResult(paragraphs=paragraphs, low_confidence_ids=low, stats=stats)
