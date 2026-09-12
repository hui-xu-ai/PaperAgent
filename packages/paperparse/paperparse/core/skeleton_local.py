#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: paperparse/core/skeleton_local.py
功能: P14 本地结构骨架（M1+M2+M3，平行实现，不触碰现有 stitch/dual 管线）：
      M1 行级提取（首页脚注区**不豁免**——所有行取出，分类阶段处理）；
      M2 结构分类 + 首页区域识别（元信息区/摘要区/正文区；Elsevier 单栏 +
         Wiley 双栏摘要两种版式）；
      M3 段落骨架（多维度拼接 + 段落开头/结尾标志封闭配对 + 跨栏/跨页封闭
         搜索 + 首页前言首段 ≥60 词约束）。
      输出 LocalSkeleton（段落边界序列 + 区域 + 统计），供 P14 后续
      M5 md 对齐 / M6 拼接修复 / M7 双通道验证消费。
对外接口: build_local_skeleton / LocalSkeleton / LocalPara
版本: v1.0.0 (2026-08-24)

设计依据（用户确认规则，PARSING-RULES-V2.md v0.6）：
- 段落边界 = 多维度综合评价：①首行缩进 ②前块尾行填满 ③句子完整性（剥离尾部
  引用编号再判终止符）④后块首词形态 ⑤类别一致性（只拼 body↔body）；
- 段落开头标志（段首缩进 + 开头多字符模糊匹配）与结尾标志（句尾终止符 + 结尾
  多字符模糊匹配）必须**成对封闭**；左栏未封闭 → 右栏自上而下搜段尾 → 右栏
  也无 → 跨页递归；
- 首页前言第一段 ≥60 单词（脚注/摘要挤压的残段不得判为完整段）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pymupdf

from paperparse.core.block_classify import is_author_line

__all__ = ["build_local_skeleton", "LocalSkeleton", "LocalPara", "LocalLine"]

# ---------- 常量 ----------
HEADER_FRAC = 0.055         # 页眉区（分类用，不豁免首页；0.08×842≈67pt 太宽，
                            # 实证 cej page2 正文 y51-62 被误判页眉→meta 丢失
                            # LP011 跨页续行；实际页眉在 y≈33 → 0.055×842≈46pt）
FOOTER_FRAC = 0.955         # 页脚区起点
CAPTION_MIN_WORDS = 8       # 图注段最少词数（一行短图注=小图，不算大图图注）
CAPTION_MIN_LINES = 2       # 图注段最少行数
FRONT_PARA_MIN_WORDS = 60   # 首页前言第一段最少单词（用户规则）
INDENT_PT = 6.0             # 首行缩进最小偏移（相对栏基准；cej 实测 ~12pt）
TAIL_FULL_PT = 3.0          # "尾行填满"判据：x1 距栏右缘 < 3pt
_REF_TAG_RE = re.compile(r"\s*[\[(]\s*\d+(?:\s*[-–—]\s*\d+)?(?:\s*[,，]\s*\d+)*\s*[)\]]\s*$")
_END_PUNCT = ".!?"
_CAPTION_START_RE = re.compile(
    r"^(fig(?:ure)?|table|scheme)\.?\s*\d+\s*[.:|]", re.IGNORECASE)
# 图注首行：编号后标点/竖线（"Figure 1 |"）/无标点但下一词大写（"Fig. 3 Schematic"）；
# 编号后直接小写字母（"Figure 4g"、"Fig. 2 h."）是正文子图引用，不误判
_CAP_NEXT_RE = re.compile(r"^(fig(?:ure)?|table|scheme)\.?\s*\d+\s*(.)", re.IGNORECASE)
_NUM_HEAD_RE = re.compile(r"^\d+(\.\d+)*\.?\s+[A-Z]")
_DECOR_HEAD_RE = re.compile(r"^(?:[A-Z]\s*){3,}$")   # "A B S T R A C T"
# 标准章节标题（无编号；正文区独立成段）
_STANDARD_HEADS = (
    "abstract", "introduction", "conclusion", "results", "discussion",
    "results and discussion", "materials and methods", "references",
    "acknowledgements", "acknowledgment", "supplementary materials",
    "conflict of interest", "declaration of competing interest",
    "data availability", "credit authorship contribution statement",
    "appendix a. supplementary data", "highlights", "keywords")
_ABSTRACT_HEAD = ("abstract", "a b s t r a c t")
_INTRO_HEAD = ("introduction", "1. introduction")
# 首页脚注区模式（* Corresponding author / E-mail / DOI / Received / 版权）
_FOOTNOTE_LINE_RE = re.compile(
    r"^(\*\s*(corresponding|e-?mail)|e-?mail address|the two authors contributed|"
    r"https?://|doi\s*[:：]|received\b|revised\b|accepted\b|available online\b|"
    r"©\s*\d{4}|\d{4}-\d{4}/©|www\.\w+\.\w{2,4}|"
    r"^[A-Z][\w\s.]*?\d{4}\s*,\s*\d{1,3}\s*,\s*\d{3,8}\s*$|"   # 期刊页脚 "Adv. Mater. 2024, 36, 2407106"
    r"^\d{5,}\s*\(\d+\s+of\s+\d+\)\s*$)", re.IGNORECASE)
_META_TAIL_RE = re.compile(
    r"(orcid|corresponding author|e-?mail address|contributed equally|"
    r"can be found under)", re.IGNORECASE)
# 机构尾行（"Xiamen 361005, China" / "Changzhou 213200, China"）——邮编+国家
_ZIP_COUNTRY_RE = re.compile(r"\b\d{4,6},\s*(china|korea|japan|usa|uk|germany|"
                             r"france|singapore|canada|australia|switzerland)$",
                             re.IGNORECASE)


@dataclass
class LocalLine:
    """[全局] 本地行级单元（M1 产物）"""
    line_id: str
    page: int
    bbox: tuple                      # x0,y0,x1,y1
    text: str
    kind: str = "body"               # body/heading/caption/meta/other
    font_size: float = 0.0
    column: int = 0
    order: int = 0
    region: str = "body"             # frontmatter/abstract/body/meta_zone
    is_footer: bool = False          # 首页脚注区行


@dataclass
class LocalPara:
    """[全局] 本地段落（M3 产物）"""
    para_id: str
    lines: list = field(default_factory=list)        # list[LocalLine]
    text: str = ""
    kind: str = "body"                               # body/heading/caption/meta
    section: str = ""
    pages: list = field(default_factory=list)
    columns: list = field(default_factory=list)
    closed: bool = False                             # 段落封闭（头尾标志配对）
    confidence: float = 1.0
    reasons: list = field(default_factory=list)      # 多维度证据
    words: int = 0

    @property
    def start_line(self) -> Optional[LocalLine]:
        return self.lines[0] if self.lines else None

    @property
    def end_line(self) -> Optional[LocalLine]:
        return self.lines[-1] if self.lines else None


@dataclass
class LocalSkeleton:
    """[全局] 本地结构骨架"""
    lines: list = field(default_factory=list)        # list[LocalLine]
    paragraphs: list = field(default_factory=list)   # list[LocalPara]
    regions: dict = field(default_factory=dict)      # {region: [line_id,...]}
    is_two_column: bool = False
    col_thresholds: dict = field(default_factory=dict)
    stats: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "regions": {k: len(v) for k, v in self.regions.items()},
            "is_two_column": self.is_two_column,
            "stats": self.stats,
            "paragraphs": [{
                "para_id": p.para_id, "kind": p.kind, "section": p.section,
                "closed": p.closed, "words": p.words, "confidence": p.confidence,
                "reasons": p.reasons, "pages": p.pages, "columns": p.columns,
                "start": (p.start_line.page, p.start_line.bbox[1]) if p.start_line else None,
                "end": (p.end_line.page, p.end_line.bbox[3]) if p.end_line else None,
                "text": p.text[:200],
            } for p in self.paragraphs],
        }


# ---------- M1 行级提取 ----------

def _join_spans(spans: list) -> str:
    """[局部] 行内 span 连接（连字符断词直连）"""
    out = ""
    for s in spans:
        st = (s.get("text") or "").strip()
        if not st:
            continue
        if out.endswith("-") and not out.endswith("--"):
            out = out[:-1] + st
        else:
            out = (out + " " + st) if out else st
    return out


def extract_lines(pdf_path: str | Path) -> tuple[list[LocalLine], dict]:
    """[全局] M1：行级提取（含字号/粗体/bbox；首页脚注区不豁免，全量取出）
    返回 (lines, page_hs)——page_hs: {page: 页高}（分类位置判据用）"""
    out: list[LocalLine] = []
    page_hs: dict[int, float] = {}
    with pymupdf.open(str(pdf_path)) as doc:
        for pno in range(doc.page_count):
            page = doc[pno]
            page_hs[pno + 1] = page.rect.height
            d = page.get_text("dict")
            for b in d.get("blocks", []):
                if b.get("type") != 0:      # 跳过图片块（M4 处理）
                    continue
                for ln in b.get("lines", []):
                    spans = ln.get("spans", [])
                    if not spans:
                        continue
                    text = _join_spans(spans).strip()
                    if not text:
                        continue
                    try:
                        bbox = tuple(float(v) for v in ln["bbox"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    fs = max((float(s.get("size", 0.0)) for s in spans), default=0.0)
                    out.append(LocalLine(
                        line_id="L%05d" % (len(out) + 1),
                        page=pno + 1, bbox=bbox, text=text,
                        font_size=fs))
    return out, page_hs


# ---------- M2 结构分类 + 区域识别 ----------

def _classify_line(ln: LocalLine, median_font: float, page_h: float = 800.0) -> str:
    """[局部] 行分类：meta → caption → heading → body（优先级）"""
    t = ln.text.strip()
    if not t:
        return "other"
    low = t.lower()
    # 页眉/页脚/页码（非首页；首页顶部可能是标题，底部是脚注区——单独判据）
    if ln.page > 1:
        if t.isdigit() or re.fullmatch(r"\d{1,3}\s+of\s+\d{1,3}", t):
            return "meta"
        if re.match(r"^[A-Z][\w\s.]*?\d{4}\s*,\s*\d{1,3}\s*,\s*\d{3,8}\s*$", t) \
                or re.match(r"^[A-Z][\w\s&\-]{3,50}\s+\d{2,4}\s*\(\d{4}\)\s+\d{2,6}$", t):
            return "meta"                       # 期刊页眉行（"Chemical Engineering Journal 522 (2025) 167798"）
        # 位置判据：顶部页眉（作者名行 "G. Tang et al." / 期刊名）→ meta
        if ln.bbox[1] < page_h * HEADER_FRAC and len(t) < 80:
            return "meta"
        if ln.bbox[3] > page_h * FOOTER_FRAC and len(t) < 40:
            return "meta"                       # 底部页码/期刊行
    # 首页脚注区行
    if _FOOTNOTE_LINE_RE.match(t) or _META_TAIL_RE.search(t) \
            or _ZIP_COUNTRY_RE.search(t.strip()):
        return "meta"
    # 邮箱/ORCID/DOI/版权/出版时间线
    if re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", t) \
            or re.search(r"orcid", low) or re.match(r"^doi\s*[:：]", low) \
            or re.search(r"©\s*\d{4}", t) \
            or re.match(r"^(received|revised|accepted|available online)\b", low):
        return "meta"
    # 作者行 / 机构行
    if is_author_line(t):
        return "meta"
    if re.search(r"\b(university|institute|school|college|laboratory|academy|"
                 r"centre|center)\b", low) and not t.rstrip().endswith((".", "!", "?")):
        return "meta"
    # 图注（Fig/Table/Scheme 开头：编号后标点/竖线/下一词大写；小写=子图引用）
    if _CAPTION_START_RE.match(t):
        return "caption"
    m = _CAP_NEXT_RE.match(t)
    if m and m.group(2).isupper():
        return "caption"
    # 列表项（"(1) Pretreatment." 悬挂缩进：首行 x0 右、续行更右——不是正文段首；
    # 单独 "(2)" 是公式编号/残留，不判列表）
    if re.match(r"^\(\d+\)\s+[A-Z]", t):
        return "list"
    # 编号标题 / 装饰节标题（A B S T R A C T）/ 标准章节标题
    if _NUM_HEAD_RE.match(t) and len(t) < 200:
        return "heading"
    if _DECOR_HEAD_RE.match(t) and len(t) < 60:
        compact = re.sub(r"\s+", "", low)
        if compact in ("abstract", "articleinfo", "keywords", "highlights"):
            return "heading"
        return "meta"
    if len(t) < 60 and low.rstrip(".").strip() in _STANDARD_HEADS:
        return "heading"
    # 大字号标题（兜底）：须首字符大写（正文中字号异常行如 "rated by the
    # hydrophobic C–F backbone. [3" font=10.84 小写开头是正文续行，非标题）
    if median_font and ln.font_size >= median_font * 1.35 and len(t) < 300 \
            and t[0].isupper():
        return "heading"
    return "body"


def _reclassify_caption_continuations(lines: list[LocalLine]) -> None:
    """[局部] 图注续行重分类（P14-LP019 修复）：caption 行之后**同页、同列、
    y 相邻（≤15pt）、无首行缩进、字号与图注一致（差 < 0.5pt）**的 body 行 =
    图注续行（实证：Fig.2 图注换行 "…c. The surface resistance of" /
    "IPS at different immersion temperatures. d. …" 被 _classify_line 误标
    body → 混入正文序列 → 锚点链式错位）。重标 caption 后由 M3 图注分组合并。
    终止条件：非 body 行、另一栏、同列 y 间隔 > 15pt、字号差 ≥ 0.5pt
    （图注 font≈7.17 vs 正文 font≈7.97，实证可分）、首行缩进（正文段首）。
    实证反例：page5 左栏图注块（y 483-511.7）下方正文续行 "increase of
    immersion time."（font 7.97、y 差 21.5pt）= LP020 跨页段在图注后的续行，
    靠 y/字号判据排除，不得并入图注。"""
    caps = [ln for ln in lines if ln.kind == "caption"]
    if not caps:
        return
    pages = sorted({c.page for c in caps})
    for page in pages:
        pl = sorted((l for l in lines if l.page == page),
                    key=lambda l: (l.bbox[1], l.bbox[0]))
        for cap in caps:
            if cap.page != page:
                continue
            started = False
            y_last = cap.bbox[3]
            for nxt in pl:
                if nxt is cap:
                    started = True
                    continue
                if not started:
                    continue
                if nxt.kind not in ("body", "other"):
                    break                # 新图注/标题/元信息行 → 图注块结束
                if nxt.column != cap.column:
                    continue             # 另一栏行（正文/图注不跨栏并）
                if nxt.bbox[1] > y_last + 15.0:
                    break                # 同列 y 间隔 > 行距 → 图注块结束
                if cap.font_size and nxt.font_size \
                        and abs(nxt.font_size - cap.font_size) >= 0.5:
                    break                # 字号不同（正文 vs 图注小字）→ 结束
                cl = _col_left(lines, nxt.page, nxt.column)
                if cl and (nxt.bbox[0] - cl) >= INDENT_PT:
                    break                # 首行缩进 = 正文段首 → 图注块结束
                nxt.kind = "caption"
                y_last = nxt.bbox[3]


def _detect_columns(lines: list[LocalLine]) -> tuple[bool, dict]:
    """[局部] 双栏检测（P14 v2：**按页独立检测** + 多数页裁决——跨页混合中心
    会被各页不同栏位填平导致 gap 失效；cej 首页三列混入也会错。"""
    from collections import defaultdict, Counter
    by_page: dict[int, list[LocalLine]] = defaultdict(list)
    for ln in lines:
        by_page[ln.page].append(ln)
    page_thr: dict[int, float] = {}
    two_pages = 0
    for page, pl in by_page.items():
        w = max(ln.bbox[2] for ln in pl) or 1.0
        narrow = sorted((ln.bbox[0] + ln.bbox[2]) / 2 for ln in pl
                        if (ln.bbox[2] - ln.bbox[0]) < w * 0.5)
        if len(narrow) < 6:
            page_thr[page] = w / 2
            continue
        gaps = [(narrow[i + 1] - narrow[i], i) for i in range(len(narrow) - 1)]
        mg, mi = max(gaps, key=lambda g: g[0])
        if mg > w * 0.12:
            page_thr[page] = (narrow[mi] + narrow[mi + 1]) / 2
            two_pages += 1
        else:
            page_thr[page] = w / 2
    total = len(by_page)
    two = two_pages >= max(1, int(total * 0.5))
    # 全局阈值 = 双栏页阈值的众数（跨页统一；未判双栏页用该众数）
    if two:
        thrs = [v for v in page_thr.values() if v > 0]
        if thrs:
            gthr = Counter(round(v, 1) for v in thrs).most_common(1)[0][0]
            page_thr = {p: gthr for p in by_page}
        else:
            two = False
    for ln in lines:
        ln.column = 1 if (two and ln.bbox[0] >= page_thr.get(ln.page, 0)) else 0
    return two, page_thr


def _detect_regions(lines: list[LocalLine]) -> dict:
    """[局部] 首页区域识别：元信息区(frontmatter) → 摘要区(abstract) → 正文区(body)
    规则：A B S T R A C T 标题或首个编号章节标题(1. Introduction)为界；
    摘要区 = Abstract 标题后到首个编号标题前的**长行**（摘要正文全宽/近全宽），
    Keywords 等短行仍归 frontmatter（同 y 交错：cej p1 Keywords x0=37.6 短行 vs
    摘要正文 x0=202 长行）。Wiley（无 Abstract 标题）→ 元信息区后到首个编号
    标题的连续行为摘要区。"""
    page1 = [ln for ln in lines if ln.page == 1]
    if not page1:
        return {"frontmatter": [], "abstract": [], "body": [ln.line_id for ln in lines]}
    fm, ab, body = [], [], []
    # 找首个编号标题下标 与 Abstract 标题下标
    first_num_idx = None
    abs_idx = None
    for i, ln in enumerate(page1):
        t = ln.text.strip()
        low = re.sub(r"\s+", "", t.lower())
        if _NUM_HEAD_RE.match(t):
            first_num_idx = i
            break
        if abs_idx is None and low == "abstract":
            abs_idx = i
    if first_num_idx is None:
        first_num_idx = len(page1)
    # 摘要区：Abstract 标题后 → 首个编号标题前；右栏行（x0 靠右）=摘要正文，
    # 左栏短行（Keywords 等 x0 靠左）=frontmatter（cej 实测：摘要 x0=202 vs
    # Keywords x0=37.6；摘要末行宽度可能短，用 x0 而非宽度判据）
    if abs_idx is not None:
        abs_x0 = page1[abs_idx].bbox[0]          # Abstract 标题 x0（右栏基准）
        for ln in page1[:abs_idx]:
            fm.append(ln.line_id)
        for ln in page1[abs_idx:first_num_idx]:
            if ln.line_id == page1[abs_idx].line_id:
                ab.append(ln.line_id)            # Abstract 标题行本身
                continue
            if ln.bbox[0] >= abs_x0 - 5.0:
                ab.append(ln.line_id)            # 右栏行 = 摘要正文
            else:
                fm.append(ln.line_id)            # 左栏短行 = Keywords 等元信息
    else:
        # 无 Abstract 标题（Wiley）：frontmatter = 大字号行（标题 fs≈2×正文、
        # 作者行 fs≈1.5×正文），其后到首个编号标题 = 摘要区
        # （adma 实测：标题 19.9pt / 作者 13.9pt / 摘要正文 9.0-9.7pt）
        med = sorted(ln.font_size for ln in page1 if ln.font_size > 0)
        med_fs = med[len(med) // 2] if med else 9.0
        cut = 0
        for i, ln in enumerate(page1[:first_num_idx]):
            big = ln.font_size >= med_fs * 1.25
            authorish = ln.kind == "meta" or _DECOR_HEAD_RE.match(ln.text.strip())
            if big or authorish:
                cut = i + 1
            else:
                break
        for ln in page1[:cut]:
            fm.append(ln.line_id)
        for ln in page1[cut:first_num_idx]:
            ab.append(ln.line_id)
    for ln in page1[first_num_idx:]:
        body.append(ln.line_id)
    body.extend(ln.line_id for ln in lines if ln.page > 1)
    for ln in lines:
        if ln.line_id in fm:
            ln.region = "frontmatter"
        elif ln.line_id in ab:
            ln.region = "abstract"
        else:
            ln.region = "body"
    return {"frontmatter": fm, "abstract": ab, "body": body}


# ---------- M3 段落骨架 ----------

def _strip_refs(text: str) -> str:
    """[局部] 剥离尾部引用编号簇（"…quality. [1]" / "…). [23]" / "[12]. " 变体）"""
    t = _REF_TAG_RE.sub("", text.rstrip())
    t = t.rstrip(")]}」』”’'\"")
    return t


def _ends_sentence(text: str) -> bool:
    """[局部] 句子完整性：先剥尾部引用编号，再剥结尾括号/引号，最后看终止符"""
    t = _strip_refs(text)
    return bool(t) and t[-1] in _END_PUNCT


def _starts_lower(text: str) -> bool:
    """[局部] 后块首词形态：小写=续接信号"""
    t = text.strip()
    if not t:
        return False
    return t[0].islower()


def _words(text: str) -> int:
    """[局部] 单词计数（只计字母词，剔数字/LaTeX/参考文献编号）"""
    return len(re.findall(r"[A-Za-z][A-Za-z'\-]*", text))


def _col_right(lines: list[LocalLine], page: int, col: int) -> float:
    """[局部] 栏右缘（该页该栏窄行 x1 **众数**——max 会被全宽行/图注拉偏，
    导致"尾行填满"误判）"""
    xs = [round(ln.bbox[2], 1) for ln in lines
          if ln.page == page and ln.column == col and (ln.bbox[2] - ln.bbox[0]) > 10]
    if not xs:
        return 0.0
    from collections import Counter
    return Counter(xs).most_common(1)[0][0]


def _col_left(lines: list[LocalLine], page: int, col: int) -> float:
    """[局部] 栏左缘（该页该栏窄行 x0 **众数**——min 会被右栏混入的异常行
    拉偏，导致每行都误判缩进而拆段）"""
    xs = [round(ln.bbox[0], 1) for ln in lines
          if ln.page == page and ln.column == col and (ln.bbox[2] - ln.bbox[0]) > 10]
    if not xs:
        return 0.0
    from collections import Counter
    return Counter(xs).most_common(1)[0][0]


def build_local_skeleton(pdf_path: str | Path) -> LocalSkeleton:
    """[全局] 主入口：PDF → M1 行级 → M2 分类/区域 → M3 段落骨架

    拼接算法（用户规则多维度综合评价）：
      - 只对 body 行拼接（heading/caption/meta 自成段落，不参与正文拼接）；
      - 潜在段首 = 首行缩进（x0 相对栏基准右移 ≥INDENT_PT）或 前段已封闭；
      - 续接 = 前段未封闭（句尾无终止符）且 后行非段首；跨栏/跨页自然续接
        （阅读序 page→column→y 排序后，左栏末行→右栏首行/下页首行）
      - 段首行同时检查"后行是否小写/填满"等维度，低置信标记 reasons。
    """
    lines, page_hs = extract_lines(pdf_path)
    if not lines:
        return LocalSkeleton(stats={"lines": 0, "paragraphs": 0})
    # M2a 分类
    sizes = [ln.font_size for ln in lines if ln.font_size > 0]
    median_font = sorted(sizes)[len(sizes) // 2] if sizes else 10.0
    for ln in lines:
        ln.kind = _classify_line(ln, median_font, page_hs.get(ln.page, 800.0))
    # M2b 双栏 + 阅读序（page → column → y → x）
    two, thr = _detect_columns(lines)
    lines.sort(key=lambda ln: (ln.page, ln.column, ln.bbox[1], ln.bbox[0]))
    for i, ln in enumerate(lines):
        ln.order = i
    # M2c 区域识别
    regions = _detect_regions(lines)
    # M2d 图注续行重分类（图注换行残段误标 body → 混入正文序列）
    _reclassify_caption_continuations(lines)

    # M3 段落拼接
    paras: list[LocalPara] = []
    cur: list[LocalLine] = []
    section = ""
    pid = 0

    def emit(kind: str = "body", sec: str = "") -> None:
        nonlocal pid, cur
        if not cur:
            return
        pid += 1
        text = " ".join(ln.text.strip() for ln in cur)
        words = _words(text)
        closed = _ends_sentence(text)
        reasons = []
        first, last = cur[0], cur[-1]
        cl = _col_left(lines, first.page, first.column)
        if cl and first.bbox[0] - cl >= INDENT_PT:
            reasons.append("indent")
        cr = _col_right(lines, last.page, last.column)
        if cr and cr - last.bbox[2] < TAIL_FULL_PT:
            reasons.append("tail_full")
        if closed:
            reasons.append("closed")
        paras.append(LocalPara(
            para_id="LP%03d" % pid, lines=cur, text=text, kind=kind,
            section=sec, pages=sorted({ln.page for ln in cur}),
            columns=sorted({ln.column for ln in cur}),
            closed=closed, confidence=1.0, reasons=reasons, words=words))
        cur = []

    def emit_all():
        """[局部] 冲刷当前累积行（按区域成段）"""
        nonlocal cur
        if not cur:
            return
        emit()

    # 按阅读序处理（page → column → y → x 已排序；abstract/frontmatter 区
    # 跨列按 y 顺序独立成段，不参与正文多维度拼接）
    front_lines: list[LocalLine] = []
    abs_lines: list[LocalLine] = []
    body_lines: list[LocalLine] = []
    cap_lines_all: list[LocalLine] = []
    for ln in lines:
        if ln.region == "frontmatter":
            front_lines.append(ln)
        elif ln.region == "abstract":
            abs_lines.append(ln)
        elif ln.kind == "caption":
            cap_lines_all.append(ln)
        elif ln.kind in ("body", "other", "list", "heading"):
            body_lines.append(ln)

    # frontmatter 区：按 y 连续段成 meta/heading 段（标题多行合并；不拼接正文）
    _fm_cur: list[LocalLine] = []
    _fm_kind = "meta"
    prev_y = -1e9
    _last_head: Optional[LocalPara] = None     # 上一 heading 段（标题续行合并用）
    for ln in sorted(front_lines, key=lambda l: (l.page, l.bbox[1], l.bbox[0])):
        if ln.kind == "heading":
            if _fm_cur:
                cur = _fm_cur; emit(_fm_kind); _fm_cur = []
            # 标题续行：与上一 heading 相邻（y 间隙 < 25pt）且字号接近 → 并入
            if _last_head and _last_head.end_line \
                    and abs(ln.bbox[1] - _last_head.end_line.bbox[3]) < 25 \
                    and abs(ln.font_size - _last_head.end_line.font_size) < 2.0:
                _last_head.lines.append(ln)
                _last_head.text = _last_head.text + " " + ln.text.strip()
                _last_head.words = _words(_last_head.text)
                _last_head.pages = sorted({x.page for x in _last_head.lines})
                _last_head.columns = sorted({x.column for x in _last_head.lines})
                continue
            cur = [ln]; emit("heading")
            _last_head = paras[-1] if paras else None
            continue
        if ln.kind == "caption":
            if _fm_cur:
                cur = _fm_cur; emit(_fm_kind); _fm_cur = []
            cur = [ln]; emit("caption")
            _last_head = None
            continue
        if _fm_cur and abs(ln.bbox[1] - prev_y) > 60:
            cur = _fm_cur; emit(_fm_kind); _fm_cur = []
        _fm_cur.append(ln)
        _fm_kind = "meta"
        _last_head = None
        prev_y = ln.bbox[1]
    if _fm_cur:
        cur = _fm_cur; emit(_fm_kind)

    # abstract 区：按列分段（Elsevier 摘要单栏一段；Wiley 摘要左右两栏并行
    # 两段——左右栏 y 同起点，各成一段）；摘要正文连续拼
    if abs_lines:
        abs_sorted = sorted(abs_lines, key=lambda l: (l.page, l.column, l.bbox[1], l.bbox[0]))
        heading_ln = abs_sorted[0] if abs_sorted[0].kind == "heading" else None
        body_abs = abs_sorted[1:] if heading_ln else abs_sorted
        if heading_ln:
            cur = [heading_ln]; emit("heading", sec="Abstract")
        if body_abs:
            # 按列分组（Wiley 双栏摘要 = 每栏一段；单栏 = 全一段）
            from collections import defaultdict as _dd
            abs_by_col: dict = _dd(list)
            for l in body_abs:
                abs_by_col[l.column].append(l)
            for col in sorted(abs_by_col):
                cur = abs_by_col[col]
                emit("abstract", sec="Abstract")

    # 正文区：多维度拼接（缩进/尾行填满/句子完整性/首词/类别）
    cur = []
    in_list = False           # 列表段内：续行强制并入（悬挂缩进行不做段首）
    for ln in body_lines:
        t = ln.text.strip()
        if not t:
            continue
        if ln.kind == "heading":
            emit_all()
            section = t
            cur = [ln]
            emit("heading", sec=section)
            in_list = False
            continue
        if ln.kind == "list":
            emit_all()                      # 列表项开新段
            cur = [ln]
            emit("list", sec=section)
            in_list = True
            continue
        if in_list:
            # 列表续行：强制并入（不受缩进/封闭影响——悬挂缩进行 x0 更右）
            lp = paras[-1] if paras else None
            if lp and lp.kind == "list":
                lp.lines.append(ln)
                lp.text = lp.text + " " + t
                lp.words = _words(lp.text)
                lp.closed = _ends_sentence(lp.text)
                lp.pages = sorted({x.page for x in lp.lines})
                lp.columns = sorted({x.column for x in lp.lines})
                if lp.closed:
                    in_list = False          # 列表项句子完整 → 后续行回正文拼接
                continue
            in_list = False
        # 公式行/残留行：短行（x1 远小于栏右缘）+ 数学特征 → 并入当前段
        cr = _col_right(lines, ln.page, ln.column)
        is_formulaish = bool(cr) and (cr - ln.bbox[2]) > 40.0 and (
            re.search(r"[=≤≥×−+]", ln.text)
            or re.search(r"[0-9]\s*[a-zA-ZπσΔ]", ln.text)
            or len(ln.text.strip()) < 25)
        if is_formulaish and cur:
            cur.append(ln)          # 公式/残留行并入当前段（不判段首）
            continue
        if not cur:
            cur = [ln]
            continue
        last_ln = cur[-1]
        # 同行多行片段：同页同栏 y 差 < 3pt（PDF 把同一视觉行拆成多个 line，
        # 如 "− as the" x0=530.6 与正文行 y 相同）→ 强制并入，不判段首
        if (ln.page == last_ln.page and ln.column == last_ln.column
                and abs(ln.bbox[1] - last_ln.bbox[1]) < 3.0):
            cur.append(ln)
            continue
        cross_col = ln.column != last_ln.column
        # 相对缩进（用户规则：首行缩进相对**同栏上一行**，非栏左缘绝对基准；
        # 无缩进期刊严格对齐 → 相对判断可靠。阈值 ≈1-2 字符宽度 ≈0.5×字号；
        # 跨栏/跨页行不判缩进（无同栏上一行可比））
        indent_pt = max(3.0, 0.5 * ln.font_size) if ln.font_size else INDENT_PT
        indented = (not cross_col and ln.page == last_ln.page
                    and ln.bbox[0] - last_ln.bbox[0] >= indent_pt)
        prev_closed = _ends_sentence(last_ln.text)
        # 尾行填满（用户规则②：前段最后一行贴栏右缘 = 句子被版面截断 → 续接）
        cr = _col_right(lines, last_ln.page, last_ln.column)
        tail_full = bool(cr) and (cr - last_ln.bbox[2]) < TAIL_FULL_PT
        # 段首/续接裁决：**前段未封闭优先续接**（悬挂缩进续行 x0 右移 13pt
        # 实证——参考文献 "[1] Y. Luo..." 首行 x0=41 续行 x0=54，若缩进
        # 优先会误判段首拆碎引用；句子未完必须续，缩进不打断）
        if not prev_closed:
            # 跨栏限制：不同栏的行须小写开头才续接（adma 首页 B 段 c0 尾
            # + A 段 c1 首行 "Electro-ionic" 大写跨栏 → 应开新段）
            if not cross_col or _starts_lower(t):
                cur.append(ln)
            else:
                emit_all()
                cur = [ln]
            continue
        if indented:
            # 前段已封闭 + 首行缩进（相对同栏上一行右移 1-2 字符宽）→ 段首
            emit_all()
            cur = [ln]
            continue
        # 前段已封闭：多维度裁决
        # tail_full 续接**不跨栏**（B 段尾行贴 c0 右缘填满 → A 段首行 c1
        # 被并入；"前栏尾行填满"不能续接另一栏的段首）
        if tail_full and not cross_col:
            cur.append(ln)          # 同栏尾行填满 → 版面截断 → 续接
            continue
        if _starts_lower(t):
            cur.append(ln)          # 首词小写 → 续行
            continue
        emit_all()                  # 封闭+未填满+大写 → 新段
        cur = [ln]
    emit_all()

    # 图注段：按编号分组合并（图注行 + 后续无缩进续行；正文区独立成段）
    if cap_lines_all:
        # 按 (page, y) 排序，同一图注的多行合并（后续行无缩进/紧跟）
        cap_sorted = sorted(cap_lines_all, key=lambda l: (l.page, l.bbox[1]))
        cap_group: list[LocalLine] = []
        for ln in cap_sorted:
            if cap_group:
                last = cap_group[-1]
                same_num = (_CAPTION_START_RE.match(cap_group[0].text)
                            or _CAP_NEXT_RE.match(cap_group[0].text))
                if (ln.page == last.page and ln.bbox[1] - last.bbox[3] < 30):
                    cap_group.append(ln)
                    continue
            if cap_group:
                cur = cap_group; emit("caption", sec=section); cap_group = []
            cap_group = [ln]
        if cap_group:
            cur = cap_group; emit("caption", sec=section)

    # 首页前言第一段 60 词约束：正文区首个段字数不足且未封闭 → 并入下一段
    # （用户规则：被脚注/摘要挤压的前言首段不会低于 60 单词）
    front_bodies = [p for p in paras if p.kind == "body" and 1 in p.pages
                    and p.start_line and p.start_line.region == "body"]
    for i, p in enumerate(front_bodies):
        if p.words < FRONT_PARA_MIN_WORDS and not p.closed and i + 1 < len(front_bodies):
            nxt = front_bodies[i + 1]
            if p.end_line and nxt.start_line and p.end_line.page == nxt.start_line.page \
                    and p.end_line.column == nxt.start_line.column:
                p.lines.extend(nxt.lines)
                p.text = p.text + " " + nxt.text
                p.words = _words(p.text)
                p.closed = _ends_sentence(p.text)
                p.pages = sorted({ln.page for ln in p.lines})
                p.columns = sorted({ln.column for ln in p.lines})
                paras.remove(nxt)
                front_bodies.remove(nxt)

    sk = LocalSkeleton(
        lines=lines, paragraphs=paras, regions=regions,
        is_two_column=two, col_thresholds=thr,
        stats={"lines": len(lines), "paragraphs": len(paras),
               "body": sum(1 for p in paras if p.kind == "body"),
               "caption": sum(1 for p in paras if p.kind == "caption"),
               "heading": sum(1 for p in paras if p.kind == "heading"),
               "closed": sum(1 for p in paras if p.closed),
               "unclosed": sum(1 for p in paras if not p.closed)})
    return sk
