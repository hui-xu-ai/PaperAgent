#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: skill/src/paperparse/core/block_classify.py
功能: 块（行）特征分类（"先分类再拼接"原则的核心）：
      - meta：作者行/作者缩写行/邮箱/ORCID/DOI 行/Received 行/机构行/版权行（不进正文）
      - caption：图/表注；heading：编号标题（2.1. ... / 1. Introduction）与大字号标题
      - title：首页大字号；body：正文；other：无法归类
      同时提供段落级污染校验工具（URL/邮箱/页脚模式——正文中不可能混入）
对外接口: classify_lines / is_author_line / strip_contamination / is_contaminated
版本: v1.0.0 (2026-08-19)
版本历史:
  v1.0.0 初始版本
"""
from __future__ import annotations

import re

from paperparse.core.pymupdf_fallback import strip_line_tail
from paperparse.middleware.schema import TextBlock

__all__ = ["classify_lines", "is_author_line", "strip_contamination", "is_contaminated"]

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ORCID_RE = re.compile(r"orcid", re.IGNORECASE)
DOI_LINE_RE = re.compile(r"^doi\s*[:：]", re.IGNORECASE)
RECEIVED_RE = re.compile(r"^(received|revised|accepted|published)\b", re.IGNORECASE)
AUTHOR_INITIALS_RE = re.compile(r"^([A-Z]\.\s+[A-Z][A-Za-z'\-]*,\s*){2,}")   # "Z. Xu, K. Deng, ..."
AFFILIATION_RE = re.compile(
    r"\b(university|institute|school|college|laboratory|academy|centre|center)\b",
    re.IGNORECASE)
CREATIVE_COMMONS_RE = re.compile(r"creative commons", re.IGNORECASE)
META_KEYWORDS = ("corresponding author", "e-mail", "email:", "tel.", "fax:",
                 "orcid identification", "received:", "revised:", "accepted:",
                 "keywords:", "key words:")
# P12F：HTML 标签剥离（作者/机构行常被 <sup>a,1</sup> 等包裹导致判定失败）
_SUP_TAG_RE = re.compile(r"<[^>]+>")
# P12F：作者标记尾缀整段剥离（"<sup>a,1</sup>" → 空）：作者行判定用的清洗文本
_SUP_CONTENT_RE = re.compile(r"<sup>.*?</sup>|<sub>.*?</sub>", re.IGNORECASE)
# 装饰节标题（全大写空格分隔）：去空格小写后是已知节 → heading（如 "A B S T R A C T"）；
# 否则（如 "A R T I C L E I N F O"）→ meta
_DECOR_HEADINGS = frozenset({
    "abstract", "introduction", "conclusion", "resultsanddiscussion", "results",
    "discussion", "materialsandmethods", "references", "acknowledgements",
    "acknowledgment", "supplementarymaterials", "conflictofinterest",
    "dataavailability", "graphicalabstract", "highlights"})
# 版权行（© 20xx）→ meta
_COPYRIGHT_RE = re.compile(r"©\s*\d{4}|rights\s+are\s+reserved", re.IGNORECASE)
# 图注首行：编号后跟 "." / ":" / "|"（Nature "Figure 1 |"）或无标点但下一词大写
# （s40820 "Fig. 1 Schematic"）；编号后直接跟**单个字母**（"Figure 4g"、"Fig. 2A illustrates"）
# 是正文子图引用，不误判（R10 放宽；2026-09-19 补：PNAS 用**大写**子图字母 A/B/C，
# 旧判据只挡小写，"Fig. 2A illustrates" 漏网被当成 Fig.2 题注 → 与真题注同图号 key，
# 渲染时 F002 被插两次）
CAPTION_RE = re.compile(r"^(Fig(ure)?|Table|Scheme)\.?\s*\d+\s*[.:|]", re.IGNORECASE)
_CAP_NEXT_RE = re.compile(r"^(fig(?:ure)?|table|scheme)\.?\s*\d+\s*(.)", re.IGNORECASE)
# 编号后（可含空格）紧跟**单个字母 + 空白/行尾** = 子图标签引用（"Fig. 2A illustrates"、
# "Fig. 2 A shows"、"Figure 4g"）→ 正文，非题注。真题注的编号后是标点（CAPTION_RE）
# 或空格+多字母大写词（"Fig. 1 Schematic"，单字母规则不匹配 → 落到 _CAP_NEXT_RE 判大写）。
_CAP_SUBFIG_RE = re.compile(
    r"^(?:fig(?:ure)?|table|scheme)\.?\s*\d+\s*[A-Za-z](?=\s|$)", re.IGNORECASE)
# 正文图引用（用户规律 2026-09-19）：图号（可带子图字母）后紧跟**指代动词**
# （shows/illustrates/presents/...）= 正文描述句（"Fig. 2A illustrates the evolution…"、
# "Figure 3 shows…"），非题注。真题注以名词短语开头（"Fig. 1. Bioinspiration…"、
# "Figure 2 | Construction…"），不会以这些第三人称动词开头。仅列**动词变位**，
# 避开 illustration/comparison/presentation/description 等题注名词（前缀不同 + \b 不误伤）。
_CAP_REF_VERB_RE = re.compile(
    r"^(?:fig(?:ure)?|table|scheme)s?\.?\s*\d+[A-Za-z]?\s+"
    r"(?:(?:is|are|was|were|be|been)\s+)?"
    r"(?:shows?|illustrates?|illustrated|illustrating|depicts?|depicted|depicting|"
    r"presents?|presented|presenting|demonstrates?|demonstrated|demonstrating|"
    r"reveals?|revealed|revealing|summarizes?|summarized|summarizing|"
    r"highlights?|highlighted|highlighting|indicates?|indicated|indicating|"
    r"displays?|displayed|displaying|reports?|reported|reporting|"
    r"describes?|described|describing|compares?|compared|comparing|"
    r"plots?|plotted|plotting|provides?|provided|providing|"
    r"appears?|appeared|appearing|shown|seen)\b",
    re.IGNORECASE)


def _is_caption_line(text: str) -> bool:
    """[局部] 图注行判定（R10）：编号后标点/竖线/大写词首；
    子图字母紧贴编号 或 编号后紧跟指代动词 = 正文图引用（非题注）"""
    t = text.strip()
    if CAPTION_RE.match(t):
        return True
    if _CAP_SUBFIG_RE.match(t):     # 子图字母引用（"Fig. 2A …"）→ 正文
        return False
    if _CAP_REF_VERB_RE.match(t):   # 指代动词引用（"Figure 3 shows …"）→ 正文
        return False
    m = _CAP_NEXT_RE.match(t)
    if m:
        return m.group(2).isupper()
    return False


# ---------------------------------------------------------------------------
# 尾部杂项（结尾声明）判据 —— en.md 干净版 / 双语主产物 / AI 全文上下文**单一来源**。
# 用户 2026-09-19 要求：供翻译/编译/问答的干净全文不含 "Declaration of Competing
# Interest"、"CRediT authorship contribution statement"、"Acknowledgements"、
# "Data availability"、"Supporting Information" 等尾部声明段。命中即从该段起截断其后全部。
# 注：paperkb.context 因两包独立另有一份同口径实现（改动需同步）。
_TAIL_NOISE_RE = re.compile(
    r"^\s*(references|bibliography|literature cited|works cited|"
    r"acknowledg(e)?ments?|conflict of interest|declarations? of (competing|conflicting) interest|"
    r"competing interests?|author contributions?|authors'? contributions?|credit authorship|"
    r"data availability|data statement|code availability|"
    r"supporting information(?!\s*(fig|figure|table|appendix|section|movie|note|scheme|"
    r"dataset|data set|ref)\b)|"
    r"supplementary (material|information|data)(?!\s*(fig|figure|table|appendix|section|movie)\b)|"
    r"associated content|additional information|author information|"
    r"ethics statement|funding|notes\b|this article references|orcid)",
    re.IGNORECASE)
# 强模式：即使出现在前半篇、只要是**短段**也认作尾部杂项（Wiley "Supporting Information"
# 声明可能排在 40% 位置）；负向前瞻排除正文交叉引用 "Supporting Information Figure S1 shows"。
_TAIL_STRONG_RE = re.compile(
    r"^\s*(references|bibliography|literature cited|works cited|"
    r"acknowledg(e)?ments?|conflict of interest|declarations? of (competing|conflicting) interest|"
    r"competing interests?|author contributions?|authors'? contributions?|credit authorship|"
    r"data availability|code availability|"
    r"supporting information(?!\s*(fig|figure|table|appendix|section|movie|note|scheme|"
    r"dataset|data set|ref)\b)|"
    r"supplementary (material|information|data)(?!\s*(fig|figure|table|appendix|section|movie)\b)|"
    r"associated content|author information|ethics statement|this article references)",
    re.IGNORECASE)
_TAIL_START_FRAC = 0.5       # 后半篇：命中原样截断
_TAIL_SHORT_CHARS = 200      # 前半篇时只有"短声明段"才认作尾部杂项


def is_tail_noise(text: str, idx: int, total: int, *,
                  is_heading: bool = False, section: str = "") -> bool:
    """[全局] 该段是否"结尾杂项"（致谢/利益冲突/作者贡献/数据可用性/支撑信息/参考文献…）。

    判据（与 paperkb.context._is_tail_noise 同口径）：
      · 先剥 markdown "## " 前缀再匹配 text，并**同时匹配 section 字段**（节名干净）；
      · **标题段无歧义**：is_heading 命中即截断（不受位置门限约束）；
      · 正文段：位于后半篇（idx ≥ 50%）命中即截断；位于前半篇时仅"短段 + 强模式"才截断
        （防正文内联引用 "Supporting Information Figure S1 shows…" 误截）。
    """
    t = re.sub(r"^#+\s*", "", (text or "")).strip()
    sec = (section or "").strip()
    if not (_TAIL_NOISE_RE.match(t) or _TAIL_NOISE_RE.match(sec)):
        return False
    if is_heading:
        return True
    if total > 4 and idx >= total * _TAIL_START_FRAC:
        return True
    return len(t) <= _TAIL_SHORT_CHARS and bool(
        _TAIL_STRONG_RE.match(t) or _TAIL_STRONG_RE.match(sec))


# R10 出版信息行（Elsevier 页眉/首页头部，首页豁免导致混入正文）→ meta
_CONTENTS_RE = re.compile(r"^contents lists available at\b", re.IGNORECASE)
_HOMEPAGE_RE = re.compile(r"^journal homepage\s*[:：]", re.IGNORECASE)
# "Chemical Engineering Journal 522 (2025) 167798"（期刊名 卷 (年) 页码）
_JOURNAL_HEADER_RE = re.compile(
    r"^[A-Z][A-Za-z &\-]{3,50}\s+\d{2,4}\s*\(\d{4}\)\s+\d{2,6}$")
# "Available online 27 August 2025"（出版时间线）
_AVAILABLE_RE = re.compile(r"^available online\b", re.IGNORECASE)
# 编号标题候选：数字后跟单词（"2.1. Electron/..." "1. Introduction"）；
# 候选词是计量单位（"2.0 Hz"、"0.5 V"）或单字母（"3.5 d"）→ 正文行，非标题
_UNITS = {"hz", "v", "mv", "kv", "uv", "mm", "um", "nm", "cm", "m", "s", "ms",
          "min", "h", "mt", "t", "a", "ma", "f", "g", "mg", "ug", "k", "pa",
          "mpa", "w", "mw", "rpm", "db", "n", "un", "mol", "mmol"}
_HEADING_CAND_RE = re.compile(r"^\d+(\.\d+)*\.?\s+([A-Z][a-z]+)")
# 名字校验：首字母大写 + ASCII/Latin-1 字母 + 缩写点/撇号/连字符（类尾 - 安全）
# + 组合重音修饰（"Cédric"）；首字符强制 ASCII 大写防正文行（"energy, Φ is ..."）误判
_AUTHOR_PART_RE = re.compile(
    r"^[A-Z][A-Za-z'.\u00c0-\u024f\u0300-\u036f-]*(?:\s+[A-Z][A-Za-z'.\u00c0-\u024f\u0300-\u036f-]*)*$")
_ZIP_COUNTRY_RE = re.compile(r"\b\d{5,6},\s*[A-Z][a-zA-Z]*\b")   # "361005, China"
_CAN_BE_FOUND_RE = re.compile(r"can be found under", re.IGNORECASE)
# P-ENHANCE R02：噪声行模式（刊头/页码/日期——header/footer 混入类）
_MASKED_CAPS_RE = re.compile(r"^(?:[A-Z]{2,}(?:\s+[A-Z]){2,}|[A-Z](?:\s+[A-Z]){2,})")
_PAGE_OF_RE = re.compile(r"^\d{1,3}\s+of\s+\d{1,3}$")
_DATE_LINE_RE = re.compile(
    r"^\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{4}\s*$",
    re.IGNORECASE)


def _is_numbered_heading(text: str) -> bool:
    """[局部] 编号标题判定：数字开头 + 候选词非计量单位/单字母（避免 "2.0 Hz" 误判）"""
    m = _HEADING_CAND_RE.match(text.strip())
    if not m:
        return False
    w = m.group(2).lower()
    return len(w) > 1 and w not in _UNITS


def is_author_line(text: str) -> bool:
    """[全局] 作者行判定：逗号分隔 2~15 个人名（每个部分首字母大写，无终止句号）

    参数:
        text: 行文本
    返回:
        是否作者行
    """
    t = _SUP_CONTENT_RE.sub("", text.strip(" *"))     # P12F：剥 <sup> 标签及其内容（作者标记）
    if not t or len(t) > 400:
        return False
    if t.rstrip().endswith((".", "!", "?")):
        return False
    parts = [p.strip(" *") for p in t.split(",") if p.strip(" *")]
    if not 2 <= len(parts) <= 15:
        return False
    for p in parts:
        if p.lower().startswith("and "):          # "*, and Dezhi Wu*"
            p = p[4:].strip()
        if not (1 < len(p) < 40 and _AUTHOR_PART_RE.match(p)):
            return False
    return True
    return True


def _classify_line(b: TextBlock, median_font: float) -> str:
    """[局部] 单行分类（优先级：meta → caption → 编号标题 → title → heading → 作者行 → body）"""
    text = b.text.strip()
    if not text:
        return "other"

    # 1) 元信息：邮箱/ORCID/版权/接收日期/DOI 行/作者缩写行/机构行/全大写短标签
    if EMAIL_RE.search(text) or ORCID_RE.search(text) or CREATIVE_COMMONS_RE.search(text):
        return "meta"
    # P12F：版权行（"© 2025 Elsevier...All rights are reserved"）→ meta
    if _COPYRIGHT_RE.search(text) and len(text) < 200:
        return "meta"
    # R10 出版信息行（Elsevier 页眉/首页头部/时间线）——首页豁免导致混入正文
    if (_CONTENTS_RE.match(text) or _HOMEPAGE_RE.match(text)
            or _JOURNAL_HEADER_RE.match(text) or _AVAILABLE_RE.match(text)):
        return "meta"
    low = text.lower()
    if any(k in low for k in META_KEYWORDS):
        return "meta"
    if RECEIVED_RE.match(text) or DOI_LINE_RE.match(text):
        return "meta"
    if AUTHOR_INITIALS_RE.match(text) and "," in text:
        return "meta"
    if is_author_line(text):                  # 作者行（提前于 title 判定，避免大字作者行误判标题）
        return "meta"
    # R02 噪声行：刊头（"S O F T RO B OT S" / "SC I EN C E | RE SEA RCH A RTIC LE"）、
    # 页码（"1 of 13"）、日期行（"26 October 2022"）——均非正文/标题
    if (_MASKED_CAPS_RE.match(text) and len(text) < 60
            and not any(ch.isdigit() for ch in text)):
        # P12F：装饰节标题（"A B S T R A C T"）→ heading；其余刊头（"A R T I C L E I N F O"）→ meta
        if _SUP_TAG_RE.sub("", text).replace(" ", "").lower() in _DECOR_HEADINGS:
            return "heading"
        return "meta"
    if _PAGE_OF_RE.match(text.strip()):
        return "meta"
    if _DATE_LINE_RE.match(text.strip()):
        return "meta"
    # 机构行（编号开头亦可，如 "1 Department of Chemical ..."；正文长句含机构词不误伤）
    # P12F：先剥 <sup> 标签及其内容（"<sup>a</sup> Jiangsu Provincial Key Laboratory..."）；
    # 上限 200→400（多机构拼接行可达 200+ 字符，cej B0004=212）
    plain = _SUP_CONTENT_RE.sub("", text)
    if (AFFILIATION_RE.search(plain) and len(text) < 400
            and not text.rstrip().endswith((".", "!", "?"))):
        return "meta"
    # 机构注记污染：邮编+国家（"361005, China"）、"can be found under"（Wiley 机构注页脚）
    if _CAN_BE_FOUND_RE.search(text) or (_ZIP_COUNTRY_RE.search(text) and len(text) < 80):
        return "meta"
    # 全大写短标签（出版商标签如 RESEARCH ARTICLE / FULL PAPER，非标题）
    if (len(text) <= 40 and not any(ch.isdigit() for ch in text)
            and re.fullmatch(r"[A-Z][A-Z0-9\s&\-]*", text)):
        return "meta"

    # 2) 图/表注（R10 放宽：编号后标点/竖线/大写首词）
    if _is_caption_line(text):
        return "caption"

    # 3) 编号标题（先于字号判断，行级后标题与正文分离）
    if _is_numbered_heading(text) and len(text) < 200:
        return "heading"

    # 4) 首页大字号标题
    if b.page == 1 and (b.font_size or 0) >= median_font * 1.35 and len(text) < 300:
        return "title"

    # 5) 大字号标题（非作者行）。P12F：长度上限 200→60——adma 首页宣传语行
    # （"Eﬃcient ion transport and enriched responsive modals..." ≈70 字符）曾因
    # 大字号被误判 heading；节标题（"Results and Discussion"/"Supporting Information"）
    # 均 <60，不受影响
    if (b.font_size or 0) >= median_font * 1.2 and len(text) < 60:
        return "heading"

    # 6) 作者行（不进正文，供元数据提取）
    if is_author_line(text):
        return "meta"

    return "body"


def classify_lines(blocks: list[TextBlock], median_font: float | None = None) -> None:
    """[全局] 给所有行赋值 kind（原地修改；序列感知：标题续行并入 heading）

    参数:
        blocks: 行级 TextBlock 列表
        median_font: 中位字号（None=自动计算；正文主导）
    """
    if median_font is None:
        sizes = [b.font_size for b in blocks if b.font_size and b.font_size > 0]
        import statistics
        median_font = statistics.median(sizes) if sizes else 10.0
    prev_kind = "other"
    prev_font = 0.0
    first_body_done = False
    for b in blocks:
        # P12F：尊重云端 header/footer 标注（首页豁免曾让页眉混入正文 → 一律 meta 不进正文）
        if b.kind in ("header", "footer"):
            b.kind = "meta"
            prev_kind = b.kind
            prev_font = b.font_size or prev_font
            continue
        b.kind = _classify_line(b, median_font)
        # P12F：首页首个非空 body 块 → 论文标题（无字号依赖；
        # 云端页眉已被排除，首个 body 即标题或摘要正文——标题通常在前）
        if (b.kind == "body" and not first_body_done and b.page == 1
                and 15 <= len(b.text.strip()) <= 250
                and not b.text.rstrip().endswith((".", "!", "?"))):
            b.kind = "title"
        if b.kind == "title" and b.page == 1:
            # 首页标题已分配（无论大字号规则或上述 body 规则）——后续行不再标 title
            # （adma 宣传语行曾因 first_body_done 未在"大字号 title"路径设置而误标）
            first_body_done = True
        if b.kind in ("body", "other"):
            # 标题续行：上一行是标题
            # - 大字号标题（首页长标题换行）：本行与标题同字号、较短
            # - 编号标题（与正文同字号，如 "2.1. ..." + "Soft Actuator"）：仅按短行判定
            # - 以 "-" 结尾的行是正文跨行断词（如 "…and ion stor-"），绝不并入标题
            if prev_kind in ("heading", "title") and prev_font \
                    and not b.text.rstrip().endswith("-"):
                is_big_title = prev_font >= median_font * 1.3
                if is_big_title:
                    # P12F：大字号标题续行上限 80→60——adma 首页宣传语行
                    # （"Eﬃcient ion transport and enriched responsive modals..." ≈70 字符）
                    # 曾因宽松上限被当标题续行 → 独立成 heading（标题数 +1）
                    cont_ok = (b.font_size and b.font_size >= prev_font * 0.9
                               and len(b.text.strip()) < 60)
                else:
                    # 编号标题续行上限收紧（R02）：正文首句（如 "These phenomena were
                    # further confirmed with a two-phase..." 55 字符）不再误并入标题
                    cont_ok = len(b.text.strip()) < 48
                if (cont_ok and b.kind in ("body", "other")
                        and not b.text.rstrip().endswith((".", "!", "?"))):
                    b.kind = "heading"
            # 图注续行：上一行是图注，且本行字号小于正文（图注常小一号字，如 fs=8 vs 正文 9；
            #   子图说明可能大写开头，故不按大小写判断；字号主判据避免吞并正文）
            elif prev_kind == "caption" and b.font_size and median_font \
                    and b.font_size < median_font * 0.96:
                b.kind = "caption"
        prev_kind = b.kind
        prev_font = b.font_size or prev_font


# ---------- 段落污染校验（拼接后） ----------

_CONTAM_RE = [
    EMAIL_RE,
    re.compile(r"https?://\S+", re.IGNORECASE),
    re.compile(r"www\.\S+", re.IGNORECASE),
    CREATIVE_COMMONS_RE,
    re.compile(r"©\s*\d{4}"),
    re.compile(r"\d{5,},\s*\d{4},\s*\d{1,2}\b"),     # 页脚 DOI 数字序列
]


def strip_contamination(text: str) -> str:
    """[全局] 段落级污染剥离：URL/邮箱/页脚标记/版权（用户问题 5/6/12 的兜底）"""
    out = strip_line_tail(text)
    for pat in _CONTAM_RE:
        out = pat.sub(" ", out)
    return re.sub(r"\s{2,}", " ", out).strip()


def is_contaminated(text: str) -> bool:
    """[全局] 段落是否仍含污染特征（拼接后校验；含污染 → needs_ai_check）"""
    return any(p.search(text) for p in _CONTAM_RE)
