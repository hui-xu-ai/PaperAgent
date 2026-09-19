#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: paperparse/core/repair_paragraphs.py
功能: P14-M6 拼接修复：本地骨架指导 mineru md 段落边界修复
      - 文本基底 = mineru md 段落（保留其文本，含 LaTeX/标签）；
      - 边界裁决 = 本地骨架（缩进/尾行填满/句子完整等维度）；
      - 低风险差异自动修（跨图断段合并、跨页断段合并、明显错拼拆分）；
      - 高风险差异进复核清单（不自动改，供 GUI）。
      输出修复后段落流 + 复核清单 + audit（before/after/证据）。
对外接口: repair_md_paragraphs / RepairResult
版本: v1.0.0 (2026-08-24)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from paperparse.core.md_align import (align_md_to_skeleton, norm_text, _dice,
                                      _tokens)
from paperparse.core.block_classify import _is_caption_line
from paperparse.core.para_align import strip_latex

__all__ = ["repair_md_paragraphs", "RepairResult", "RepairItem"]

# 自动修复阈值：md 段与本地段文本 Dice ≥ 此值视为"同一段"（可安全操作边界）
AUTO_DICE = 0.6
# 高风险：文本差异大（本地识别文本与 md 差异可能因 OCR/LaTeX，不自动改）
HIGH_RISK_DICE = 0.4
# P16：证据加权合并阈值（得分 ≥ 此值才合并）
MERGE_THRESHOLD = 3


@dataclass
class RepairItem:
    """[全局] 修复后的段落（文本取 md）"""
    para_id: str
    text: str
    kind: str = "body"          # title/heading/body/caption/image
    section: str = ""
    md_idx: list = field(default_factory=list)   # 来源 md 段 idx（可多个=合并）
    source: str = "md"          # md=直接保留 / merged=合并 / split=拆分
    confidence: float = 1.0

    def to_dict(self) -> dict:
        return {"para_id": self.para_id, "text": self.text, "kind": self.kind,
                "section": self.section, "md_idx": self.md_idx,
                "source": self.source, "confidence": self.confidence}


@dataclass
class RepairResult:
    """[全局] 修复结果"""
    paragraphs: list = field(default_factory=list)    # list[RepairItem]
    review: list = field(default_factory=list)        # 高风险差异（供 GUI 复核）
    audit: list = field(default_factory=list)         # 修复动作（before/after/证据）
    stats: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"paragraphs": [p.to_dict() for p in self.paragraphs],
                "review": self.review, "audit": self.audit,
                "stats": self.stats}


def _is_caption(text: str) -> bool:
    return bool(re.match(r"^(fig(?:ure)?|table|scheme)\.?\s*\d+", text.strip(), re.I))


def _is_heading(text: str) -> bool:
    return bool(re.match(r"^\d+(\.\d+)*\.?\s+[A-Z]", text.strip())) \
        or text.strip().startswith(("## ", "# "))


def repair_md_paragraphs(md_text: str, skeleton, *,
                         auto: bool = True) -> RepairResult:
    """[全局] 主入口：md 段落 + 本地骨架 → 修复后段落流

    修复规则（用户确认：低风险自动 + 高风险复核）：
      A. **跨图断段合并**：md 把一段拆成两段（中间夹图片标记+图注），本地骨架
         认为应合并 → 合并 md 两段（文本拼接，图注/图片标记独立保留）；
      B. **跨页断段合并**：md 段尾无终止符 + 下段首词小写（本地骨架跨页续接）→ 合并；
      C. **明显错拼拆分**：本地骨架认为 md 一段含两个独立段（段首缩进+前段封闭）
         → 拆分（按本地骨架段落边界在 md 文本中找切点）。
    复核清单：文本 Dice < HIGH_RISK_DICE 的边界差异（本地与 md 内容差异大，
    可能 OCR/LaTeX 差异导致，不自动改）。
    """
    res = align_md_to_skeleton(md_text, skeleton)
    md_paras = res.md_paras
    line_map = res.line_map
    audit: list = []
    review: list = []

    # 0) 公式段保护：$$ 块是**结构锚**（PARSING-RULES-V2 隐患10：公式行不参与
    #    正文段落拼接）。parse 把公式段判为 body → 会参与合并 → 隔段合并把
    #    公式当"中间独立段"跳过丢弃（实证：cej 公式(1) E=3.87… 在 repair 后
    #    消失，md#88"…calculated as:" + md#90"where m is…" 隔段合并吞掉 md#89）
    #    → 统一改 kind="equation"：不进 body_md（不合并），第 3 步保留输出。
    #    识别范围：$$ 块（mineru 标准）+ \[…\] 块 + 整段单 $…$ 对（mineru
    #    偶发输出单 $ 显示公式）——统一视为结构锚。
    for _p in md_paras:
        _t = (_p.text or "").strip()
        if _t.startswith("$$") \
                or (_t.startswith("\\[") and _t.endswith("\\]")) \
                or (_t.startswith("$") and _t.endswith("$")
                    and _t.count("$") == 2 and len(_t) > 2):
            _p.kind = "equation"

    # 正文区起点：首个编号章节标题（"1. Introduction"）后的段
    # （heading 的 section 字段已去 "## " 前缀）
    _body_zone_start = 0
    for _i, _p in enumerate(md_paras):
        if _p.kind == "heading" and re.match(r"^\d+(\.\d+)*\.?\s+",
                                             _p.section.strip()):
            _body_zone_start = _i + 1
            break
    # P15：References 区起点（标题后条目逐条独立，不参与合并；存 md idx）
    _ref_start = None
    for _i, _p in enumerate(md_paras):
        if _p.kind == "heading" and re.match(
                r"^\s*references?\s*$", _p.section.strip(), re.I):
            _ref_start = _p.idx
            break

    # 本地骨架 body 段（含起止行/文本）
    local_bodies = [p for p in getattr(skeleton, "paragraphs", []) or []
                    if getattr(p, "kind", "") == "body"]

    # 本地正文区行（M2 区域识别 frontmatter/abstract/body）。**正文区判定兜底**：
    # mineru 可能把 Intro 正文段排在编号标题前（实证 adma md#3 "Electro-ionic
    # soft actuators…" 在 "## 1. Introduction" 之前）→ 仅凭 md idx 会被当
    # frontmatter → 排最前 + 不参与断段合并（S1 段序错乱根因）。改判：md 段
    # 对齐行区间覆盖本地 body 区行 → 按正文区处理（合并/排序）。
    _local_body_lines = set(
        getattr(skeleton, "regions", {}).get("body", []) or [])

    def _is_body_zone(mid: int) -> bool:
        """md 段是否正文区：md idx 在首个编号标题后，或对齐行区间覆盖本地
        body 区行（mineru 把正文段排到标题前时靠本地区域兜底）"""
        if mid >= _body_zone_start:
            return True
        rng = md_line_ranges.get(mid)
        if not rng:
            return False
        _s, _e = _line_num(rng[0]), _line_num(rng[1])
        for _n in range(_s, _e + 1):
            if "L%05d" % _n in _local_body_lines:
                return True
        return False

    # 0) 假标题预处理：md heading（"## " 开头、非编号标题）若与本地 body 段
    #    文本匹配（本地把它当正文行）→ 转 body（剥 "## " 前缀）。mineru 偶把
    #    正文续行标成标题（实证：md#43 "## increase of immersion time." =
    #    LP020 段尾续行，本地已拼为 "…decreases with the increase of immersion
    #    time."）——不转则假标题保留原样且阻断与前段正文的合并。
    #    判据用**单词覆盖比例**（短词 vs 长段 Dice 会被分母稀释：5 词 vs
    #    167 词段 Dice≈0.06，但 md#43 的词全部出现在 LP020 里）
    for _p in md_paras:
        if _p.kind != "heading" or not (_p.text or "").strip().startswith("## "):
            continue
        _head_text = _p.text.strip()[3:].strip()
        if re.match(r"^\d+(\.\d+)*\.?\s+[A-Z]", _head_text):
            continue                       # 编号标题 = 真标题
        # 全大写字母间距缩写（"A B S T R A C T"/"A R T I C L E I N F O"/
        # "K E Y W O R D S"）= 真结构标题，不转 body——token 是单字母，
        # 与任何正文段覆盖 ≥0.8 必然误伤（实证：ABSTRACT 被转 body 丢 "## "，
        # ARTICLE INFO 仅因某字母缺失侥幸保留）
        if re.match(r"^(?:[A-Z] ?)+$", _head_text.strip()):
            continue
        # 标准结构/尾部章节名 = 真标题，不转 body——标题词会出现在正文/声明
        # 段里导致覆盖 ≥0.8 误伤（实证：adma "## Supporting Information" /
        # "## Data Availability Statement" 被降级为明文）
        if re.match(
                r"^(?:references?|supporting information|data availability"
                r"(?: statement)?|acknowledg(?:ement|ements)|declaration of "
                r"competing interest|credit authorship contribution statement"
                r"|keywords?|abstract|conclusion(?:s)?|appendix(?: [a-z]\.?)?"
                r"|nomenclature|author contributions?|funding|conflict of "
                r"interest)\s*$", _head_text, re.I):
            continue
        _h_toks = _tokens(norm_text(_head_text))
        if not _h_toks:
            continue
        for _lb in local_bodies:
            _lb_toks = _tokens(norm_text(_lb.text))
            if _lb_toks and len(_h_toks & _lb_toks) / len(_h_toks) >= 0.8:
                _p.kind = "body"
                _p.text = _head_text
                _p.norm = norm_text(_head_text)
                audit.append({"action": "fake_heading_to_body",
                              "md_idx": _p.idx, "text": _head_text[:80]})
                break

    # 1) 建立 md 段 → 本地段 覆盖关系：每个本地段覆盖哪些 md 段
    #    本地段 ↔ md 段：md 段对齐的行区间与该本地段行区间重叠 → 关联
    md_line_ranges = {p.idx: line_map[p.idx] for p in md_paras
                      if p.idx in line_map}
    local_of_md: dict[int, list] = {}     # md idx → [local para_id,...]

    def _line_num(line_id: str) -> int:
        try:
            return int(line_id[1:])
        except (ValueError, IndexError):
            return 0

    for p in md_paras:
        rng = md_line_ranges.get(p.idx)
        if not rng:
            continue
        s, e = _line_num(rng[0]), _line_num(rng[1])
        for lp in local_bodies:
            ls, le = _line_num(lp.start_line.line_id), _line_num(lp.end_line.line_id)
            if s <= le and ls <= e:       # 行区间重叠
                local_of_md.setdefault(p.idx, []).append(lp.para_id)

    # 文本覆盖（合并/排序共用）：md 段单词在本地段文本中的覆盖比例。
    # 行区间重叠是弱信号（对齐扩展越界会误关联）→ 用文本覆盖做强验证。
    def _covers(lp_text: str, md_norm: str) -> float:
        t_lp = _tokens(norm_text(lp_text))
        t_md = _tokens(md_norm)
        if not t_md:
            return 0.0
        return len(t_md & t_lp) / len(t_md)

    # 2) 顺序扫描 md 正文段：判定 合并/拆分/保留
    result: list[RepairItem] = []
    pid = 0
    i = 0
    # P16：独立面板标签段（"(a)"/"(c)4" 等）是子图噪声，**不参与正文邻接**——
    # 它们夹在正文与图之间会阻断跨图合并（实证 adma 问题2/6/8）。直接从
    # body 流剔除（噪声不输出）。
    # P16：拆分"图区"body 段（mineru 把 面板标签/子图标题 + 图片标记 + 图注 挤在
    # 同一 body 段，如 "(d) Dual-responsive…\n![](img)\nFigure 1. …"）→
    #   图注文本单独成 caption（防丢失），子图标题/面板标签噪声丢弃，
    #   使正文邻接干净（修 6/8 漏拼 + 图注 1/4/5/7 丢失 + 问题1）。
    # 严格判据：复用 block_classify._is_caption_line 单一来源（排除正文子图引用
    # "Fig. 2a shows" 与大写子图 "Fig. 2A illustrates"，与 md_align R10 同源）
    _CAP_POS = re.compile(r"(?:fig(?:ure)?|table|scheme)\.?\s*\d+", re.I)
    for _p in md_paras:
        if _p.kind != "body" or "![" not in (_p.text or ""):
            continue
        _cand = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", (_p.text or ""))
        _cand = re.sub(r"^(?:\([a-h]\)[\s,;]*\n?)+", "", _cand).strip()
        # 图注可能在段中（"(d) 子图标题…\nFigure 1. …"）→ 逐个图号位置测试是否真题注起点
        _m = next((mm for mm in _CAP_POS.finditer(_cand)
                   if _is_caption_line(_cand[mm.start():])), None)
        if _m:
            _p.kind = "caption"          # 图注文本独立保留（子图标题/标签丢弃）
            _p.text = _cand[_m.start():].strip()
        elif len(_cand) <= 60:
            _p.kind = "image"            # 纯图区噪声（无题注无正文）→ build 层丢弃
        # else：有实质正文但无真题注 → 保持 body（不丢弃，防正文引用段被误删）

    _PANEL_ONLY = re.compile(r"^(?:\([a-h]\)\s*(?:\d)?[\s,;]*)+$")
    _DIGIT_ONLY = re.compile(r"^\d{1,3}$")          # 独立页码噪声
    body_md = [p for p in md_paras if p.kind == "body"
               and not _PANEL_ONLY.match((p.text or "").strip())
               and not _DIGIT_ONLY.match((p.text or "").strip())]
    # 前言首段（首个编号标题后第一个 body 段）——双栏首页常被挤压跨栏，需拼
    _intro_first_idx = next((p.idx for p in body_md
                             if p.idx >= _body_zone_start), None)

    def _tail_plain(text: str) -> str:
        """[局部] 剥 LaTeX + 尾部引用/括号 → 段尾纯文本"""
        t = strip_latex(text).rstrip()
        t = re.sub(r"\s*\[[\d\s,\-–—]+\]\s*$", "", t).rstrip()
        t = re.sub(r"\([^()]*\)\s*$", "", t).rstrip()
        return t

    def _end_cls(text: str) -> str:
        """[局部] 段尾类别：formula/terminal/dash/comma/word/empty"""
        t = text.rstrip()
        if not t:
            return "empty"
        # 段尾是公式组（$...$ 或 $$..$$ 结尾，或含 \right)$ 等数学闭括号尾）
        if t.endswith("$$") or t.endswith("$"):
            return "formula"
        if re.search(r"\$[^$\n]{0,40}\$$", t[-60:]):
            return "formula"
        last = t[-1]
        if last in ".!?":
            return "terminal"
        if last == "-":
            return "dash"
        if last in ",;:":
            return "comma"
        if last.isalpha() or last.isdigit():
            return "word"
        return "other"

    _FUNC_WORD_TAIL = re.compile(
        r"\b(the|of|for|in|to|and|or|a|an|as|with|by|from|on|at|that|which|"
        r"where|when|while|because|over|under|between|than|via|through|their|"
        r"its|our|this|these|those|such|due|using|based)\s*$", re.I)

    def _py_mupdf_closed(p_abs: int) -> bool:
        """[局部] PyMuPDF "." 检测：本地骨架源文该段尾是否有结束符。
        （mineru 段尾若被公式截断常漏"."——源文有结束符说明该段实为已结束，
        移除"无结束符"的假阳性拼分。）"""
        for _pid in local_of_md.get(p_abs, []):
            _lp = next((x for x in local_bodies
                        if x.para_id == _pid), None)
            if _lp and _lp.text:
                _t = _lp.text.rstrip()
                if _t and _t[-1] in ".!?":
                    return True
        return False

    def _column_below_blank(p_abs: int) -> bool:
        """[局部] 段尾所在栏下方是否空白（未填满）→ 段落到此结束。
        取 P 最后一个本地行；同页同栏下方无正文行/图 → 空白。"""
        lps = local_of_md.get(p_abs, [])
        if not lps:
            return False
        last = None
        for _pid in lps:
            _lp = next((x for x in local_bodies
                        if x.para_id == _pid), None)
            if _lp and _lp.end_line:
                if last is None or _lp.end_line.bbox[3] > last.bbox[3]:
                    last = _lp.end_line
        if last is None:
            return False
        pg, y1, col = last.page, last.bbox[3], last.column
        # 同页同栏、y > 段尾 y 的正文行 → 下方还有文字，未到栏底
        for ln in (getattr(skeleton, "lines", []) or []):
            if ln.page == pg and ln.column == col \
                    and ln.bbox[1] > y1 + 2 and ln.text.strip():
                return False
        return True

    def _next_start_kind(nxt) -> str:
        """[局部] 续段首类别：lower/upper/formula/num"""
        t = nxt.text.strip()
        if not t:
            return "empty"
        if t.startswith(("$", "$$")):
            return "formula"
        m = re.search(r"[A-Za-z]", t)
        if not m:
            return "num"
        return "lower" if m.group(0).islower() else "upper"

    def _next_indented(n_abs: int) -> bool:
        """[局部] 续段首行是否有缩进（相对临近行）——缩进=新段起点"""
        lps = local_of_md.get(n_abs, [])
        if not lps:
            return False
        _lp = next((x for x in local_bodies
                    if x.para_id == lps[0]), None)
        if not (_lp and _lp.start_line):
            return False
        _x0 = _lp.start_line.bbox[0]
        _col = _lp.start_line.column
        _x_median = None
        xs = [ln.bbox[0] for ln in (getattr(skeleton, "lines", []) or [])
              if ln.page == _lp.start_line.page and ln.column == _col
              and getattr(ln, "kind", "") == "body"]
        if xs:
            xs.sort()
            _x_median = xs[len(xs) // 2]
        if _x_median is None:
            return False
        return _x0 > _x_median + 2.0

    def _front_intro_short(p_abs: int, p_norm: str) -> bool:
        """[局部] 前言首段（首页、首个编号标题后首段）词数过少 → 跨栏被挤压"""
        if not (p_abs >= _body_zone_start and _on_page1_by_abs(p_abs)):
            return False
        return len(p_norm.split()) < 25

    def _on_page1_by_abs(m_abs: int) -> bool:
        for _pid in local_of_md.get(m_abs, []):
            _lp = next((x for x in local_bodies
                        if x.para_id == _pid), None)
            if _lp and 1 in (_lp.pages or []):
                return True
        return False

    while i < len(body_md):
        p = body_md[i]
        # 检查 p 是否已被合并（跳过）
        # A. 跨图断段合并：md 段 p 与 p+1 之间被 image/caption 分隔，且本地
        #    同一段覆盖两者（本地认为应合并）
        merged = False
        if i + 1 < len(body_md):
            nxt = body_md[i + 1]
            # md 中 p 与 nxt 之间隔的结构块（P16：跨公式合并 bug 修复——
            # 原 gap_has_fig 把 equation/heading 也算"跨图"，导致公式被吞、
            # "where m is…" 被误拼到 "…calculated as:" 后）。
            # gap_has_fig   = 中间隔 image/caption（正文在图后继续，**可**跨合并）
            # gap_has_block = 中间隔 equation/heading（结构锚，**禁止**跨合并）
            gap_has_fig = False
            gap_has_block = False
            p_abs = p.idx
            n_abs = nxt.idx
            for mid in range(p_abs + 1, n_abs):
                _mk = md_paras[mid].kind
                if _mk in ("image", "caption"):
                    gap_has_fig = True
                # 公式/标题是段落边界：两侧正文不得跨合并
                # （heading：md#91 Data Availability 正文 + md#93 关键词文本
                #  中间隔 "## Keywords" 被短段合并吞掉——实证 adma 尾部）
                if _mk in ("equation", "heading"):
                    gap_has_block = True
                    break
            # 本地同一段覆盖 p 和 nxt（或 p 段尾未封闭 + nxt 小写开头）
            same_local = set(local_of_md.get(p_abs, [])) & \
                set(local_of_md.get(n_abs, []))
            p_norm = norm_text(p.text)
            n_norm = norm_text(nxt.text)

            local_cover_both = False
            for _pid in same_local:
                _lp = next((x for x in local_bodies
                            if x.para_id == _pid), None)
                if _lp and _covers(_lp.text, p_norm) >= 0.5 \
                        and _covers(_lp.text, n_norm) >= 0.5:
                    local_cover_both = True
                    break
            # 跨页断段：p 段尾无终止符 且 nxt 首词小写
            # （"…(Fig. 1a)" 结尾：剥引用编号 + 剥结尾括号内容后再判终止符）
            strip_p = strip_latex(p.text).rstrip()
            strip_p2 = re.sub(r"\s*\[[\d\s,\-–—]+\]\s*$", "", strip_p).rstrip()
            strip_p2 = re.sub(r"\([^()]*\)\s*$", "", strip_p2).rstrip()
            p_unclosed = bool(strip_p2) and strip_p2[-1] not in ".!?"
            # 首词小写必须看**原文**（norm 已全小写 → 恒真 bug 实证：md#33
            # "…decreases with the" + md#44 "In terms…" 大写开头被误合并）
            n_lower = bool(n_norm) and bool(
                re.match(r"[a-z]", nxt.text.strip()))
            # 短段规则（用户新增）：前段字数很少（< 40 词）且本地骨架同一段
            # 覆盖两者 → 默认合并（实证：md#56 "…was studied." 33 词 + md#57，
            # 本地 LP023 已拼为一段）——前段即使句子完整也合并
            p_short = len(p_norm.split()) < 40
            # P15：对齐失败段（line_map=None，如 md#56 "…was studied." 孤段）
            # → 文本覆盖兜底：找同时覆盖 p 和 nxt 的本地段（_covers 不依赖
            # line_map）。**只在明显断段候选（p_unclosed 或 p_short）时生效**
            # ——完整句+独立段（[45,56] "In addition…"+"Then the cation…"）
            # 不得靠本地段误合并（实证：p_unclosed=False n_lower=False 也被
            # 兜底合并）。
            if not local_cover_both and (p_unclosed or p_short):
                for _lp in local_bodies:
                    if _covers(_lp.text, p_norm) >= 0.5 \
                            and _covers(_lp.text, n_norm) >= 0.5:
                        local_cover_both = True
                        break
            # md[11] 类未对齐续段：nxt 未对齐 但 前缀与前段本地段文本匹配（兜底）
            nxt_aligned = n_abs in local_of_md
            # 正文区保护：frontmatter（标题/作者/机构/Keywords/摘要）绝不与正文
            # 合并——首个编号章节标题（"1. Introduction"）之后的段才算正文区；
            # 含图片标记的段（图注残留行 "d ![](images/…)"）不参与正文合并
            in_body_zone = _is_body_zone(p_abs) and _is_body_zone(n_abs)
            nxt_is_text = "![" not in nxt.text
            # P15：References 区保护——"## References" 标题后的参考文献条目
            # 逐条独立，绝不合并（实证：md#115 "[10] C. Zhao…" + md#116 被
            # local_cover_both 合并成一条）
            in_refs_zone = (_ref_start is not None
                            and p_abs >= _ref_start and n_abs >= _ref_start)
            # P15 修复：**跨图断段分支死代码**——`not gap_has_fig` 前置与
            # `(gap_has_fig and ...)` 分支逻辑矛盾，gap_has_fig=True 时 if 永不
            # 进入 → 跨图断段（正文段被图片区拆开，如 "…decreases with the" +
            # 图片区 + "increase of immersion time."）全部不合并。去掉前置，
            # 三个分支按各自条件独立生效（跨图：图区间隔 + 本地同段或小写续）。
            # ---- P16 加权合并评分（阈值 ≥ 3 才拼）----
            tail = _tail_plain(p.text)
            end_cls = _end_cls(tail)
            n_start = _next_start_kind(nxt)
            n_rel = bool(_FUNC_WORD_TAIL.search(nxt.text.strip()))
            score = 0
            # P 段尾
            if end_cls == "formula":
                score -= 3                                   # 公式=自足结尾
            elif end_cls == "terminal":
                score -= 1                                   # .!? 不能当不拼强依据
            elif end_cls == "dash":
                score += 3                                   # 断词必拼
            elif end_cls in ("comma", "word", "other"):
                if not _py_mupdf_closed(p_abs):              # 源文确无结束符
                    score += 3
                if _FUNC_WORD_TAIL.search(tail):
                    score += 2
            # 段尾所在栏未填满（下方空白）= 段落到此结束
            if _column_below_blank(p_abs):
                score -= 3
            # 续段开头
            if n_start == "lower":
                score += 2
            elif n_start == "formula":
                score += 1
            elif n_start == "upper":
                # 大写=新句默认不拼；仅当 P 强续接（逗号/连字符）+（本地同段/跨图）放行
                if not (end_cls in ("comma", "dash")
                        and (local_cover_both or gap_has_fig)):
                    score -= 4
            # 续段关系/连接词
            if n_rel:
                score += 2
            # 续段首行缩进
            score += -1 if _next_indented(n_abs) else 1
            # 本地同段覆盖（权威）
            if local_cover_both:
                score += 3
            # 前言首段词数过少（跨栏被挤压）
            if _front_intro_short(p_abs, p_norm):
                score += 2
            # 前言开头段（首段 + 次段同页1，双栏被挤压跨栏）→ 强拼（修问题9）
            if p_abs == _intro_first_idx and _on_page1_by_abs(p_abs) \
                    and _on_page1_by_abs(n_abs):
                score += 4
            # 短段 + 本地同段（保留原短段规则）
            if p_short and local_cover_both:
                score += 3

            if in_body_zone and nxt_is_text and not in_refs_zone \
                    and not gap_has_block and (
                    (p_short and local_cover_both)        # 原短段规则：强制合并
                    or score >= MERGE_THRESHOLD):
                # 合并：文本 = p + " " + nxt（图注/图片保留为独立段，M4 图已本地提取）
                pid += 1
                merged_text = p.text + " " + nxt.text
                result.append(RepairItem(
                    para_id="RP%03d" % pid, text=merged_text, kind="body",
                    section=p.section, md_idx=[p_abs, n_abs],
                    source="merged", confidence=0.95))
                audit.append({"action": "merge_md_paras",
                              "md_idx": [p_abs, n_abs],
                              "reason": "gap_has_fig=%s gap_has_block=%s "
                                        "local_cover_both=%s "
                                        "p_unclosed=%s n_lower=%s"
                                        % (gap_has_fig, gap_has_block,
                                           local_cover_both,
                                           p_unclosed, n_lower),
                              "before_p": p.text[:80],
                              "before_n": nxt.text[:80],
                              "after": merged_text[:120]})
                i += 2
                merged = True
                continue
        # B. 隔段合并：p 未封闭 + 后 k 段小写开头 + 与 p 覆盖**同一本地段**
        #    （文本直接匹配，不依赖 line_map——adma 双栏对齐锚选错/区间
        #    不完整，行区间重叠不可靠），中间段（i+1..k-1）是独立段（不覆盖
        #    共同本地段）→ 合并 p+k。
        #    （实证 adma 首页：md#3 A 前半 "…eﬃcient" + md#6 A 后半
        #    "method via…" 中间夹 md#5 B 段 "Artificial muscles"——mineru
        #    把 A 拆两半、B 插中间；本地 LP006 覆盖 md#3+md#6）
        if not merged and i + 2 < len(body_md):
            _p_abs = p.idx
            _strip_p = strip_latex(p.text).rstrip()
            _sp2 = re.sub(r"\s*\[[\d\s,\-–—]+\]\s*$", "", _strip_p).rstrip()
            _sp2 = re.sub(r"\([^()]*\)\s*$", "", _sp2).rstrip()
            _p_unclosed = bool(_sp2) and _sp2[-1] not in ".!?"
            if _p_unclosed and _is_body_zone(_p_abs) \
                    and (_ref_start is None or _p_abs < _ref_start) \
                    and "![" not in p.text:      # p 也不得含图片标记（md#25
                # "d ![](...)" 图注残留不得参与隔段合并）
                _p_norm = norm_text(p.text)
                for _k in range(i + 2, min(i + 5, len(body_md))):
                    _nxt = body_md[_k]
                    if "![" in _nxt.text or not re.match(
                            r"[a-z]", _nxt.text.strip()):
                        continue
                    # 跨公式禁止跳跃合并（与 A 分支 gap_has_block 同语义）。方程/标题
                    # 已从 body_md 剔除 → 本分支的 _mids_ok 只检查 body 中间段，看不到
                    # 它们 → p 与 k 会**跨方程拼接**（实证：intro"…calculated as:" +
                    # $$方程$$ + legend 误拼成 "calculated as: the parameters…"）。
                    # equation 恒拦截；heading **仅当 p 非正文区**才拦截——p 已锚定
                    # 本地正文区（mineru 把正文段误排到标题前，_is_body_zone 兜底判
                    # 正文区）→ 跨标题续接是正确修复（实证 adma md#3 "Electro-ionic…
                    # efficient" + md#6 "method via…" 跨 "## 1. Introduction" 续接，
                    # cc11589 一刀切拦截导致拼接回归）。
                    if any(md_paras[_m].kind == "equation"
                           for _m in range(_p_abs + 1, _nxt.idx)):
                        continue
                    if not _is_body_zone(_p_abs) and any(
                            md_paras[_m].kind == "heading"
                            for _m in range(_p_abs + 1, _nxt.idx)):
                        continue
                    _n_norm = norm_text(_nxt.text)
                    _common_lp = None
                    for _lp in local_bodies:
                        if _covers(_lp.text, _p_norm) >= 0.5 \
                                and _covers(_lp.text, _n_norm) >= 0.5:
                            _common_lp = _lp
                            break
                    if not _common_lp:
                        continue
                    # 中间段不得覆盖共同本地段（独立段才允许跳过）
                    _mids_ok = all(
                        _covers(_common_lp.text,
                                norm_text(body_md[j].text)) < 0.5
                        for j in range(i + 1, _k))
                    if not _mids_ok:
                        continue
                    # 中间段（i+1..k-1）= 独立段，**不得丢失**（实证 adma
                    # md#5 "Artiﬁcial muscles…" 被 B 隔段合并吞掉）：按 md
                    # 原序输出为独立 RepairItem，排序按本地锚定归位
                    for _j in range(i + 1, _k):
                        _mj = body_md[_j]
                        pid += 1
                        result.append(RepairItem(
                            para_id="RP%03d" % pid, text=_mj.text,
                            kind=_mj.kind, section=_mj.section,
                            md_idx=[_mj.idx], source="md"))
                    pid += 1
                    _merged_text = p.text + " " + _nxt.text
                    result.append(RepairItem(
                        para_id="RP%03d" % pid, text=_merged_text,
                        kind="body", section=p.section,
                        md_idx=[_p_abs, _nxt.idx], source="merged",
                        confidence=0.9))
                    audit.append({
                        "action": "merge_skip_middle",
                        "md_idx": [_p_abs, _nxt.idx],
                        "skipped": [body_md[j].idx
                                    for j in range(i + 1, _k)],
                        "local_para": _common_lp.para_id,
                        "reason": "同本地段文本覆盖+中间段独立",
                        "before_p": p.text[:60],
                        "before_n": _nxt.text[:60],
                        "after": _merged_text[:100]})
                    i = _k + 1
                    merged = True
                    break
            if merged:
                continue          # 隔段合并成功：i 已前进，回 while 顶部
        if not merged:
            pid += 1
            result.append(RepairItem(
                para_id="RP%03d" % pid, text=p.text, kind=p.kind,
                section=p.section, md_idx=[p.idx], source="md"))
        i += 1

    # 3) 标题/图注/图片段：保留 md 原样（不做段落级修复）
    for p in md_paras:
        if p.kind != "body":
            pid += 1
            result.append(RepairItem(
                para_id="RP%03d" % pid, text=p.text, kind=p.kind,
                section=p.section, md_idx=[p.idx], source="md"))

    # 3.5) ~~S3 元数据兜底（backfill_local_meta）已移除（P16）~~
    # 原逻辑从本地 PyMuPDF 骨架抓取含 university/institute/e-mail/orcid/doi/https
    # 的 meta 行拼成一个大段塞进正文——把 PDF 首页的机构/邮箱/ORCID/Creative
    # Commons 授权声明整块注入（用户确认：这些 PDF 额外抓取的作者/机构信息
    # 不需要，后续用 Web of Science 更权威干净的结果替代）。源 mineru md 越
    # 干净越容易触发，删除之。

    # 排序：**仅首页段按本地骨架阅读序重排**（同页左栏→右栏——Wiley 双栏
    # mineru 段序错位实证：adma 首页前言 A 前半→B→A 后半，本地序 B→A→C）。
    # 用户确认："一般需要排序只会在首页，其他页没有错误" → 非首页段保持
    # md 原序（mineru 其他页顺序正确，不依赖锚定——避免错误锚定污染）。
    # frontmatter 区（标题/作者/机构/Keywords/摘要）恒排最前、md 原序。
    # 锚定：查表优先（local_of_md 行区间重叠 = 位置证据，O(1)），合并段取
    # md_idx 并集；未对齐段才文本覆盖兜底（预计算 token 缓存，数量少）。
    # （原实现 140×70 全文覆盖矩阵、每次 _covers 重算 norm_text 全文 →
    # 60s 卡死——实为 1680201 引入的残缺 while 死循环；查表 → 毫秒级。
    # 阶段8 修正，用户确认方案）
    local_idx = {lp.para_id: _i for _i, lp in enumerate(local_bodies)}
    local_toks = [_tokens(norm_text(lp.text)) for lp in local_bodies]

    body_rank: dict[int, int] = {}
    md_rank: dict[int, int] = {}          # md_idx → 本地段序号
    for _it in result:
        if _it.kind != "body":
            continue
        if min(_it.md_idx or [0]) < _body_zone_start \
                and not any(_is_body_zone(_m) for _m in (_it.md_idx or [])):
            # 真 frontmatter 区段（标题/作者/机构/Keywords/摘要/ARTICLE INFO，
            # 未锚定到本地正文区）不参与本地锚定——md 的 frontmatter 顺序就是
            # 出版社正确顺序（摘要/Highlights 例外）。行区间重叠会把机构/
            # Keywords/摘要行误锚到本地段 → 混入正文（实证：cej RP005 摘要排
            # 第一）。但**锚定到本地 body 区的段（mineru 把 Intro 正文排到
            # 标题前）按正文处理**（实证 adma md#3 "Electro-ionic soft
            # actuators…" 在 "## 1. Introduction" 前）。
            continue
        _min_r = None
        for _m in (_it.md_idx or []):               # 合并段取 md_idx 并集
            for _pid in local_of_md.get(_m, []):    # 行区间重叠 → 本地段
                _ri = local_idx.get(_pid)
                if _ri is not None and (_min_r is None or _ri < _min_r):
                    _min_r = _ri
        if _min_r is None:                          # 未对齐段：文本覆盖兜底
            _t_md = _tokens(norm_text(_it.text))
            if _t_md:
                for _i, _t_lp in enumerate(local_toks):
                    if len(_t_md & _t_lp) / len(_t_md) >= 0.5:
                        _min_r = _i
                        break
        if _min_r is not None:
            body_rank[id(_it)] = _min_r
            for _m in (_it.md_idx or []):
                md_rank[_m] = _min_r

    def _rank_key(_it):
        """排序键 = (组, 组内序, md 原序)。组：
        -1 frontmatter 区（恒最前，md 原序）
         0 首页正文/首页锚定段（按本地阅读序——mineru 顺序错只发生在首页
           双栏交错场景：Wiley adma 首页前言 A→B→A 实证；用户确认"一般
           需要排序只会在首页，其他页没有错误"）
         1 其他页段（mineru 顺序正确 → 保持 md 原序，不依赖锚定）
         2 完全无锚段（md 原序沉底）"""
        _midx = min(_it.md_idx or [0])
        if _midx < _body_zone_start and not _is_body_zone(_midx):
            # 真 frontmatter 区（含摘要/Highlights）→ 恒排最前，按 md 原序；
            # 锚定到本地正文区的段（md#3 案例）走正文分支
            return (-1, 0, _midx)
        if _it.kind == "body" and id(_it) in body_rank:
            _r = body_rank[id(_it)]
            if _r is not None and _on_page1(_r):
                return (0, _r, _midx)          # 首页正文：本地阅读序
            return (1, _midx, 0)               # 其他页正文：md 原序
        _r = None
        for _m in range(_midx + 1, max(md_rank, default=0) + 2):
            if _m in md_rank:
                _r = md_rank[_m] - 0.5
                break
        if _r is not None and _on_page1(_r):
            return (0, _r, _midx)              # 锚到首页段：跟首页组
        if _r is not None:
            return (1, _midx, 0)               # 其他：md 原序
        return (2, _midx, 0)                   # 完全无锚：md 原序沉底

    def _on_page1(_r) -> bool:
        """锚定 rank 对应本地段是否在首页（page 1 含跨页段）"""
        _i = int(_r) if _r is not None and _r >= 0 else -1
        if 0 <= _i < len(local_bodies):
            return 1 in (local_bodies[_i].pages or [])
        return False

    result.sort(key=_rank_key)

    # 4) 复核清单：高风险文本差异（本地段与 md 段文本 Dice 低）
    for p in md_paras:
        rng = md_line_ranges.get(p.idx)
        if not rng:
            continue
        s, e = _line_num(rng[0]), _line_num(rng[1])
        for lp in local_bodies:
            ls, le = _line_num(lp.start_line.line_id), _line_num(lp.end_line.line_id)
            if s <= le and ls <= e:
                d = _dice(p.norm, norm_text(lp.text))
                if d < HIGH_RISK_DICE and len(p.norm) > 30:
                    review.append({"md_idx": p.idx, "local_para": lp.para_id,
                                   "dice": round(d, 3), "md_text": p.text[:150],
                                   "local_text": lp.text[:150]})
                break

    stats = {"md_body": len(body_md), "result": len(result),
             "merged": sum(1 for r in result if r.source == "merged"),
             "review": len(review), "audit": len(audit)}
    return RepairResult(paragraphs=result, review=review,
                        audit=audit, stats=stats)
