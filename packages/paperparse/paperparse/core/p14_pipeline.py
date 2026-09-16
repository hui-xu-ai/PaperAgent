#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: paperparse/core/p14_pipeline.py
功能: P14 管线 process_pdf_v2：M1-M7（本地骨架权威边界 + mineru 文本基底 +
      拼接修复 + 双通道整段验证）+ M8（段落级字符仲裁，复用 dual_ai_review）→
      修复后 markdown + document.json（信封兼容 process_pdf）。
对外接口: process_pdf_v2 / build_markdown / char_conflicts
版本: v1.0.0 (2026-08-25)

流程:
  1. M1-M3  build_local_skeleton(pdf)   → 本地段落骨架（权威边界）
  2. M4     extract_figures_caption_driven → 大图本地提取（图注驱动）
  3. M5     align_md_to_skeleton        → md 段 ↔ 本地行对齐
  4. M6     repair_md_paragraphs        → 修复段落流（文本取 md）
  5. M7     verify_pair（修复段 ↔ paddleocr 段）→ 验证信号（仅提示）
  6. M8     char_conflicts + arbitrate   → 段落内字符差异 AI 仲裁 → 应用
  7. 组装   markdown + document.json

配对（修复段 ↔ paddleocr）：
  修复段.md_idx → 本地段（行区间重叠）→ 本地段行(page,y) → 同页 y 重叠的
  paddleocr 正文块合并 = 该段 OCR 对照文本。
"""
from __future__ import annotations

import difflib
import json
import re
import time
from pathlib import Path

from paperparse.core.skeleton_local import build_local_skeleton
from paperparse.core.md_align import (align_md_to_skeleton, parse_md_paragraphs,
                                      norm_text, _dice, _tokens)
from paperparse.core.repair_paragraphs import repair_md_paragraphs
from paperparse.core.para_verify import verify_pair
from paperparse.core.image_extract import extract_figures_caption_driven
from paperparse.core.latex_normalize import (normalize_formula_fragments,
                                             wrap_orphan_latex)
from paperparse.core.consensus_fix import apply_domain_fix, find_boundary_candidates
from paperparse.middleware.schema import (ArticleDocument, ArticleMetadata,
                                          DocumentAudit, Figure, Paragraph)

__all__ = ["process_pdf_v2", "build_markdown", "char_conflicts",
           "to_article_document"]

# paddleocr 噪声块角色（不参与段落对照）
_PADDLE_NOISE = {"header", "footer", "page_number", "page-header",
                 "page-footer", "footnote", "page-number"}


def _norm_format(t: str) -> str:
    """[局部] 格式表示归一化（上标标签/Unicode 符号/LaTeX 命令 → 同一文本），
    用于判定"仅表示差异"（如 <sup>−</sup> vs $^{-}$）。
    2026-08-26 从 dual_pipeline 提取（该模块已归档，P14 唯一使用者）。"""
    t = re.sub(r"</?sup>", "", t)
    t = re.sub(r"</?sub>", "", t)
    for src, dst in zip("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789"):
        t = t.replace(src, dst)
    for src, dst in zip("₀₁₂₃₄₅₆₇₈₉", "0123456789"):
        t = t.replace(src, dst)
    t = t.replace("µ", "mu")
    t = t.replace("$", "")
    for u, a in (("–", "-"), ("−", "-"), ("—", "-"), ("≈", "~"),
                 ("±", "+-"), ("×", "x"), ("·", "*"), ("∙", "*"), ("•", "*"),
                 ("≤", "<="), ("≥", ">="), ("≠", "!="), ("Δ", "Delta"),
                 ("α", "alpha"), ("β", "beta"), ("γ", "gamma"), ("μ", "mu"),
                 ("π", "pi"), ("θ", "theta"), ("Ω", "Omega")):
        t = t.replace(u, a)
    t = re.sub(r"\\approx", "~", t)
    t = re.sub(r"\\cdot", "*", t)
    t = re.sub(r"\\times", "x", t)
    t = re.sub(r"\\left|\\right", "", t)
    t = re.sub(r"\\mathrm\{([^}]*)\}", r"\1", t)
    t = re.sub(r"\\text\{([^}]*)\}", r"\1", t)
    t = re.sub(r"\\,", "", t)
    t = re.sub(r"\^\{([^}]*)\}", r"\1", t)
    t = re.sub(r"_\{([^}]*)\}", r"\1", t)
    t = t.replace("^", "").replace("_", "")
    t = t.replace("\\", "").replace("{", "").replace("}", "")
    t = re.sub(r"\s+", "", t)
    return t
# M8 仲裁项最小长度（过短片段误判率高）
_MIN_CHUNK = 4
# M7 验证阈值（仅信号）
_VERIFY_OK = 0.9
# 图面板标签（P16：Wiley adma 把 (a)(b)(c)4… 子图面板标签单独当正文文本）
_LEAD_PANEL_RE = re.compile(r"^(?:\([a-h]\)[\s,;]*\n?)+")
_PANEL_LABEL_RE = re.compile(r"(?<![A-Za-z0-9])\([a-h]\)(?:\d)?(?![A-Za-z0-9])")


def _strip_panel_labels(text: str) -> str:
    """[局部] 剥离正文段的孤立图面板标签 "(a)" "(b)" "(c)4"…（段首 + 全位置；
    前后不邻字母数字才算孤立）。图注段保留子图编号，故只对 body 调用。
    剥后空段由调用方删除。"""
    text = _LEAD_PANEL_RE.sub("", text)
    text = _PANEL_LABEL_RE.sub("", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _is_isolated_short_char(text: str) -> bool:
    """[局部] 段落是否**孤立短字符噪声**（如 "d"、"b."、"1"、"Co" 单独成段）——
    单/短字符不可能成段落（无此类排版）；剥空白/标点/公式/LaTeX 后，字母+数字 ≤2
    → 判噪声删除。用户确认：无合法单字符段落，无需反例（有词的长段不受影响）。"""
    t = re.sub(r"\$[^$]*\$|\\[a-zA-Z]+|\s|[(){}[\].,;:!?'\"\-]", "", text or "")
    if not t:
        return False
    return len(re.findall(r"[A-Za-z0-9]", t)) <= 2


# 2026-08-26：正文引用编号上标化 [n]/[n,m]/[n–m] → <sup>[n]</sup>
_REF_SUP_PROT = re.compile(r"<sup>\[\s*\d+(?:\s*[,–—-]\s*\d+)*\s*\]</sup>")
_REF_SUP_BARE = re.compile(
    r"(?<![\d\]])\[\s*\d+(?:\s*[,–—-]\s*\d+)*\s*\](?![\d\]])")


def _sup_inline_refs(text: str) -> str:
    """[局部] 正文引用编号统一上标（mineru 部分漏转 <sup>[n]</sup>；已上标的
    占位保护不动；仅 body 段调用——参考文献区/表格由 build_markdown 分流）。"""
    prot: list[str] = []

    def _save(m):
        prot.append(m.group(0))
        return "\u0006%d\u0006" % (len(prot) - 1)

    def _rest(m):
        return prot[int(m.group(1))]

    t = _REF_SUP_PROT.sub(_save, text)
    t = _REF_SUP_BARE.sub(lambda m: "<sup>%s</sup>" % m.group(0), t)
    return re.sub(r"\u0006(\d+)\u0006", _rest, t)


def _fallback_merge_intro_first(md_text: str, threshold: int = 80) -> str:
    """[局部] 保底改善（问题9）：前言第一段字数 < 阈值 → 与下一段拼接。

    双栏首页前言常被挤压跨栏，首段过短（缺后半）→ 直接合并补全。
    只作用于首个 "1. Introduction" 后的第一段正文；仅拼接、不改文本。
    """
    blocks = re.split(r"\n\s*\n", md_text)
    intro_i = next((i for i, b in enumerate(blocks) if re.match(
        r"^#{1,6}\s*\d+(\.\d+)*\.?\s*[Ii]ntroduction\b", b.strip())), None)
    if intro_i is None:
        return md_text
    p_i = intro_i + 1
    while p_i < len(blocks) and (not blocks[p_i].strip()
                                 or blocks[p_i].strip().startswith(("#", "!", "|"))):
        p_i += 1
    if p_i + 1 >= len(blocks):
        return md_text
    first = blocks[p_i].strip()
    nxt = blocks[p_i + 1].strip()
    if not first or not nxt or nxt.startswith(("#", "!", "|")):
        return md_text
    if len(re.findall(r"[A-Za-z]+", first)) >= threshold:
        return md_text
    blocks[p_i] = first + " " + nxt
    blocks[p_i + 1] = ""
    return "\n\n".join(b for b in blocks if b.strip())


def _merge_intro_first_in_doc(doc, threshold: int = 80) -> None:
    """[局部] document.json 级保底：前言第一段过短(<阈值) → 与下一正文段合并。
    与 _fallback_merge_intro_first 保持同步（en.md 与 document 一致）。"""
    paras = getattr(doc, "paragraphs", []) or []
    intro_i = next((i for i, p in enumerate(paras)
                    if p.is_heading and re.match(
                        r"^\d+(\.\d+)*\.?\s*[Ii]ntroduction\b",
                        (p.text_en or "").strip())), None)
    if intro_i is None:
        return
    p_i = intro_i + 1
    while p_i < len(paras) and (paras[p_i].is_heading or paras[p_i].is_caption):
        p_i += 1
    if p_i + 1 >= len(paras):
        return
    first = paras[p_i]
    if len(re.findall(r"[A-Za-z]+", first.text_en or "")) >= threshold:
        return
    j = p_i + 1
    while j < len(paras) and (paras[j].is_heading or paras[j].is_caption):
        j += 1
    if j >= len(paras):
        return
    nxt = paras[j]
    first.text_en = ((first.text_en or "").strip() + " "
                     + (nxt.text_en or "").strip()).strip()
    if first.text_zh and nxt.text_zh:
        first.text_zh = ((first.text_zh or "").strip() + " "
                         + (nxt.text_zh or "").strip()).strip()
    paras.remove(nxt)


def _md_of_local(skeleton, line_map) -> dict:
    """[局部] md 段 idx → 本地段列表（行区间重叠；修复段配对用）"""
    local_bodies = [p for p in getattr(skeleton, "paragraphs", []) or []
                    if getattr(p, "kind", "") == "body"]

    def _num(lid: str) -> int:
        try:
            return int(re.sub(r"\D", "", lid))
        except (ValueError, IndexError):
            return 0

    out: dict[int, list] = {}
    for mid, rng in (line_map or {}).items():
        s, e = _num(rng[0]), _num(rng[1])
        for lp in local_bodies:
            ls = _num(lp.start_line.line_id)
            le = _num(lp.end_line.line_id)
            if s <= le and ls <= e:
                out.setdefault(mid, []).append(lp)
    return out


def _is_paddle_noise(b) -> bool:
    """[局部] 百度块噪声判定：kind 噪声（页眉/页脚/页码/参考文献——用户：参考文献不
    拼接）+ 元数据文本模式（ORCID/Received/Revised/Accepted/Published online/Supporting
    Information/How to cite/DOI/页码 "N of M"）。"other" 不整体排除（含作者行 frontmatter
    合法内容），按文本模式判。"""
    if getattr(b, "kind", "") in ("header", "footer", "page_number", "page-header",
                                  "page-footer", "footnote", "page-number", "reference"):
        return True
    t = (getattr(b, "text", "") or "").lower()
    if re.search(r"\b\d+\s+of\s+\d+\b", t):
        return True
    return any(k in t for k in ("orcid", "received:", "revised:", "accepted:",
                                "published online", "supporting information is available",
                                "how to cite", "doi:"))


def _head_anchor_ok(btext: str, pid_tokens: list, *,
                    head_n: int = 6, window: int = 18, min_match: int = 2) -> bool:
    """[局部] Q5 防线1：块首有序锚校验——块前 head_n 词需与**段首 window 词**有
    **连续 min_match 词以上的有序匹配**（SequenceMatcher 匹配块）。

    防集合覆盖度误配：长段落 + 高频领域词（immersion/IPS/Fig…）不同段落块也
    ≥0.5 集合覆盖（实证 cej RP017："In terms..." 块被误配到 "In addition..." 段
    → 拼接文本 token 集合吞掉 mineru 全部词 → overlap_set=1.0 失真 → AI 仲裁
    浪费 token）。连续匹配要求：无关块只有 of/the 等零散高频词（无连续 ≥2 词）
    → 拒绝；OCR 错拼 1 词不影响（其余连续匹配块仍 ≥2）。
    短块（<3 词，标题/图注碎片）不校验（无足够锚）。"""
    from difflib import SequenceMatcher
    bhead = norm_text(btext).split()[:head_n]
    if len(bhead) < 3:
        return True
    phead = pid_tokens[:window]
    sm = SequenceMatcher(None, bhead, phead)
    total = sum(b.size for b in sm.get_matching_blocks() if b.size >= min_match)
    return total >= min_match


def _content_pair_paddle(repair_items, pblocks, *,
                         min_cov: float = 0.5,
                         md_of_local: dict | None = None) -> tuple[dict, dict]:
    """[局部] ★内容锚定配对（P16：**按 kind 分池 + 噪声过滤**）：
    百度段 → 已拼接 mineru 段。

    用户 2026-08-25 三点修正：
    - **按 kind 分池**：body 块→body 段、caption 块→caption 段、heading 块→heading 段，
      图注/标题单独拼接，正文拼接排除标题/题注干扰（实证 RP101 图注被 "2.1." 标题污染）。
    - **噪声过滤**：参考文献块/页码/ORCID/Received/Supporting Information 等不参与拼接
      （实证 ORCID kind=other 混入、Received 被误标 body）。
    - 正文段 token 集合覆盖度全局最优（容 OCR 拼写/公式差异）+ 段头覆盖校验 +
      坐标 soft hybrid（歧义时圈内确认，不排除）。
    返回 ({para_id: paddle_text}, {para_id: page})。
    """
    paras = [r for r in repair_items
             if getattr(r, "kind", "") in ("body", "caption", "heading")]
    kind_by_pid = {r.para_id: r.kind for r in paras}
    np_tokens: list[tuple[str, list]] = [
        (r.para_id, norm_text(r.text).split()) for r in paras]
    tokens_by_pid: dict[str, list] = dict(np_tokens)
    pset_by_pid: dict[str, set] = {
        pid: set(toks) for pid, toks in np_tokens}
    segs = [b for b in (getattr(pblocks, "blocks", None) or [])
            if not _is_paddle_noise(b)]
    # P16 hybrid：每段预收集本地骨架行坐标（页, y0, y1）
    para_coords: dict[str, set] = {}
    if md_of_local is not None:
        for r in paras:
            coords: set = set()
            for mid in (r.md_idx or []):
                for lp in md_of_local.get(mid, []):
                    for ln in (getattr(lp, "lines", []) or []):
                        pg = getattr(ln, "page", 0)
                        bb = getattr(ln, "bbox", None)
                        if not bb:
                            continue
                        coords.add((pg, bb[1], bb[3]))
            if coords:
                para_coords[r.para_id] = coords
    assigned: dict[str, list] = {}
    page_of: dict[str, int] = {}
    for b in segs:
        btext = b.text or ""
        words = re.findall(r"[a-zA-Z]+", btext)
        if len(words) < 3:                     # 过滤无实质词块（页码/数字/碎片）
            continue
        bn = norm_text(btext).split()
        if len(bn) < 3:
            continue
        bset = set(bn)
        need = min_cov + (0.3 if len(bset) < 6 else 0.0)   # 短块需更高覆盖
        bkind = getattr(b, "kind", "body") or "body"
        # 按 kind 圈定候选：body块→body段 / caption块→caption段 / heading块→heading段
        cand = [pid for pid in pset_by_pid
                if kind_by_pid.get(pid) == bkind and pset_by_pid[pid]]
        if not cand:
            continue
        scores = [(pid, len(bset & pset_by_pid[pid]) / len(bset))
                  for pid in cand]
        if not scores:
            continue
        scores.sort(key=lambda x: -x[1])
        best_pid, best_d = scores[0]
        # 坐标圈内确认（soft hybrid）：仅当 top 歧义时，用坐标池优先 y 重叠段
        if para_coords and len(scores) > 1 and scores[1][1] >= best_d * 0.85:
            _bb = getattr(b, "bbox", None)
            if _bb:
                by0, by1 = _bb[1], _bb[3]
                in_pool = {pid for pid, _ in scores
                           if any(pg == b.page
                                  and not (by1 < cy0 - 2 or by0 > cy1 + 2)
                                  for (pg, cy0, cy1) in para_coords.get(pid, ()))}
                cand_in_pool = [(pid, d) for pid, d in scores if pid in in_pool]
                if cand_in_pool and cand_in_pool[0][1] >= need:
                    best_pid, best_d = cand_in_pool[0]
        # Q5 防线1：块首有序锚——best 段内无块首序列（集合覆盖误配）→ 尝试次优段
        # （锚匹配 + 覆盖够）；无锚匹配段 → 丢弃该块（宁缺毋滥）
        if not _head_anchor_ok(btext, tokens_by_pid.get(best_pid, [])):
            picked = None
            for pid2, d2 in scores[1:]:
                if d2 < need:
                    break
                if _head_anchor_ok(btext, tokens_by_pid.get(pid2, [])):
                    picked = (pid2, d2)
                    break
            if picked is None:
                continue
            best_pid, best_d = picked
        if best_d < need:
            continue                      # 未命中 → 宁缺毋滥
        assigned.setdefault(best_pid, []).append(b.text)
        page_of.setdefault(best_pid, b.page)
    # 段头覆盖校验：段头 10 词缺失 → 补匹配块（pool 限定同 kind，防跨类污染）
    for pid, ptoks in np_tokens:
        if pid not in assigned:
            continue
        head_set = set(ptoks[:10])
        if not head_set:
            continue
        cur_set = set(norm_text(" ".join(assigned[pid])).split())
        if len(head_set & cur_set) / len(head_set) >= 0.5:
            continue
        best_b, best_d = None, 0.0
        for b in segs:
            if kind_by_pid.get(pid) != (getattr(b, "kind", "body") or "body"):
                continue
            bset = set(norm_text(b.text or "").split())
            if not bset:
                continue
            d = len(head_set & bset) / len(head_set)
            if d > best_d:
                best_d, best_b = d, b
        if best_b is not None and best_d >= 0.5:
            assigned[pid].insert(0, best_b.text)
    return ({pid: " ".join(t) for pid, t in assigned.items()}, page_of)


def _in_word_env(text: str, i1: int, i2: int) -> bool:
    """[局部] 片段**左侧**是否词环境（左邻是字母/连字符/撇号）——OCR 断词
    修复都是词内/词尾缺字母（Efici|ent、ab|x、cost-|efective），左侧必是原词
    一部分；词前孤立字母（" |good" 前插 'f'）是 OCR 幻觉特征 → 不算。
    注：只看左侧不看右侧——"This is f good" 的 'f' 右邻是字母 'g'，若看右
    侧会误放行（2026-08-25 边界用例实证）。"""
    _l = text[i1 - 1] if i1 > 0 else ""
    return bool(_l and (_l.isalpha() or _l in "-'"))


def _gap_in_word(text: str, i1: int, i2: int) -> bool:
    """[局部] 空白片段**两侧**是否词字符（词内空格粘连——"calo rimetry" 的
    空格左邻 'o' 右邻 'r' 都是字母 → 粘连候选，送仲裁）"""
    _l = text[i1 - 1] if i1 > 0 else ""
    _r = text[i2] if i2 < len(text) else ""
    return bool(_l and _r and _l.isalpha() and _r.isalpha())


def _mask_formulas_keep_len(text: str, ch: str = "\u0001") -> str:
    """[局部] 公式块（$...$/$$...$$）→ 等长 ch 占位（P15：difflib 会把
    "$1 0 0 ~ ^ { \\circ } \\mathrm { C }$" 拆成单字符 opcode 无法聚合——
    mask 后公式是整体单元，不再拆碎；等长保证索引与原文一致，取片段仍用
    原文（m_raw=mineru_text[i1:i2]））。
    **2026-08-26 修复（R5）**：双通道用不同占位符（mineru \\u0001 / paddle
    \\u0002）——同字符时 difflib 会把 mineru 公式尾与 paddle 公式 mask 判
    equal → 公式被拦腰切 → 仲裁半截替换（实证 0.98°s 残留
    "$\\mathrm{0.98°s}$ { - 1 }$(bareLIG),to...，SHORT-TERM 误记为源损坏）。"""
    return re.sub(r"\$\$[\s\S]+?\$\$|\$[^$\n]+?\$",
                  lambda m: ch * len(m.group(0)), text)


def char_conflicts(mineru_text: str, paddle_text: str, page: int = 0) -> list[dict]:
    """[局部] 段落级字符 diff → text_conflict 项（difflib opcodes）

    过滤：纯空白差异；**无字母的纯符号碎片**（", "/"; " 等标点噪声，
    如 "100 C" vs "100 °C" 的 "C"→"°C" 含字母须保留——真实字符差异）；
    **大 chunk（任一侧 > 40 字符）** = 段边界偏移类（paddle 块边界与 md
    段不同步），非字符错误，不送仲裁（防 P 替换破坏语义）。
    P15：公式块 mask 为整体单元（防 difflib 拆碎公式碎片）；公式内空格
    因 mask 不再产生 opcode（排除 LaTeX 语法空格误判）。
    返回 [{type, page, mineru:{text}, paddleocr:{text}, evidence:{tag}}]
    """
    if not mineru_text or not paddle_text:
        return []
    out: list[dict] = []
    # 公式块 mask（等长占位，索引不变）——difflib 在 mask 文本上跑；
    # R5：双通道不同占位符（\u0001 vs \u0002）防跨通道 equal 拦腰切公式
    m_mask = _mask_formulas_keep_len(mineru_text, "\u0001")
    p_mask = _mask_formulas_keep_len(paddle_text, "\u0002")
    sm = difflib.SequenceMatcher(None, m_mask, p_mask)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        # 片段从**原文**取（mask 等长 → 索引一致；含真实公式文本）
        m_raw = mineru_text[i1:i2]
        p_raw = paddle_text[j1:j2]
        m_core = m_raw.strip()
        p_core = p_raw.strip()
        if not m_core and not p_core:
            # **词内空格粘连**（P15 用户反馈：mineru "calo rimetry" paddle
            # "calorimetry"——空格被 strip 成空片段，原"纯空白差异→格式类"
            # 误滤）→ 一侧是纯空格、另一侧空，且空格两侧是词字符（词内
            # 空格）→ 真实差异，送仲裁（AI 判哪侧正确，落地删/插空格）。
            if (m_raw and m_raw.isspace() and not p_raw
                    and _gap_in_word(mineru_text, i1, i2)) or \
               (p_raw and p_raw.isspace() and not m_raw
                    and _gap_in_word(paddle_text, j1, j2)):
                out.append({
                    "type": "text_conflict", "page": page,
                    "mineru": {"text": m_raw},
                    "paddleocr": {"text": p_raw},
                    "evidence": {"tag": tag, "i1": i1, "i2": i2,
                                 "j1": j1, "j2": j2,
                                 "m_ctx": mineru_text[max(0, i1 - 60):i2 + 60],
                                 "p_ctx": paddle_text[max(0, j1 - 60):j2 + 60]}})
            continue                       # 其余纯空白差异 → 格式类
        if len(m_core) > 40 or len(p_core) > 40:
            continue                       # 段边界偏移类大块 → 跳过
        has_letter = bool(re.search(r"[A-Za-z]", m_core + p_core))
        if not has_letter and len(m_core) < _MIN_CHUNK \
                and len(p_core) < _MIN_CHUNK:
            continue                       # 无字母纯符号碎片 → 忽略
        # mineru 侧**纯格式标记**（HTML 标签/孤立 LaTeX 命令，无实质内容）→
        # 非识别误差，跳过；**$ 只剥符号不剥公式块**（P15：公式碎片
        # "$1 0 0 ~ ^ { \circ } \mathrm { C }$" 有内容 "100 °C"，\$[^$]*\$ 整
        # 块剥会误判"纯标记"→ 校准丢失）。
        # 仅当 m_core 非空才检查（insert 形态 m_core="" 是真实插入差异）。
        if m_core:
            m_plain = re.sub(r"\$|<[^>]+>|\\[A-Za-z]+|[^A-Za-z\s]",
                             "", m_core)
            if not m_plain.strip():
                continue
        # P15：**任一侧含 LaTeX/HTML 标记**的差异——剥标记归一化后等价
        # （作者上标 "<sup>a,1</sup>" vs "$ ^{a,1} $"、<sup> vs $^{}$ 类纯
        # 格式差异）→ 跳过省 token（双侧含标记也判——此前只判单侧，<sup>
        # vs $^{}$ 漏过进仲裁浪费）；**不等价或碎片形态**（mineru
        # "$1 0 0 ~ ^ { \circ } \mathrm { C }$" vs "100 °C"）→ 送仲裁（AI
        # 判识别劣化）。
        if re.search(r"[\\$<>{}]", m_core) or re.search(r"[\\$<>{}]", p_core):
            _eq = bool(_norm_format(m_core) == _norm_format(p_core)
                       and _norm_format(m_core))
            # 碎片判据（P15 修正）：$...$ 去 LaTeX 命令/括号/^_~ 后，**含 ≥2
            # 个被空格拆散的单字符 token**（"1 0 0 C"）才是碎片——正常上标
            # "$ ^{a,1} $" 去命令后是单 token "a,1"，不误判
            _frag = False
            for _x in (m_core, p_core):
                _mm = re.search(r"\$([^$]*)\$", _x)
                if _mm:
                    _inner = re.sub(r"\\[A-Za-z]+|[{}\^_~]", "", _mm.group(1))
                    if sum(1 for _t in _inner.split() if len(_t) == 1) >= 2:
                        _frag = True
                        break
            # R5 修正（2026-08-26）：**内容等价直接跳过**（不再要求 not _frag）——
            # mask 通道隔离（\u0001/\u0002）后公式 vs 公式会产生 opcode，而碎片判据
            # 会把化学式碎片 "$\mathrm { B F } _ { 4 }$"（去命令后 "B F 4" 三单字符）
            # 误判为碎片 → 相同公式也报冲突。_eq=True 时两侧内容一致，跳过无损失；
            # 碎片判据只在 _eq=False（公式 vs 明文）路径有意义（送仲裁）。
            if _eq:
                continue
        # **真实词级差异（断词/拼写）判据**（2026-08-25 修订，修 2 测试失败）：
        # - **单字母 insert/delete 保留**（m_core 或 p_core 单字母、另一侧空）
        #   ——temperature→temprature 的 'e'（difflib 常拆成 replace "er"→"r"，
        #   见下）、ab→abx 的 'x'；词环境放宽为"左邻或右邻是字母/连字符/撇号"
        #   （词中插入 Efici|ent 与词尾插入 ab|x 都算）；句子边界孤立碎片
        #   （''→'f' 前后皆非字母）→ 跳过（送仲裁只会 unresolved，实证
        #   107 项 → 100 unresolved）
        # - **replace 形态：至少一侧 ≥2 字母**（"er"→"r" 保留；两侧都单字母
        #   如 "a"→"b" 保守跳过——真实差异概率低且仲裁收益小）
        if not m_core and p_core:
            if not (len(p_core) <= 2 and re.fullmatch(r"[A-Za-z]", p_core)
                    and _in_word_env(mineru_text, i1, i2)):
                continue
        elif m_core and not p_core:
            if not (len(m_core) <= 2 and re.fullmatch(r"[A-Za-z]", m_core)
                    and _in_word_env(paddle_text, j1, j2)):
                continue
        elif not (re.fullmatch(r"[A-Za-z]{2,}", m_core)
                  or re.fullmatch(r"[A-Za-z]{2,}", p_core)
                  # P15：公式碎片/数字混合（mineru 把 "100 °C" 公式化成
                  # "$1 0 0 ~ ^ { \circ } \mathrm { C }$"——非纯字母词对，
                  # 原判据误滤）→ 含标记字符或字母+数字混合 → 送仲裁（AI
                  # 判 whether 表示差异(both)或识别错误(paddleocr)）
                  or re.search(r"[\\$<>{}]", m_core + p_core)
                  or (re.search(r"[A-Za-z]", m_core + p_core)
                      and re.search(r"\d", m_core + p_core))):
            continue
        out.append({
            "type": "text_conflict", "page": page,
            "mineru": {"text": mineru_text[i1:i2]},
            "paddleocr": {"text": paddle_text[j1:j2]},
            "evidence": {"tag": tag, "i1": i1, "i2": i2,
                         "j1": j1, "j2": j2,
                         # 前后 15 字符上下文：insert 碎片（''→'f'）AI 无法
                         # 定位（实证判 mineru"P多余识别出f"）——带上下文
                         # "Efici[f]ent" 才能判 paddleocr（Efficient 正确）
                         "m_ctx": mineru_text[max(0, i1 - 60):i2 + 60],
                         "p_ctx": paddle_text[max(0, j1 - 60):j2 + 60]}})
    return out


def _strip_math(text: str) -> str:
    """[局部] 去 LaTeX/数学标记与标点 → 纯内容串（比较公式/单位内容是否等价）。
    2026-08-26 增强：Unicode 数学符/单位归一（°→circ、⁻→-、上下标→数字/符号），
    使 mineru LaTeX（$100^{\\circ}\\mathrm{C}$）与百度明文（100°C）等价判定
    真正工作——否则单位/公式差异全进复核。"""
    t = re.sub(r"\$", "", text or "")
    t = re.sub(r"\\circ\b", "circ", t)          # 度（\circ → circ 保留语义，再剥命令）
    t = re.sub(r"\\[a-zA-Z]+", "", t)           # \mathrm \cdot 等
    t = re.sub(r"\\[,;:! ]", "", t)             # 薄空格命令（\, \; \: \!）2026-08-26
    t = re.sub(r"[{}\[\]()@]", "", t)           # 括号/花括号/@
    t = re.sub(r"[\s_^~]+", "", t)              # 空白/下划线/脱字符
    # Unicode 数学符/单位归一（百度明文形态 → LaTeX 语义等价）
    for u, d in (("°", "circ"), ("⁻", "-"), ("⁺", "+"), ("−", "-"), ("μ", "mu"),
                 ("Ω", "Omega"), ("Δ", "Delta"), ("×", "x"), ("·", "*"),
                 ("≈", "~"), ("–", "-"), ("—", "-"), ("±", "+-")):
        t = t.replace(u, d)
    for src, dst in zip("₀₁₂₃₄₅₆₇₈₉", "0123456789"):
        t = t.replace(src, dst)
    for src, dst in zip("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789"):
        t = t.replace(src, dst)
    t = re.sub(r"[-·.·]", "", t)                # 连字符/点
    return t.lower()


def _is_formula_conflict(conflict: dict) -> bool:
    """[局部] 该差异是否公式/上下标/单位类（mineru 或 paddle 含数学标记）"""
    m = (conflict.get("mineru") or {}).get("text", "")
    p = (conflict.get("paddleocr") or {}).get("text", "")
    return bool(re.search(r"[$\\_^]", m + p))


def _formula_equivalent(conflict: dict) -> bool:
    """[局部] mineru(LaTeX) 与 paddle(明文) 公式**内容等价**（如
    $\\mathrm{Co(O_x/P_x)@P\\cdot LIG\\cdot P.P}$ vs Co(O_x/P_x)@P-LIG-P.P）→
    百度确认内容对、mineru LaTeX 保留即可（不替换成明文，保渲染）。
    2026-08-26 收紧子串判定：一侧含另一侧且**多出部分含数字** → 不等价
    （百度漏识别下标/数字，实证 BF₄⁻ 的 4 被漏 → "bf" in "bf4" 误判等价）。"""
    m = (conflict.get("mineru") or {}).get("text", "")
    p = (conflict.get("paddleocr") or {}).get("text", "")
    pm, pp = _strip_math(m), _strip_math(p)
    if len(pm) < 2 or len(pp) < 2:
        return False
    if pm == pp:
        return True

    def _subset(short: str, long: str) -> bool:
        if short not in long:
            return False
        extra = long.replace(short, "", 1)      # 多出部分
        return not re.search(r"\d", extra)      # 多出数字 = 内容缺失，不等价

    if len(pp) > len(pm):
        return _subset(pm, pp)                  # 百度更长（补全）→ 无数字增量等价
    if len(pm) > len(pp):
        return _subset(pp, pm)                  # mineru 更长（百度漏）→ 无数字缺失等价
    return False


def _is_paddle_authoritative(conflict: dict) -> bool:
    """[局部] P16：是否"百度OCR为准"类错误。**2026-08-26 缩窄范围**——百度只对
    **纯文本词级小噪声**（拼写/断词/标点/空格）权威：
    - 公式/LaTeX/HTML sup/上下标/单位/数学符号：**不自动百度替换**——走
      _formula_equivalent（内容等价→both 保留 mineru LaTeX；不等价→unresolved
      进 GUI 复核）。根因：百度 OCR 会漏识别（实证 BF₄⁻ 的 4 被漏 → 替换丢下标），
      "百度含公式/单位也准"一刀切放大百度错误。
    - 数字差异（任一侧含数字另一侧无）：丢字/增字风险 → 不自动替换。
    - 空格丢失/粘连（纯空白差异）→ 百度准（两侧无实质内容差）。
    命中 → 直接以百度文本替换，不送 AI；类外歧义项 → unresolved（GUI 复核）。"""
    m = (conflict.get("mineru") or {}).get("text", "")
    p = (conflict.get("paddleocr") or {}).get("text", "")
    if not m and not p:
        return False
    # 守卫：百度把**词识别成纯数字/符号**（如 "China ("→"15) 240"）→ 百度垃圾，
    # 不可信，进 GUI 复核。只有百度含字母（词/公式/上下标）才准。
    _m_l = bool(re.search(r"[A-Za-z]", m))
    _p_l = bool(re.search(r"[A-Za-z]", p))
    if _m_l and not _p_l:
        return False
    # 守卫：百度替换长度差异过大（>1.8x / <0.5x）→ 结构性错读（如 "JMDZ"→"6 of 15) "），
    # 非小修正确认，进 GUI 复核。**公式类豁免**（LaTeX 冗长 vs 明文，长度比天然小）。
    if m and p and not re.search(r"[$\\_^]", m + p):
        _ratio = len(p) / len(m)
        if _ratio > 1.8 or _ratio < 0.5:
            return False
    if m.isspace() or p.isspace():                 # 空格丢失/粘连 → 百度准
        return True
    # 2026-08-26 缩窄：公式/LaTeX/HTML sup/上下标/数学符号 → **不自动百度
    # 替换**（百度 OCR 漏识别风险，如 BF₄⁻ 的 4；走 _formula_equivalent/复核）。
    # °（度）移出：纯文本单位补全（100 C → 100 °C，mineru 常漏 °）→ 百度准。
    _MATH_SIG = re.compile(
        r"[$\\<]|[⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺ΩμℓÅ₀₁₂₃₄₅₆₇₈₉\u2212\u2248\u2260\u2264\u2265\u00d7\u00f7\u00b1]")
    if _MATH_SIG.search(m + p):
        return False
    # 数字差异守卫：任一侧含数字另一侧无（丢字/增字，如 BF₄⁻→BF⁻ 的 4 丢失）
    # → 不自动替换（进复核）。两侧都有数字或都无数字才放行。
    _m_d = bool(re.search(r"\d", m))
    _p_d = bool(re.search(r"\d", p))
    if _m_d != _p_d:
        return False
    # 纯文本词级小差异（字母/标点/空格/连字符，无公式/数字异常）→ 百度准
    if re.fullmatch(r"[A-Za-z0-9.\-',;:!?()&/%=\s\u00b0]+", m + p):
        return True
    return False


def _apply_arbitrations(para_text: str, conflicts: list[dict],
                        arbitrations: list,
                        apply_min_conf: float = 0.8) -> tuple[str, list[dict]]:
    """[局部] 仲裁应用：verdict=P & conf≥apply_min_conf → 用 paddle 片段替换
    mineru 片段（唯一出现才替换）。返回 (最终文本, audit 清单)

    P15：conf<apply_min_conf（默认 0.8，2026-08-26 用户授权 0.9→0.85→0.8）的
    verdict=P **不自动落地** → 进 GUI 复核清单（用户拍板：<conf 阈值项进复核）。
    """
    audit: list[dict] = []
    if not conflicts:
        return para_text, audit
    arb_by_id = {a.id: a for a in (arbitrations or [])}
    final = para_text
    # **从后往前应用**：insert 会改变后续字符索引（Eficient→Efficient 是
    # insert 'f'，mineru 侧空片段），reversed 保证前面冲突的索引不漂移
    for i, c in reversed(list(enumerate(conflicts))):
        a = arb_by_id.get(i)
        if not a or a.verdict != "paddleocr":
            continue                 # mineru/both/neither/unresolved → 保留 mineru
        if a.confidence < apply_min_conf:
            continue                 # P15：低置信不自动落地（进 GUI 复核）
        m_text = (c.get("mineru") or {}).get("text", "")
        p_text = (c.get("paddleocr") or {}).get("text", "")
        if not m_text and p_text:
            # insert 形态：mineru 缺字符（断词修复），在 evidence.i1 处插入
            _i1 = (c.get("evidence") or {}).get("i1")
            if _i1 is None or _i1 > len(final):
                continue
            # P16 公式采纳：百度明文 → 包回 $...$ 保渲染（根治"公式段缺 $"）
            if _is_formula_conflict(c):
                p_text = _wrap_paddle_formulas(p_text)
            final = final[:_i1] + p_text + final[_i1:]
            audit.append({"action": "arbitrate_insert", "verdict": "P",
                          "at": _i1, "insert": p_text[:30],
                          "reason": (a.reason or "")[:60]})
            continue
        # P15：**纯空白项**（空格粘连——"calo rimetry"→"calorimetry" 删/插
        # 空格）——片段 " " 在段落中多次出现，count 匹配必 ambiguous → 按
        # evidence 位置落地（**位置校验**：该处确为空白才操作，防 reversed
        # 遍历下前面项的索引漂移；漂移则跳过宁缺毋滥）
        if m_text.isspace() and not p_text:
            _i1, _i2 = ((c.get("evidence") or {}).get("i1"),
                        (c.get("evidence") or {}).get("i2"))
            if _i1 is not None and _i2 is not None \
                    and _i2 <= len(final) and final[_i1:_i2] == m_text:
                final = final[:_i1] + final[_i2:]
                audit.append({"action": "arbitrate_delete_ws", "verdict": "P",
                              "at": _i1, "reason": (a.reason or "")[:60]})
            continue
        if p_text.isspace() and not m_text:
            _i1 = (c.get("evidence") or {}).get("i1")
            if _i1 is not None and _i1 <= len(final):
                final = final[:_i1] + p_text + final[_i1:]
                audit.append({"action": "arbitrate_insert_ws", "verdict": "P",
                              "at": _i1, "reason": (a.reason or "")[:60]})
            continue
        if not m_text or m_text not in final:
            continue
        n = final.count(m_text)
        if n != 1:
            audit.append({"action": "skip_p_ambiguous", "chunk": m_text[:40],
                          "count": n, "reason": "片段在段落中多次出现，不自动替换"})
            continue
        # P16 公式采纳：百度明文 → 包回 $...$ 保渲染（根治"公式段缺 $"）
        if _is_formula_conflict(c):
            p_text = _wrap_paddle_formulas(p_text)
        final = final.replace(m_text, p_text)
        audit.append({"action": "arbitrate_replace", "verdict": "P",
                      "before": m_text[:60], "after": p_text[:60],
                      "reason": (a.reason or "")[:60]})
    return final, audit


def build_markdown(repair_items: list, figures: list | None = None,
                   include_references: bool = True) -> str:
    """[局部] 修复段落流 → markdown（标题/正文/图注原样；图片标记替换本地图）

    细节：
    - image 段只输出**图片标记行**（md 中 "![](...)\nFig. 1. caption" 同块的
      caption 已由 parse_md_paragraphs 拆成独立 caption 段，若整块输出会重复）；
    - 图标记路径替换用该 image 段**之后最近的 caption 编号**（md 结构：图片
      标记紧跟其图注；"上一个 caption"会错位——Fig.3 前标记会挂到 Fig.2 图）；
    - **未替换为本地图的 image 段（mineru/OCR 残留小图引用垃圾）→ 删除**；
    - **References 区 → 表格**（编号 | 文献），供导出与 AI 引用查询；
    - 2026-08-26：include_references=False（用户决策：md 文件不保留参考文献表，
      准确文献信息后续从 bib 文件导入；MinerU 完整版 mineru_full.md 含参考文献
      留 library 备份）→ References 区整体跳过（正文引用 [n] 保留）。
    """
    fig_by_num: dict[int, str] = {}
    for f in figures or []:
        m = re.match(r"^(?:fig(?:ure)?|table|scheme)\.?\s*(\d+)",
                     (f.caption or "").strip(), re.I)
        if m:
            fig_by_num[int(m.group(1))] = f.file

    # P16：图放置改为"图注前插本地大图"——丢弃散落的 image 段（含摘要区误插、
    # 多子图标记），按图注编号在每张图注前插入对应本地大图（图:图注严格对齐）。
    out: list[str] = []
    in_refs = False
    ref_rows: list[tuple[int, str]] = []
    for i, r in enumerate(repair_items):
        text = (r.text or "").strip()
        if not text:
            continue
        # References 区：heading 进入；"[N]" 引用段收集为表格行
        # （P16：adma 源无 "References" 标题 → 直接靠 "[N] 条目" 连续识别兜底）
        if r.kind == "heading" and re.search(r"^##?\s*references$",
                                             text, re.I):
            in_refs = True
            if include_references:
                out.append(text)
            continue
        if in_refs or re.match(r"^(?:<sup>)?\[\d+\](?:</sup>)?\s+\S", text):
            # 2026-08-26：参考文献条目可能带上标（<sup>[41]</sup> Y. ...）——
            # 原 ^\[\d+\] 匹配失败 → 参考文献泄漏进 en.md（用户反馈"没删除"）；
            # 闭合标签 </sup> 也要兼容（<sup>[1]</sup> 形态）
            m_ref = re.match(r"^(?:<sup>)?\[(\d+)\](?:</sup>)?\s+\S", text)
            if m_ref:
                in_refs = True
                ref_rows.append((int(m_ref.group(1)), text))
                continue
            # 非引用段（References 之后杂项）→ 先冲刷表格
            if ref_rows:
                if include_references:
                    out.append(_ref_table(ref_rows))
                ref_rows = []
            in_refs = False
            if not include_references:
                # References 区整体跳过（含 References 之后的杂项：Supplementary/
                # Data availability 等由后续段落流正常处理——继续往下走）
                pass
        if r.kind == "image":
            continue                      # 散落图标记丢弃（图由图注前插入）
        # 图注：前插对应本地大图，再输出图注文本
        if r.kind == "caption":
            _mcap = re.match(r"^(?:fig(?:ure)?|table|scheme)\.?\s*(\d+)",
                             text, re.I)
            if _mcap and int(_mcap.group(1)) in fig_by_num:
                out.append("![](%s)" % fig_by_num[int(_mcap.group(1))])
            out.append(text)
            continue
        # 剥离段内残留图片标记（"d ![](...)" 类 body 段 OCR 残留），
        # 剥离后为空 → 整段删除
        text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text).strip()
        if not text:
            continue
        # P16：图面板标签清洗（Wiley adma 大量 (a)(b)(c)4… 被 mineru 当正文）。
        # - 段首孤立标签行（"(h)\n![]()\nFigure 4. …"）剥标签行、保留图注
        # - body 段内/整段的孤立 "(a)" "(b)" 标签（前后不邻字母数字）→ 全剥；
        #   纯标签段（"(a)\n(b)"）剥后清空 → 整段删除
        # - 图注段（caption）**不调用**：子图编号（"Figure 2. … a) SEM"）合法保留
        if r.kind == "body":
            text = _strip_panel_labels(text)
        if not text:
            continue
        # P16：孤立短字符段落（"d"/"b."/"1" 单独成段）→ 删除（无合法单字符段落）
        if r.kind == "body" and _is_isolated_short_char(text):
            continue
        # 2026-08-26：正文引用编号 [n]/[n,m]/[n–m] → <sup>[n]</sup>（mineru 部分漏
        # 转上标；已上标的保护不动；参考文献区由 in_refs 分流不经过这里）
        if r.kind == "body":
            text = _sup_inline_refs(text)
        # P16：公式碎片归一化（mineru 把 "$0 . 3 7$" 空格拆散 → "$0.37$"）
        if r.kind == "body":
            text = normalize_formula_fragments(text)
        # P16：孤儿 LaTeX 补 $ 定界（mineru 可能漏 $，如 "\mathrm{CoO_x}@LIG"）
        if r.kind in ("body", "caption"):
            text = wrap_orphan_latex(text)
            # P16：公式命令正确性校验（\Nu_{2}→\mathrm{N}_{2}，防错式保留）
            text = _fix_formula_commands(text)
        out.append(text)
    if ref_rows and include_references:
        out.append(_ref_table(ref_rows))
    md = "\n\n".join(out) + "\n"
    if not include_references:
        # 2026-08-26 用户建议：输出前保险扫描尾部参考文献特征（兜底 [N] 识别漏网）
        md = _strip_trailing_references(md)
    return md


_REF_LINE_RES = (
    re.compile(r"^\|?\s*\[\d+\]\s*\|?\s+\S"),        # 表格行 | [1] | Y. ...
    re.compile(r"^(?:<sup>)?\[\d+\](?:</sup>)?\s+[A-Z]"),  # 上标/裸 [N] 条目
)


def _strip_trailing_references(md: str) -> str:
    """[局部] 输出前保险：删除**尾部参考文献区**（用户建议：References 标题下
    直接删 + 尾部特征扫描兜底）。识别：
    1) `References` 标题行（## References / References）→ 从标题删到尾部；
    2) 否则从尾部找**连续 ≥2 行**参考文献特征（表格行 / 段首 [N] 条目）的起点
       → 删到尾部（参考文献区恒在文末；正文内联引用 <sup>[N]</sup> 在行内不匹配）。
    返回清洗后文本（无命中则原样）。"""
    lines = md.splitlines()
    # 1) References 标题（含变体：References / REFERENCES / 带 #）
    for i, ln in enumerate(lines):
        if re.match(r"^#{0,3}\s*references\s*$", ln, re.I):
            return "\n".join(lines[:i]).rstrip() + "\n"
    # 2) 尾部参考文献特征区（空行跳过——条目间有空行分隔；非空 ref 行 ≥2 才删）
    def _is_ref(ln: str) -> bool:
        return any(r.match(ln) for r in _REF_LINE_RES)
    region_end = len(lines)
    nonempty_refs = 0
    while region_end > 0:
        ln = lines[region_end - 1]
        if not ln.strip():
            region_end -= 1
            continue
        if _is_ref(ln):
            region_end -= 1
            nonempty_refs += 1
            continue
        break
    if nonempty_refs >= 2:                      # 连续 ≥2 条参考文献才判定为参考文献区
        return "\n".join(lines[:region_end]).rstrip() + "\n"
    return md


def _ref_table(ref_rows: list) -> str:
    """[局部] 引用行 → Obsidian 兼容表格块（**内部无空行**——空行会打断
    表格语法；整体作为单个段落由 join 与其他块分隔）"""
    rows = ["| 编号 | 参考文献 |", "|---|---|"]
    for _n, _t in sorted(ref_rows):
        _t_body = re.sub(r"^\[\d+\]\s*", "", _t)
        rows.append("| [%d] | %s |" % (_n, _t_body.replace("|", "\\|")))
    return "\n".join(rows)


def _wrap_paddle_formulas(text: str) -> str:
    r"""[局部] 把 paddle 明文里的公式/上下标/单位子串包成 $\mathrm{...}$ 让 KaTeX 渲染
    （P16：不再仅 review 展示——采纳进最终文本的百度公式也用它落地，根治"公式段缺 $"）。
    保护已配对 $...$ 与普通英文词；数学信号（_/^/@/{}/\ 命令/Unicode 数学符/上下标/
    单位）命中即包。如 Co(O_x/P_x)@P-LIG-P.P、9.80 Am^2 kg^-1、to1.92°s-1、ΔV=Φ−φ。
    对残留 "$\mathrm{s^}${-1}" 类破损（旧正则把 ^ 后的 { 抛到 $ 外）用含 { } 的数学
    信号串整体包裹修复。"""
    if not text:
        return text
    prot: list[str] = []

    def _save(m):
        prot.append(m.group(0))
        return "\u0002%d\u0002" % (len(prot) - 1)

    def _rest(m):
        return prot[int(m.group(1))]

    t = re.sub(r"\$\$[\s\S]+?\$\$|\$[^$\n]+?\$", _save, text)
    # 源损坏守卫：保护已配对 $..$ 后仍剩 $（未配对）或花括号不平衡 → 源乱码（如
    # "0.98°s { - 1 }$(bareLIG),to1.92°s-1}}"），**不包裹**，防止把乱码越搞越坏
    # （2026-08-25 回归实证：对乱码包裹产生 "$$...$"-1}}$$" 更坏）。
    if "$" in t or _brace_imbalance(t) != 0:
        return re.sub(r"\u0002(\d+)\u0002", _rest, t)
    # 数学信号字符（_/^/@/{}/\LaTeX 命令/Unicode 数学符/上下标/单位）
    _MS = (r"_^@\\{}"
           r"\u0394\u03a6\u03c6\u03c8\u03b8\u03a9\u03bc\u00b0"
           r"\u00b1\u00d7\u00f7\u00b7\u2212\u2013\u221e\u2248\u2260\u2264\u2265"
           r"\u00b9\u00b2\u00b3\u2070\u2074\u2075\u2076\u2077\u2078\u2079"
           r"\u207b\u207a\u2080\u2081\u2082\u2083\u2084\u2085\u2086\u2087\u2088\u2089")
    _A = (r"[A-Za-z0-9./()\u2212\u00b1\u00d7\u00f7\u00b7,=@"
          r"\u0394\u03a6\u03c6\u03c8\u03b8\u03a9\u03bc\u00b0"
          r"\u221e\u2248\u2260\u2264\u2265"
          r"\u00b9\u00b2\u00b3\u2070\u2074\u2075\u2076\u2077\u2078\u2079\u207b\u207a"
          r"\u2080\u2081\u2082\u2083\u2084\u2085\u2086\u2087\u2088\u2089_^/{}\-]")
    # 尾部 _A+（≥1 字符）：不包裹残缺信号（"E_" 无后续内容 → 不包成 $\mathrm{E_}$，
    # 防 "E_$Fermi level" 碎片）
    _pat = re.compile(r"(?<![A-Za-z0-9$])(" + _A + r"*[" + _MS + r"]" + _A + r"+)")
    t = _pat.sub(lambda m: "$\\mathrm{%s}$" % m.group(1).rstrip(".,;:"), t)
    return re.sub(r"\u0002(\d+)\u0002", _rest, t)


def _fix_formula_commands(text: str) -> str:
    r"""[局部] P16 公式命令正确性校验：mineru 常把氮气识别成希腊字母 Nu（\Nu_{2} 在
    源里就有，双通道若无冲突不会改）。化学语境下 \Nu 后跟数字下标 = 氮气签名 →
    \mathrm{N}_{下标}（保渲染相同"N"、语义正确，供 AI/翻译识别为 nitrogen 而非希腊
    字母 Nu）。仅处理 \Nu+数字下标，不触碰 \nu/\Nu 其他合法用法。"""
    if not text:
        return text
    return re.sub(r"\\Nu\s*_\s*\{?\s*(\d+)\s*\}?",
                  r"\\mathrm{N}_{\1}", text)


# P16：LaTeX 希腊命令 → Unicode（� 乱码解析时，参考通道的 LaTeX 命令需还原成字符）
_LATEX_GREEK = {
    r"\varphi": "\u03c6", r"\phi": "\u03c6", r"\Phi": "\u03a6",
    r"\epsilon": "\u03b5", r"\varepsilon": "\u03b5",
    r"\pi": "\u03c0", r"\Pi": "\u03a0", r"\theta": "\u03b8",
    r"\mu": "\u03bc", r"\nu": "\u03bd", r"\Nu": "\u039d",
    r"\alpha": "\u03b1", r"\beta": "\u03b2", r"\gamma": "\u03b3",
    r"\delta": "\u03b4", r"\sigma": "\u03c3", r"\omega": "\u03c9",
    r"\Omega": "\u03a9", r"\Delta": "\u0394",
}


def _best_math_char(seg: str) -> str | None:
    r"""[局部] 从 diff 参考片段提取最有意义的替换字符：先查 LaTeX 希腊映射，再去
    $/\mathrm{} 标记取字母或数学符号；无则 None（不替换，防取到 '$'/'\\' 等标记）。"""
    if not seg:
        return None
    for cmd, ch in _LATEX_GREEK.items():
        if cmd in seg:
            return ch
    t = seg.replace("$", "")
    t = re.sub(r"\\mathrm\s*\{([^{}]*)\}", r"\1", t)
    t = re.sub(r"\\[a-zA-Z]+", "", t)
    t = re.sub(r"[\s{}\[\]]", "", t)
    for ch in t:
        if ch not in "\ufffd\\$_^~":
            if ch.isalpha() or ch in "\u03b1-\u03c9\u0391-\u03a9\u2212\u00b0\u03bc":
                return ch
    return None


def _resolve_replacement_chars(text: str, ref_text: str) -> tuple[str, int]:
    """[局部] P16：把段落里的 U+FFFD(�) 用参考通道(ref_text，百度OCR/骨架 PyMuPDF)
    对齐字符替换。difflib 对齐 text↔ref，� 所在片段取 ref 对齐片段的最优字符（容
    两通道格式差异——ref 可带 $/LaTeX 命令，� 只出现在 mineru 侧）。无法解析则不换。
    返回 (新文本, 替换次数)。"""
    if "\ufffd" not in (text or "") or not ref_text:
        return text, 0
    sm = difflib.SequenceMatcher(None, text, ref_text)
    out = list(text)
    count = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        seg = text[i1:i2]
        if "\ufffd" not in seg:
            continue
        repl = _best_math_char(ref_text[j1:j2])
        if repl is None:
            continue
        for k in range(i1, i2):
            if out[k] == "\ufffd":
                out[k] = repl
                count += 1
    return "".join(out), count


# P16：公式块定位（$...$ / $$...$$）
_FORMULA_BLOCK_RE = re.compile(r"\$\$[\s\S]+?\$\$|\$[^$\n]+?\$")


def _brace_imbalance(inner: str) -> int:
    r"""[局部] 数学块内花括号 { } 平衡度：正=缺 }，负=缺 {，0=平衡。忽略 \{ \} 转义。"""
    t = re.sub(r"\\[{}]", "", inner or "")
    return t.count("{") - t.count("}")


def _formula_self_check(md_text: str) -> tuple[str, list[str]]:
    """[局部] P16 输出前公式自检：扫描最终 markdown 的 $ 配对/括号配对。
    **自动修复安全项**：数学块内缺 }（截断常见，补足）；$ 数奇数/缺 {（更危险）
    → 只记录到 issues（供 AI/人工兜底），不敢乱改。返回 (修复后文本, issues)。"""
    issues: list[str] = []
    if not md_text:
        return md_text, issues
    seen: set = set()

    def _fix_braces(m: re.Match) -> str:
        whole = m.group(0)
        body = whole[2:-2] if whole.startswith("$$") else whole[1:-1]
        imb = _brace_imbalance(body)
        if imb > 0:
            fixed = whole[:-1] + "}" * imb + whole[-1]   # 结尾 $ 前补 }
            key = (whole, fixed)
            if key not in seen:
                seen.add(key)
                issues.append("[公式括号缺}×%d] %s → %s"
                              % (imb, whole[:55], fixed[:60]))
            return fixed
        return whole

    t = _FORMULA_BLOCK_RE.sub(_fix_braces, md_text)
    n = t.count("$")
    if n % 2:
        issues.append("[$不配对] 全文 $ 数为奇数(%d)——定位第一个可疑段落" % n)
    return t, issues


def _build_domain_review_item(r, orig_text: str, sugg_text: str,
                              fixes: list[dict], *, applied: bool,
                              page: int = 1) -> dict:
    """[局部] 词典层复核项（共识错误：conf 0.8 review 候选待确认 /
    conf 0.95 自动修复 audit 展示）。与仲裁复核项同格式，GUI 复核区复用：
    mineru 侧=校正前段落，paddleocr 侧=词典建议全文，diff 标红定位差异。"""
    mid = ("md%d" % (r.md_idx or [1])[0]) if r.md_idx else ("para-" + r.para_id)
    conf = max((f.get("confidence") or 0) for f in fixes)
    rules = ",".join(f.get("rule", "") for f in fixes)
    return {
        "report_idx": 0,                       # 合并时重排
        "page": page,                          # md 段→本地行反查；无则 1（页图可加载）
        "blocking": True,                      # ★2026-09-16：与仲裁项统一 schema
        "item_kind": "domain",                 # 词典层（共识错误）修复项
        "mineru": {"block_id": mid, "kind": r.kind, "text": orig_text},
        "paddleocr": {"block_id": "paddle-" + r.para_id, "kind": r.kind,
                      "text": sugg_text},
        "ai": {"verdict": "both" if applied else "paddleocr",
               "reason": ("词典校正%s(%s conf %s)" % ("已自动" if applied else "待确认",
                                                      rules, conf))[:80],
               "confidence": conf, "applied": applied},
        "user_choice": "", "auto_resolved": "",
        "evidence": {"para_id": r.para_id, "md_idx": r.md_idx or [],
                     "kind": "domain", "rules": rules},
    }


def _build_review_items(review_cands: list[dict],
                        conflicts_by_para: dict[str, list[dict]],
                        page_by_para: dict[str, int]) -> list[dict]:
    """[局部] P15 复核清单构建（conf<0.8/unresolved → review.json items，
    与 P12 同格式，GUI 复核复用 ReviewService）。

    2026-08-26 用户决策：复核项粒度 = **单差异点**（不再是整段对照）——
    只把 AI 不确定的那个 conflict 送复核，mineru/paddleocr 文本 = 该点片段，
    用户只需看 AI 说明的这一处，不被其他差异标注干扰。

    review_cands: [{para_id, r(RepairItem), pending([Arbitration...])}]
    conflicts_by_para: {para_id: [conflict...]}（局部索引与 a.id 对应）
    """
    items = []
    for cand in review_cands:
        r = cand["r"]
        mid = ("md%d" % (r.md_idx or [1])[0]) if r.md_idx else ("para-" + r.para_id)
        cfl = conflicts_by_para.get(r.para_id, [])
        for a in cand["pending"]:
            c = cfl[a.id] if 0 <= a.id < len(cfl) else {}
            m_text = (c.get("mineru") or {}).get("text", "")
            p_text = (c.get("paddleocr") or {}).get("text", "")
            if not m_text and not p_text:
                continue
            # ★2026-09-16：第三信号（PDF 文本层）作为**证据**进复核项——人工/AI 判不准时，
            # "PDF 原文支持哪一侧"是最强旁证（不自动落地，权限仍在复核环节）。
            tv = c.get("third_vote") or {}
            reason = (a.reason or "")[:80]
            if tv.get("verdict") and tv["verdict"] != "unknown":
                if tv.get("decisive") is False:
                    _tv_txt = "文本层未区分（%s）" % tv["verdict"]
                else:
                    _tv_txt = "文本层支持：%s" % ("MinerU" if tv["verdict"] == "mineru"
                                                  else "PaddleOCR")
                reason = (reason + "｜" + _tv_txt)[:110]
            items.append({
                "report_idx": len(items),
                "page": page_by_para.get(r.para_id, 0),
                "blocking": True,                 # ★阻断翻译门控（待用户确认）
                "item_kind": "conflict",
                "mineru": {"block_id": mid, "kind": r.kind, "text": m_text},
                "paddleocr": {"block_id": "paddle-" + r.para_id, "kind": r.kind,
                              "text": _wrap_paddle_formulas(p_text)},
                "ai": {"verdict": a.verdict, "reason": reason,
                       "confidence": a.confidence, "applied": False},
                "third_vote": tv or {},
                "user_choice": "", "auto_resolved": "",
                "evidence": {"para_id": r.para_id, "md_idx": r.md_idx or [],
                             "conflict_idx": a.id,
                             "third_vote": (tv.get("verdict") if tv else "")}})
    return items


def _build_skip_items(skips: list[dict], page_by_para: dict[str, int]) -> list[dict]:
    """[局部] 未能自动落地的冲突项（`skip_p_ambiguous`）→ **阻断**复核项。

    2026-09-16 收口：`_apply_arbitrations` 对"片段在段内多次出现"的冲突主动放弃替换
    （宁缺毋滥），但此前只写进无人读的 arbitration_audit.jsonl ⇒ 用户看不到"有冲突没修"。
    """
    items: list[dict] = []
    for s in skips:
        r = s["r"]
        mid = ("md%d" % (r.md_idx or [1])[0]) if r.md_idx else ("para-" + r.para_id)
        items.append({
            "report_idx": 0,                  # 合并后统一重编号
            "page": page_by_para.get(r.para_id, 0),
            "blocking": True,
            "item_kind": "skip_ambiguous",
            "mineru": {"block_id": mid, "kind": r.kind, "text": s.get("chunk", "")},
            "paddleocr": {"block_id": "paddle-" + r.para_id, "kind": r.kind, "text": ""},
            "ai": {"verdict": "unresolved", "confidence": 0.0, "applied": False,
                   "reason": "该片段在段落中出现 %s 次，未自动替换（需人工确认）"
                             % s.get("count", 0)},
            "user_choice": "", "auto_resolved": "",
            "evidence": {"para_id": r.para_id, "md_idx": r.md_idx or [],
                         "conflict_idx": -1, "source": "apply_skip"}})
    return items


def _build_quality_items(flags: list[dict]) -> list[dict]:
    """[局部] 质量提示项（misaligned / 低重叠段）→ **非阻断**（不门控翻译，只求可见）。

    2026-09-16（用户："看不到的错误要可见"）：这些段此前被静默跳过（不一致也不告警），
    ⇒ 它们永远只用 MinerU 结果。现在进同一份 review.json 供前端"质量提示"区展示；
    `blocking=False` 保证不会把每篇论文都卡在"待复核"。
    """
    items: list[dict] = []
    for f in flags:
        para_id = f.get("para_id", "")
        mid = "para-" + para_id
        items.append({
            "report_idx": 0,                  # 合并后统一重编号
            "page": f.get("page", 0),
            "blocking": False,
            # 来源分类：quality（misaligned/低重叠）| third_signal（共识错误/丢内容）
            "item_kind": f.get("item_kind") or "quality",
            "mineru": {"block_id": mid, "kind": f.get("kind", "body"),
                       "text": f.get("mineru_text", "")},
            "paddleocr": {"block_id": "paddle-" + para_id, "kind": f.get("kind", "body"),
                          "text": _wrap_paddle_formulas(f.get("paddle_text", ""))},
            "ai": {"verdict": "unresolved", "confidence": 0.0, "applied": False,
                   "reason": (f.get("reason") or "")[:120]},
            "user_choice": "", "auto_resolved": "",
            "evidence": {"para_id": para_id, "md_idx": [], "conflict_idx": -1,
                         "source": "verify", "verify_verdict": f.get("verdict", ""),
                         "overlap_set": f.get("overlap_set", 0.0)}})
    return items


def _extract_md_meta(md_text: str, pdf_name: str = "") -> tuple[str, str, list[str], str, list[str]]:
    """[局部] 从 mineru full.md 头部尽力提取元数据（**宁缺毋滥**，提取不到留空）。

    返回 (title, abstract, keywords, doi, authors)：
    - title：首个 "# " 单级标题行；
    - abstract："## Abstract" 后第一个非空段落（折叠换行/剥离内联 LaTeX $..$）；
    - keywords："Keywords:" 行（; 或 , 分隔）；
    - doi：md 中 "DOI:" 行；其次 pdf_name（Wiley 目录名 10.xxxx_x 形态尽力还原）；
    - authors：标题后第一个非空段落（逗号分隔姓名、无机构关键词——机构行/邮箱/
      搜索行不是作者，直接放弃提取，宁缺毋滥）。
    """
    lines = [ln.rstrip() for ln in md_text.splitlines()]

    title = ""
    for ln in lines:
        m = re.match(r"^#\s+(.+)$", ln.strip())
        if m:
            title = m.group(1).strip()
            break

    abstract = ""
    abstract_started = False
    for ln in lines:
        # Elsevier 排版 "A B S T R A C T"（字母间空格）→ 去空格匹配
        if re.match(r"^##\s*abstract\b", re.sub(r"\s+", "", ln.strip()),
                    re.I):
            abstract_started = True
            continue
        if not abstract_started:
            continue
        if re.match(r"^#+", ln.strip()):
            break
        if ln.strip():
            abstract = re.sub(r"\s+", " ",
                              re.sub(r"\$[^$]*\$", "", ln)).strip()
            break

    keywords: list[str] = []
    for ln in lines:
        # Elsevier 排版 "K e y w o r d s" → 去单词内空格再匹配
        m = re.match(r"^\s*[Kk]eywords?\s*[:：]\s*(.+)$", ln) or \
            re.match(r"^\s*([Kk]eywords?)\s*[:：]\s*(.+)$",
                     re.sub(r"(?<=[A-Za-z])\s+(?=[A-Za-z])", "", ln.strip()))
        if m:
            _kw = m.group(m.lastindex)   # 两个模式组数不同 → 取最后一个捕获组
            if re.search(r"[;；,，]", _kw):
                keywords = [k.strip() for k in re.split(r"[;；,，]", _kw)
                            if k.strip()]
            # 无分隔符（Elsevier "Ionic polymer sensor Ionic liquid..." 空格
            # 相连 5 个短语）→ 无法可靠切分 → 宁缺毋滥返回空
            break

    doi = ""
    for ln in lines:
        m = re.search(r"DOI\s*[:：]\s*(10\.\d{4,9}/[^\s,;]+)", ln, re.I)
        if m:
            doi = m.group(1).strip().rstrip(".,;")
            break
    if not doi:
        m = re.search(r"(10\.\d{4,9}[/_][\w.\-()/:;]+)", pdf_name)
        if m:
            # 目录名形态 "10.1016_j.cej.2025.167798.pdf" → 标准 DOI
            doi = m.group(1).replace("_", "/")
            doi = re.sub(r"\.pdf$", "", doi).rstrip(".")

    authors: list[str] = []
    _AFF_KW = ("university", "institute", "laboratory", "department",
               "school", "college", "academy", "center", "centre", "univ.")
    if title:
        t_idx = next((i for i, ln in enumerate(lines)
                      if re.match(r"^#\s+(.+)$", ln.strip())
                      and re.match(r"^#\s+(.+)$", ln.strip()).group(1).strip()
                      == title), None)
        if t_idx is not None:
            for ln in lines[t_idx + 1:]:
                s = ln.strip()
                if not s or s.startswith("#"):
                    continue
                low = s.lower()
                if any(k in low for k in _AFF_KW) or re.match(
                        r"^(doi|e-?mail|search|keywords?)\b", low):
                    break                       # 机构/邮箱行 → 非作者，放弃
                if "," in s and re.search(r"[A-Za-z]", s):
                    parts = [p.strip() for p in s.split(",") if p.strip()]
                    if parts and all(re.search(r"[A-Za-z]", p) for p in parts):
                        authors = parts
                break
    return title, abstract, keywords, doi, authors


def to_article_document(repair_items: list, figures: list,
                        md_text: str, pdf_name: str = "") -> ArticleDocument:
    """[全局] p14 修复段落流 + 图 → ArticleDocument（与 load_document 兼容）。

    背景：p14 原 document.json 是手写精简格式（meta/title/abstract/paragraphs/
    figures/references），`load_document`（翻译/导出/会话全依赖）读不了 → 本
    转换层让 p14 落盘 ArticleDocument 兼容格式（save_document 写盘）。

    - metadata：从 md 头部尽力提取（title=首个 # 标题、abstract=## Abstract 后
      首段、keywords/doi/authors 尽力提取——**宁缺毋滥**，不为元数据折腾）；
      parser="p14"、source_pdf=pdf_name；
    - paragraphs：repair_items → Paragraph（para_id="P%03d"、order、section、
      source_block_ids=["mdN"]、confidence、is_heading/is_caption、
      coords={"pages": [], "source": "mineru-md"}）；
      **image 段跳过**（P15 用户反馈：image 段 text 含 mineru OCR 残留
      `![](images/<hash>.jpg)` 与图注文本——转成普通段落会被渲染层原样输出
      成垃圾/图注重复；图片由 figures 管理，渲染层按图注编号自动插图）；
    - figures：p14 Figure → schema Figure（fig_id/file/caption/page）；
    - tables/references 留空（p14 References 已渲染为 markdown 表格，不转）。
    """
    paras = []
    for i, r in enumerate(repair_items or []):
        if getattr(r, "kind", "") == "image":
            continue          # P15：image 段不转段落（图片由 figures + 渲染层管理）
        text = (r.text or "").strip()
        # P15：正文段里的图片标记残留（mineru 把 "d ![](...)" 图注残留拼进
        # body 段）→ 剥离；剥离后为空（纯标记）→ 跳过
        text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text).strip()
        if not text:
            continue
        # P16：body 段图面板标签 "(a)(b)" 噪声清洗（图注段保留子图编号）
        if getattr(r, "kind", "") == "body":
            text = _strip_panel_labels(text)
        if not text:
            continue
        # 2026-08-26：引用编号上标化（与 build_markdown 一致，document.json 同步）
        if getattr(r, "kind", "") == "body":
            text = _sup_inline_refs(text)
        # P16：公式碎片归一化（与 build_markdown 保持一致）
        if getattr(r, "kind", "") == "body":
            text = normalize_formula_fragments(text)
        if not text:
            continue
        # P16 修复（2026-08-26）：与 build_markdown 公式修复链对齐——孤儿 LaTeX 补 $
        # + 命令正确性校验（\Nu_{2}→\mathrm{N}_{2}）。此前只修 en.md 不修 document.json
        # → GUI 阅读区（读 document.json）残留 \Nu_{2} 等错式。
        if getattr(r, "kind", "") in ("body", "caption"):
            text = wrap_orphan_latex(text)
            text = _fix_formula_commands(text)
        try:
            conf = min(1.0, max(0.0, float(getattr(r, "confidence", 1.0))))
        except (TypeError, ValueError):
            conf = 1.0
        paras.append(Paragraph(
            para_id="P%03d" % (i + 1), order=i + 1,
            section=r.section or "", text_en=text,
            source_block_ids=["md%d" % x for x in (r.md_idx or [])],
            confidence=conf, needs_ai_check=False, is_calibrated=False,
            is_heading=r.kind == "heading", is_caption=r.kind == "caption",
            coords={"pages": [], "source": "mineru-md"}))
    title, abstract, keywords, doi, authors = _extract_md_meta(md_text, pdf_name)
    meta = ArticleMetadata(
        title=title, abstract=abstract or None, keywords=keywords,
        doi=doi or None, authors=authors, source_pdf=pdf_name, parser="p14")
    return ArticleDocument(
        metadata=meta, paragraphs=paras,
        figures=[Figure(fig_id=f.fig_id, file=f.file, caption=f.caption,
                        page=f.page) for f in (figures or [])],
        tables=[], references=[], ai_summary=None,
        audit=DocumentAudit(run_id=""))


def process_pdf_v2(pdf_path: str | Path, *, md_path: str | Path | None = None,
                   md_text: str | None = None, out_dir: str | Path = "output/v2",
                   paddle: bool = True, ai_review: bool = True,
                   provider=None, run_id: str | None = None,
                   paddle_blocks_path: str | Path | None = None,
                   sf_ocr: bool = False,
                   cancel_check=None, on_wait=None) -> dict:
    """[全局] ★ P14 管线门面：本地骨架 + mineru md 修复 + 双通道验证 + 字符仲裁

    参数:
        pdf_path: 输入 PDF
        md_path: mineru full.md 路径（文本基底；与 md_text 二选一）
        md_text: 直接传 mineru md 文本
        out_dir: 输出根目录（<out_dir>/<pdf_stem>/ 下 document.json + en.md + images/）
        paddle: 是否跑 OCR 辅通道（M7 验证 + M8 仲裁；False 则纯 M1-M6）
        ai_review: 辅通道下是否 AI 仲裁（False 只出验证信号与 diff 清单）
        provider: 仲裁模型 provider（默认环境链）
        paddle_blocks_path: 复用已有 paddleocr blocks.json（官方云队列满/离线时）
        sf_ocr: 用硅基流动 PaddleOCR-VL 替代官方云（无排队；无 bbox → 配对走
                内容 Dice，跳过 y 重叠拼接；**P15 已弃用**——官方云唯一通道，
                本参数仅保留 debug 用途）
        cancel_check/on_wait: P15 官方云队列等待机制（透传 PaddleOCRClient）
    返回: 信封 dict（status/run_id/document_json/en_md/... + stats）
    """
    started = time.time()
    # 加载 .env 到进程环境（仲裁 provider 读 os.environ；缓存路径不经
    # PaddleOCRClient 不会触发 load_config → 必须显式加载）
    # 批2：返回值接住 —— PaddleOCR optionalPayload 取自 cfg（restructurePages 等）
    from paperparse.config import load_config
    cfg = load_config()
    pdf = Path(pdf_path)
    stem = pdf.stem
    if md_text is None:
        if md_path is None:
            return {"status": "error", "error": "需提供 md_path 或 md_text",
                    "note": "P14 管线以 mineru full.md 为文本基底"}
        md_text = Path(md_path).read_text(encoding="utf-8")

    out = Path(out_dir) / stem
    out.mkdir(parents=True, exist_ok=True)
    images_dir = out / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    work = out / "work"
    work.mkdir(parents=True, exist_ok=True)

    stats: dict = {}

    # ---- M1-M3 本地骨架（权威边界）----
    skeleton = build_local_skeleton(str(pdf))
    stats["skeleton"] = skeleton.stats

    # ---- M4 大图本地提取（图注驱动）----
    figures = []
    try:
        figures = extract_figures_caption_driven(str(pdf), skeleton, images_dir)
    except Exception as e:  # noqa: BLE001 - 图提取失败不阻塞文本管线
        stats["figures_error"] = str(e)[:120]
    stats["figures"] = len(figures)

    # ---- M5 对齐 ----
    align = align_md_to_skeleton(md_text, skeleton)
    stats["align"] = align.stats

    # ---- M6 拼接修复 ----
    repair = repair_md_paragraphs(md_text, skeleton)
    stats["repair"] = repair.stats

    # ---- M7 双通道验证 + M8 字符仲裁（paddleocr 通道）----
    verify_items: list[dict] = []
    arb_stats = {"arbitrated": 0, "unresolved": 0, "applied_p": 0}
    _third_stats: dict = {}       # ★2026-09-16：第三信号（PDF 文本层）统计
    if paddle:
        pblocks = None
        # 2026-08-26：sf_ocr（硅基流动）通道已移除（P15 弃用，模块归档）；
        # 2026-09-16：其残留判据（sf_para_text / paddle_source=="sf"）一并删除——
        # 该变量恒空、判据恒假，官方云 PaddleOCR 是唯一辅通道。
        if paddle_blocks_path:
            try:
                from paperparse.middleware.schema import ParserBlocks
                _blk = json.loads(Path(paddle_blocks_path).read_text(encoding="utf-8"))
                # 缓存 JSON 为字符串化存储（bbox/page 是 str）→ 还原类型
                _parsed = []
                for b in _blk:
                    try:
                        _bbox = [float(x) for x in
                                 str(b.get("bbox", "[0,0,0,0]"))
                                 .strip("[]").split(",")]
                    except (ValueError, AttributeError):
                        _bbox = [0.0, 0.0, 0.0, 0.0]
                    _parsed.append({
                        "block_id": str(b.get("block_id", "")),
                        "page": int(float(b.get("page", 0))),
                        "bbox": _bbox, "text": str(b.get("text", "")),
                        "kind": str(b.get("kind", "other"))})
                pblocks = ParserBlocks(
                    source="paddleocr",
                    pages=max([x["page"] for x in _parsed] or [0]),
                    blocks=_parsed)
                stats["paddle_source"] = "cache"
            except Exception as e:  # noqa: BLE001
                stats["paddle_error"] = "缓存加载失败: %s" % str(e)[:120]
        if pblocks is None:
            try:
                from paperparse.core.parse_params import paddleocr_options_from_cfg
                from paperparse.core.paddleocr_client import PaddleOCRClient
                # 批2：下发 optionalPayload（restructurePages/mergeTables/relevelTitles，默认全开）。
                # 此前不传 options ⇒ 服务端默认 restructurePages=false ⇒ 跨页表格被拆断、标题层级不重整。
                _po_opts = paddleocr_options_from_cfg(cfg) if cfg is not None else None
                _po_client = PaddleOCRClient()
                pblocks = _po_client.parse_pdf(
                    str(pdf), backup=True, options=_po_opts,
                    cancel_check=cancel_check, on_wait=on_wait)
                stats["paddle_options"] = _po_opts
            except Exception as e:  # noqa: BLE001
                pblocks = None
                stats["paddle_error"] = str(e)[:160]
        if pblocks and getattr(pblocks, "blocks", None):
            md_of_local = _md_of_local(skeleton, align.line_map)
            local_by_id = {p.para_id: p for p in skeleton.paragraphs}
            conflicts_all: list[dict] = []
            pv_by_para: dict[str, dict] = {}
            quality_flags: list[dict] = []             # ★2026-09-16：非阻断质量提示
            skipped_ambiguous: list[dict] = []         # ★2026-09-16：有冲突但未能自动落地
            paddle_text_by_para: dict[str, str] = {}   # P15 复核清单：段落对照文本
            page_by_para: dict[str, int] = {}
            # 内容锚定配对（P16：百度段→修复段对照文本，替代坐标法 _paddle_join_for）
            # 2026-09-16：删除 sf（硅基流动）分支残留——`sf_para_text` 恒空、判据恒假，
            # 官方云 PaddleOCR 现在是唯一辅通道（sf_ocr 形参仅保留 API 兼容）。
            content_pair, content_page = _content_pair_paddle(
                repair.paragraphs, pblocks,
                md_of_local=md_of_local)
            # ★2026-09-16 M8d：**第三信号 = PDF 自带文本层**（0 API 成本；按页门控）。
            # 双通道是"分歧检测器"而非"正确性检测器"（两侧同错 ⇒ 零报告），文本层用来
            # ① 给冲突项投第三票（不自动落地，只进复核证据）；② 检测共识错误；③ 检测丢内容。
            # 扫描件文本层接近 0 ⇒ usable 为空集时整块跳过，行为与旧版完全一致。
            _local_pages: dict[int, str] = {}
            _local_usable: set[int] = set()
            try:
                from paperparse.core.local_text import (page_texts, unverified_tokens,
                                                        usable_pages, vote_conflict)
                _local_pages = page_texts(str(pdf))
                _local_usable = usable_pages(_local_pages)
            except Exception as e:  # noqa: BLE001 - 第三信号不可用不影响解析
                stats["third_signal_error"] = str(e)[:120]
            _third_stats = {"pages_total": len(_local_pages), "pages_usable": len(_local_usable),
                            "votes": {}, "unverified": 0, "enabled": bool(_local_usable)}
            _page_cache: dict[int, str] = {}

            def _pages_for(md_idx) -> list[int]:
                """该修复段涉及的本地页号（`LocalPara.pages` 是列表、`LocalLine.page` 是单值）。"""
                out: list[int] = []
                for mid in (md_idx or []):
                    for _lp in md_of_local.get(mid, []):
                        _pgs = list(getattr(_lp, "pages", None) or [])
                        if not _pgs:
                            _pg = getattr(_lp, "page", 0)
                            _pgs = [_pg] if _pg else []
                        for pg in _pgs:
                            if pg and pg not in out:
                                out.append(pg)
                return out

            def _page_text_for(md_idx) -> str:
                """该段所在**页**的文本层内容（token 级核对与冲突第三票都用页级：
                段落级映射（md 段 ↔ 本地行组）并非一一对应，实测误报严重，已弃用）。"""
                key = (md_idx or [0])[0]
                if key in _page_cache:
                    return _page_cache[key]
                txt = ""
                if _local_usable:
                    pgs = [p for p in _pages_for(md_idx) if p in _local_usable]
                    txt = " ".join(_local_pages.get(p, "") for p in pgs).strip()
                _page_cache[key] = txt
                return txt

            for r in repair.paragraphs:
                if r.kind not in ("body", "caption"):   # P16：图注也参与对照修正
                    continue
                paddle_text = content_pair.get(r.para_id, "")
                local_txt = _page_text_for(r.md_idx)
                if not paddle_text:
                    stats.setdefault("pairing", {}).setdefault("none", 0)
                    stats["pairing"]["none"] += 1
                    _third_stats.setdefault("no_paddle_pair", 0)
                    _third_stats["no_paddle_pair"] += 1
                    # ③ token 级共识检查：辅通道没配上（无对照）时，用文本层核对
                    #    "公式/带单位数值"是否真的存在于该页 —— 专治两通道同错的盲区
                    #    （段落级 Dice/长度比版本在真实论文上误报严重，已弃用，见 local_text.py）。
                    if local_txt:
                        bad = unverified_tokens(r.text, local_txt)
                        if bad:
                            _third_stats["unverified"] += len(bad)
                            _pgs = _pages_for(r.md_idx)
                            quality_flags.append({
                                "para_id": r.para_id,
                                "page": (_pgs or [0])[0],
                                "verdict": "unpaired", "overlap_set": 0.0, "kind": r.kind,
                                "mineru_text": r.text[:400], "paddle_text": "",
                                "local_text": local_txt[:400],
                                "item_kind": "third_signal",
                                "reason": "PDF 文本层中找不到这些公式/数值：%s（可能两通道同错）"
                                          % "、".join(b["token"] for b in bad[:6])})
                    continue
                stats.setdefault("pairing", {}).setdefault("content", 0)
                stats["pairing"]["content"] += 1
                page = content_page.get(r.para_id, 0)
                paddle_text_by_para[r.para_id] = paddle_text
                page_by_para[r.para_id] = page
                pv = verify_pair(r.text, paddle_text, para_id=r.para_id)
                pv_by_para[r.para_id] = pv.to_dict()
                verify_items.append({"para_id": r.para_id, "page": page,
                                     "mineru_words": len(pv.mineru_words),
                                     "paddle_words": len(pv.paddle_words),
                                     **pv.to_dict()})
                # Q5 防线3：段首对齐失败（misaligned=配对错位/误拼接）→ 跳过
                # char_conflicts（不产生 diff 项、不送 AI 仲裁，省 token）
                if pv.verdict == "misaligned":
                    stats.setdefault("pairing", {}).setdefault("misaligned", 0)
                    stats["pairing"]["misaligned"] += 1
                    quality_flags.append({
                        "para_id": r.para_id, "page": page, "verdict": pv.verdict,
                        "overlap_set": pv.overlap_set, "kind": r.kind,
                        "mineru_text": r.text[:400], "paddle_text": paddle_text[:400],
                        "reason": "段首对齐失败（配对错位/误拼接）→ 本段未做双通道比对"})
                    continue
                # M8：只对高重叠段做字符级 diff（真实拼写/标点差异）
                if pv.overlap_set >= 0.9:
                    conflicts = char_conflicts(r.text, paddle_text, page)
                    for c in conflicts:
                        c["para_id"] = r.para_id
                        # ① 第三票：文本层支持哪一侧（只进复核证据，**不自动落地**）。
                        #    用 **±60 字符上下文窗口**（evidence.m_ctx/p_ctx）而不是那 1~2 个
                        #    字符的差异片段——实测冲突绝大多数是"插一个空格/字母"，片段本身
                        #    短到无法判定；上下文窗口≥12 字符才有区分度，且两侧窗口天然只差
                        #    冲突处那几个字符 ⇒ "哪一侧窗口在 PDF 文本层里逐字出现"即可裁决。
                        if local_txt:
                            tv = vote_conflict(c, local_txt)
                            if tv.get("verdict") not in (None, "unknown"):
                                c["third_vote"] = tv
                                _third_stats["votes"][tv["verdict"]] = \
                                    _third_stats["votes"].get(tv["verdict"], 0) + 1
                                if tv.get("decisive") is False:
                                    _third_stats.setdefault("inconclusive", 0)
                                    _third_stats["inconclusive"] += 1
                    conflicts_all.extend(conflicts)
                    # ② token 级共识检查（**已配对**的段也要做：两通道一致但与 PDF 文本层
                    #    不符 = 共识错误，char_conflicts 永远看不到）
                    if local_txt:
                        bad = unverified_tokens(r.text, local_txt)
                        if bad:
                            _third_stats["unverified"] += len(bad)
                            quality_flags.append({
                                "para_id": r.para_id, "page": page,
                                "verdict": "consensus", "overlap_set": pv.overlap_set,
                                "kind": r.kind, "mineru_text": r.text[:400],
                                "paddle_text": paddle_text[:400],
                                "local_text": local_txt[:400],
                                "item_kind": "third_signal",
                                "reason": "PDF 文本层中找不到这些公式/数值：%s（可能两通道同错）"
                                          % "、".join(b["token"] for b in bad[:6])})
                else:
                    # ★2026-09-16 收口：**低重叠段（0.7~0.9）此前静默跳过**，既不出冲突项
                    # 也不告警 ⇒ 这些段永远只用 MinerU 结果。现在记进"质量提示"清单
                    # （非阻断：可见、不影响翻译门控，见 blocking=False）。
                    quality_flags.append({
                        "para_id": r.para_id, "page": page, "verdict": pv.verdict,
                        "overlap_set": pv.overlap_set, "kind": r.kind,
                        "mineru_text": r.text[:400], "paddle_text": paddle_text[:400],
                        "reason": "两通道重合度 %.2f < 0.9 → 未做字符级仲裁（仅 MinerU 结果）"
                                  % pv.overlap_set})
            (work / "verify.json").write_text(
                json.dumps({"items": verify_items}, ensure_ascii=False, indent=1),
                encoding="utf-8")
            stats["verify"] = {"items": len(verify_items),
                               "ok": sum(1 for v in verify_items
                                         if v["verdict"] == "ok"),
                               "review": sum(1 for v in verify_items
                                             if v["verdict"] == "review"),
                               "suspicious": sum(1 for v in verify_items
                                                 if v["verdict"] == "suspicious")}
            # ---- M8 仲裁：百度 OCR 为准（P16）----
            # 用户确认：单词/空格/LaTeX/单位/上下标/标点 类错误一律以百度 OCR 为准
            # （实测百度更准），**不再送 AI**；类外歧义项 → unresolved → GUI 复核。
            if conflicts_all:
                from paperparse.core.dual_ai_review import Arbitration
                by_para: dict[str, list[dict]] = {}
                for i, c in enumerate(conflicts_all):
                    by_para.setdefault(c.get("para_id", ""), []).append(c)
                arb_by_idx: dict[int, object] = {}
                for i, c in enumerate(conflicts_all):
                    _m = (c.get("mineru") or {}).get("text", "")
                    _p = (c.get("paddleocr") or {}).get("text", "")
                    if _is_formula_conflict(c) and _formula_equivalent(c):
                        # 公式内容等价（mineru LaTeX = 百度明文同义）→ 保留 mineru
                        # LaTeX（保渲染），不替换成明文，也不进复核
                        arb_by_idx[i] = Arbitration(
                            id=i, diff_type=c.get("type", ""),
                            page=c.get("page", 0), verdict="both",
                            reason="公式内容等价(百度确认)", confidence=1.0,
                            mineru=c.get("mineru", {}),
                            paddleocr=c.get("paddleocr", {}))
                    elif _is_paddle_authoritative(c):
                        arb_by_idx[i] = Arbitration(
                            id=i, diff_type=c.get("type", ""),
                            page=c.get("page", 0), verdict="paddleocr",
                            reason="百度OCR为准", confidence=1.0,
                            mineru=c.get("mineru", {}),
                            paddleocr=c.get("paddleocr", {}))
                    elif _m and not _p:
                        # 用户规则2：百度缺内容（mineru 有、paddle 空）→ MinerU
                        # 自动接收（不需要人工确认）
                        arb_by_idx[i] = Arbitration(
                            id=i, diff_type=c.get("type", ""),
                            page=c.get("page", 0), verdict="mineru",
                            reason="百度缺内容,以MinerU为准", confidence=1.0,
                            mineru=c.get("mineru", {}),
                            paddleocr=c.get("paddleocr", {}))
                    else:
                        arb_by_idx[i] = Arbitration(
                            id=i, diff_type=c.get("type", ""),
                            page=c.get("page", 0), verdict="unresolved",
                            reason="类外歧义,待AI复核", confidence=0.0,
                            mineru=c.get("mineru", {}),
                            paddleocr=c.get("paddleocr", {}))
                # ---- 用户规则3：剩余歧义 → 局部 AI 复核（不整段发送：用冲突片段+
                #      前后上下文 m_ctx/p_ctx；批量一篇一请求 + 自动分块重试，防限流）----
                ai_ids = [i for i, a in arb_by_idx.items() if a.verdict == "unresolved"]
                if ai_ids and ai_review:
                    from paperparse.core.dual_ai_review import arbitrate
                    try:
                        ai_items = [conflicts_all[i] for i in ai_ids]
                        # ★2026-09-16 修：**必须传注入的 provider**——此前不传 ⇒ 内部回落
                        # env 链（DEEPSEEK_*），导致 ①GUI 切换供应商对 M8 仲裁无效；
                        # ②provider 的 on_usage 永不触发 ⇒ 仲裁 token 不进 llm_usage 台账
                        # （实测台账 0 行，成本不可观测）。provider=None 时行为与旧版一致。
                        ai_results = arbitrate(ai_items, provider=provider, paper=stem)
                        for gi, ai_arb in zip(ai_ids, ai_results):
                            if ai_arb is not None:
                                ai_arb.id = gi          # 全局索引
                                arb_by_idx[gi] = ai_arb
                        arb_stats["ai_reviewed"] = len(ai_ids)
                    except Exception as e:  # noqa: BLE001 - AI 失败不阻塞
                        stats["ai_review_error"] = str(e)[:120]
                # ★2026-09-16：**挖掘用快照**——下面 _apply_arbitrations 前会把 a.id 重映射成
                # 段内局部索引（1696-1698），M8c 却按**全局**索引取（原实现因此拿到了错位的
                # arbitration，且调用处传了未定义名 `arb` → 被 except 吞成 char_rules_error，
                # 实测 100% 从未产出 mined_rules.json）。
                arb_snapshot = {i: a for i, a in arb_by_idx.items()}
                arb_stats["arbitrated"] = sum(
                    1 for a in arb_by_idx.values() if a.verdict != "unresolved")
                arb_stats["unresolved"] = sum(
                    1 for a in arb_by_idx.values() if a.verdict == "unresolved")
                applied = 0
                review_cands: list[dict] = []   # P15：conf<0.8/unresolved → GUI 复核
                for para_id, cfl in by_para.items():
                    r = next((x for x in repair.paragraphs
                              if x.para_id == para_id), None)
                    if not r:
                        continue
                    # 该段 conflicts 的 arbitration 索引 = 它们在 conflicts_all 的位置
                    idx_map = {id(c): i for i, c in enumerate(conflicts_all)}
                    sub_arb = []
                    for c in cfl:
                        i = idx_map.get(id(c))
                        if i is not None:
                            sub_arb.append(arb_by_idx.get(i))
                    # 重映射为局部索引（Arbitration.id 是 conflicts_all 全局索引，
                    # _apply_arbitrations 按 cfl 局部 enumerate 匹配）
                    for _k, _a in enumerate(sub_arb):
                        if _a is not None:
                            _a.id = _k
                    new_text, audit = _apply_arbitrations(
                        r.text, cfl, [a for a in sub_arb if a is not None])
                    if new_text != r.text:
                        r.text = new_text
                        applied += 1
                    if audit:
                        (work / "arbitration_audit.jsonl").open(
                            "a", encoding="utf-8").write(
                            json.dumps({"para_id": para_id, "items": audit},
                                       ensure_ascii=False) + "\n")
                        # ★2026-09-16 收口：`skip_p_ambiguous`（片段在段内多次出现→放弃替换）
                        # 此前只写进**无人读**的 audit 文件 ⇒ 用户完全看不到"有冲突没修"。
                        # 现在作为阻断项进复核清单。
                        for _au in audit:
                            if _au.get("action") == "skip_p_ambiguous":
                                skipped_ambiguous.append({
                                    "para_id": para_id, "r": r,
                                    "chunk": _au.get("chunk", ""),
                                    "count": _au.get("count", 0)})
                    # P15：该段任一仲裁 unresolved / (verdict=P & conf<0.8) / **neither（AI 判
                    # 两侧都错）** → 进复核清单（段落聚合；不自动落地）。
                    # ★2026-09-16 修：`neither` 此前**被静默吞掉**——不改文本、不进复核、
                    # audit 也不记（audit 只在文本真变时写）⇒ 用户永远看不到"这段两边都错"。
                    pending = [a for a in sub_arb if a is not None
                               and (a.verdict in ("unresolved", "neither")
                                    or (a.verdict == "paddleocr"
                                        and a.confidence < 0.8))]
                    if pending:
                        review_cands.append({"para_id": para_id, "r": r,
                                             "pending": pending})
                arb_stats["applied_p"] = applied
                arb_stats["neither"] = sum(
                    1 for a in arb_snapshot.values() if a.verdict == "neither")
                arb_stats["skipped_ambiguous"] = len(skipped_ambiguous)
                arb_stats["quality_flags"] = len(quality_flags)
                # ---- M8b 复核清单（P15：conf<0.8/unresolved/neither → review.json，
                #      与 P12 同位置同格式，GUI 复核复用 ReviewService）----
                # 2026-08-26：review.json 归位解析产物目录 <out_dir>/work/
                # （用户决策：library/<DOI>/work/ 自包含，清理简单；不再写
                # 全局 work/dual/ 避免与测试/其他论文互相污染）
                # ★2026-09-16 收口（用户要求"看不到的错误要可见"）：
                #   · blocking=True（阻断翻译门控）= 待确认冲突项 + 未能自动落地的冲突项；
                #   · blocking=False（只提示、不门控）= misaligned / 低重叠段（此前静默跳过）；
                #   · 两类同放 items（GUI 一份清单），各自带 item_kind/blocking 供前端区分。
                review_items = _build_review_items(review_cands, by_para, page_by_para)
                skip_items = _build_skip_items(skipped_ambiguous, page_by_para)
                quality_items = _build_quality_items(quality_flags)
                all_items = review_items + skip_items + quality_items
                for _i, _it in enumerate(all_items):
                    _it["report_idx"] = _i
                blocking_count = len(review_items) + len(skip_items)
                rev_dir = out / "work"
                rev_dir.mkdir(parents=True, exist_ok=True)
                rev_payload: dict = {
                    "count": blocking_count,
                    "quality_count": len(quality_items),
                    "total": len(all_items),
                    "items": all_items,
                    "ai": {**arb_stats, "source": "p14",
                           "pending_review": blocking_count}}
                if not all_items:
                    # 2026-09-12（用户实测报障）：**零待复核项也要落一份显式空清单**——
                    # 否则复核服务找不到 review.json，把"本次无待复核项"误报成
                    # "该文献未走双通道解析"（仲裁其实跑了：arbitration_audit.jsonl 在）。
                    rev_payload["note"] = "双通道已执行，本次无待复核项"
                    stats["review_empty"] = True
                (rev_dir / "review.json").write_text(
                    json.dumps(rev_payload, ensure_ascii=False, indent=1),
                    encoding="utf-8")
                arb_stats["pending_review"] = blocking_count
                stats["review_json"] = str(rev_dir / "review.json")
                stats["review_quality"] = len(quality_items)
                # ---- M8c 字符级规则挖掘（P15：拼写/断词词对 → mined_rules.json，
                #      与 P12 同位置同格式，tools/aggregate_dual_rules.py 跨篇聚合复用）----
                try:
                    from paperparse.core.rule_mining import mine_char_rules
                    # 2026-09-16 修：改用**全局索引快照** arb_snapshot（旧代码变量名 `arb`
                    # 未定义 → NameError 被 except 吞掉，规则挖掘从未真正运行）。
                    char_cands = mine_char_rules(conflicts_all,
                                                 list(arb_snapshot.values()), paper=stem)
                    if char_cands:
                        mined = [c.to_rule() for c in char_cands]
                        rev_dir = out / "work"
                        rev_dir.mkdir(parents=True, exist_ok=True)
                        (rev_dir / "mined_rules.json").write_text(
                            json.dumps(mined, ensure_ascii=False, indent=1),
                            encoding="utf-8")
                        arb_stats["char_rules"] = len(mined)
                    else:
                        arb_stats["char_rules"] = 0
                except Exception as e:  # noqa: BLE001 - 挖掘失败不阻塞
                    stats["char_rules_error"] = str(e)[:120]
            (work / "char_conflicts.json").write_text(
                json.dumps({"items": conflicts_all,
                            "stats": arb_stats}, ensure_ascii=False, indent=1),
                encoding="utf-8")
        else:
            stats["paddle_error"] = stats.get("paddle_error") or "paddleocr 无有效块"
    stats["arbitration"] = arb_stats
    if _third_stats:
        stats["third_signal"] = {**_third_stats, "votes": dict(_third_stats["votes"])}

    # ---- P16 � 乱码兜底解析（百度通道对齐 + 骨架 PyMuPDF 兜底）----
    # char_conflicts 把无 ASCII 字母的 �(U+FFFD) 碎片当"纯符号"过滤 → 双通道抓不到；
    # � 恒为缺陷，用配对百度文本（首选）或本地骨架 PyMuPDF 文本对齐替换；解析不了进
    # qa_report（供 AI/人工兜底）。
    _cp = locals().get("content_pair") or {}
    _mdlocal = locals().get("md_of_local") or {}
    fffd_fixed = 0
    fffd_unresolved: list[str] = []
    for r in repair.paragraphs:
        if r.kind not in ("body", "caption") or "\ufffd" not in r.text:
            continue
        ref = _cp.get(r.para_id, "")
        if not ref or "\ufffd" in ref:
            # 百度无/也有 � → 本地骨架 PyMuPDF 文本兜底
            parts = []
            for mid in (r.md_idx or []):
                for lp in _mdlocal.get(mid, []):
                    parts.append(getattr(lp, "text", "") or "")
            ref = " ".join(parts)
        new_r, n = _resolve_replacement_chars(r.text, ref)
        if new_r != r.text:
            r.text = new_r
            fffd_fixed += n
        if "\ufffd" in r.text:
            fffd_unresolved.append(r.para_id)
    if fffd_fixed or fffd_unresolved:
        stats["fffd"] = {"fixed": fffd_fixed, "unresolved": fffd_unresolved}

    # ---- P16b 领域词典校正（共识错误：双通道同错 → diff 零报告，词典兜底）----
    # 只修"带显式电荷 + 词典唯一命中 + 缺数字下标"（BF−→BF₄⁻）；裸元素名绝不改。
    # 详见 core/consensus_fix.py（词典由 tools/build_domain_dict.py 生成，运行时零依赖）
    # 产出并入 review.json（GUI 复核区可见）：conf 0.8 review 候选（待确认）+
    # conf 0.95 自动修复（ai.applied 标记，audit 展示）
    dom_stats: dict = {"applied_paras": 0, "fixes": [],
                       "review_items": [], "auto_items": []}
    dom_boundary: dict[str, list[dict]] = {}   # 词典边界外候选（C 层 AI 扫描输入）
    # 段落 → 页码映射（md 段 idx → 本地行 page；供复核项 PDF 页图定位）
    dom_page: dict[str, int] = {}
    try:
        _mdlocal = _md_of_local(skeleton, align.line_map)
        for _r2 in repair.paragraphs:
            for _mid in (_r2.md_idx or []):
                for _lp in _mdlocal.get(_mid, []):
                    _pg = getattr(_lp, "page", 0) or 0
                    if _pg:
                        dom_page[_r2.para_id] = _pg
                        break
                if _r2.para_id in dom_page:
                    break
    except Exception:  # noqa: BLE001 - 页码反查失败不阻塞（复核项 page=1 兜底）
        pass
    for r in repair.paragraphs:
        if r.kind not in ("body", "caption"):
            continue
        orig = r.text
        new_text, fixes = apply_domain_fix(orig, kind=r.kind)
        if new_text != r.text:
            r.text = new_text
            dom_stats["applied_paras"] += 1
        if fixes:
            for f in fixes:
                f["para_id"] = r.para_id
            dom_stats["fixes"].extend(fixes)
            review_fs = [f for f in fixes if f.get("review")]
            auto_fs = [f for f in fixes if not f.get("review")]
            if review_fs:
                sugg = orig
                for f in review_fs:
                    if f["before"] and f["before"] in sugg:
                        sugg = sugg.replace(f["before"], f.get("after") or "")
                dom_stats["review_items"].append(
                    _build_domain_review_item(r, orig, sugg, review_fs, applied=False,
                                              page=dom_page.get(r.para_id, 1)))
            if auto_fs:
                dom_stats["auto_items"].append(
                    _build_domain_review_item(r, orig, new_text, auto_fs, applied=True,
                                              page=dom_page.get(r.para_id, 1)))
        # C 层输入：词典边界外候选（带电荷未命中，AI 发现新共识错误）
        cands = find_boundary_candidates(new_text)
        if cands:
            dom_boundary[r.para_id] = cands
    if dom_stats["fixes"]:
        stats["domain_fix"] = {"applied_paras": dom_stats["applied_paras"],
                               "fixes": dom_stats["fixes"]}
        (work / "domain_fix.json").write_text(
            json.dumps({"applied_paras": dom_stats["applied_paras"],
                        "fixes": dom_stats["fixes"]},
                       ensure_ascii=False, indent=1), encoding="utf-8")
    # ---- P16c C 层 AI 共识扫描（词典外；ai_review 开启时）→ review.json 待确认 ----
    if dom_boundary and ai_review:
        try:
            from paperparse.core.consensus_scan import ai_scan, build_scan_review_items
            flat = [dict(c, para_id=pid) for pid, cs in dom_boundary.items() for c in cs]
            scans = ai_scan(flat, provider=provider, paper=stem)
            scan_items = build_scan_review_items(repair.paragraphs, scans,
                                                 dom_boundary, page_map=dom_page)
            dom_stats["scan_items"] = scan_items
            stats["domain_scan"] = {"candidates": len(flat),
                                    "suggested": sum(1 for s in scans if s.get("valid"))}
        except Exception as e:  # noqa: BLE001 - AI 扫描失败不阻塞
            stats["domain_scan_error"] = str(e)[:120]
    # 词典层复核项并入 review.json（与 M8b 仲裁复核同文件同格式，GUI 复用；
    # 2026-08-26：归位 <out_dir>/work/，见 M8b 注释）
    dom_all_items = (dom_stats.get("scan_items") or []) + \
        dom_stats["review_items"] + dom_stats["auto_items"]
    if dom_all_items:
        rev_dir = out / "work"
        rev_dir.mkdir(parents=True, exist_ok=True)
        rp = rev_dir / "review.json"
        if rp.exists():
            review = json.loads(rp.read_text(encoding="utf-8"))
        else:
            review = {"count": 0, "items": [], "ai": {"source": "p14"}}
        base = len(review["items"])
        for i, it in enumerate(dom_all_items):
            it["report_idx"] = base + i
            review["items"].append(it)
        # ★2026-09-16：`count` 的语义 = **阻断项数**（门控翻译），不能等于全部 items——
        # 否则质量提示项（blocking=False）会被算成"待复核"，把每篇论文都卡住。
        review["total"] = len(review["items"])
        review["count"] = sum(1 for it in review["items"] if it.get("blocking") is not False)
        review["quality_count"] = sum(1 for it in review["items"]
                                      if it.get("blocking") is False)
        review["ai"]["domain"] = {"scan": len(dom_stats.get("scan_items") or []),
                                  "review": len(dom_stats["review_items"]),
                                  "auto": len(dom_stats["auto_items"])}
        rp.write_text(json.dumps(review, ensure_ascii=False, indent=1),
                      encoding="utf-8")
        stats["review_json"] = str(rp)
        stats["domain_review"] = {"scan": len(dom_stats.get("scan_items") or []),
                                  "review": len(dom_stats["review_items"]),
                                  "auto": len(dom_stats["auto_items"])}

    # ---- 组装 markdown + document.json ----
    # 2026-08-26：en.md 不保留参考文献表（用户决策——准确文献信息后续 bib 导入；
    # MinerU 拼接修复完整版 mineru_full.md 含参考文献留 library 备份）
    md_out = build_markdown(repair.paragraphs, figures, include_references=False)
    # 2026-08-26 用户决策：mineru_full.md = **MinerU 拼接修复完整版**（含参考文献，
    # build_markdown include_references=True）——双通道审核参照；解析重建目录后
    # 自动恢复，不再依赖外部 assemble
    try:
        mineru_full = build_markdown(repair.paragraphs, figures,
                                     include_references=True)
        (out / "mineru_full.md").write_text(mineru_full, encoding="utf-8")
    except OSError:  # noqa: BLE001 - 备份失败不阻塞解析
        pass
    # 保底改善（问题9）：前言第一段过短(<80词) → 与下一段拼接（导出前）
    md_out = _fallback_merge_intro_first(md_out)
    # P16 输出前公式自检（$配对/括号配对→修复安全项+记录到 qa_report）
    md_out, _formula_issues = _formula_self_check(md_out)
    stats["formula_self_check"] = _formula_issues
    en_md = out / "en.md"
    en_md.write_text(md_out, encoding="utf-8")
    # P15 Step5：document.json 落盘 ArticleDocument 兼容格式（load_document/
    # 翻译/导出/会话依赖）——由 to_article_document 转换 + save_document 写盘
    from paperparse.core.document_builder import save_document
    doc = to_article_document(repair.paragraphs, figures, md_text, pdf.name)
    _merge_intro_first_in_doc(doc)          # 保底改善与 en.md 一致
    doc_json = out / "document.json"
    save_document(doc, doc_json)
    # P16：清洗后自检（QA 检查，只报告不改）——正文孤立段/句尾/段首大写/图图注
    try:
        from paperparse.core.md_qa_check import check_markdown_qa
        qa = check_markdown_qa(md_out, doc_json=str(doc_json))
        qa["formula_self_check"] = _formula_issues
        # ★2026-09-16（用户："看不到的错误要可见"）：把双通道仲裁统计与复核清单规模写进
        # qa_report（此前 qa_report 只写不读、且不含仲裁信息 ⇒ 质量信号对界面不可见）。
        qa["arbitration"] = dict(arb_stats)
        if _third_stats:
            qa["third_signal"] = {**_third_stats, "votes": dict(_third_stats["votes"])}
        qa["review"] = {"blocking": int(arb_stats.get("pending_review") or 0),
                        "quality": int(stats.get("review_quality") or 0)}
        if stats.get("verify"):
            qa["verify"] = dict(stats["verify"])
        if dom_stats.get("fixes"):
            qa["domain_fix"] = dom_stats
        (out / "qa_report.json").write_text(
            json.dumps(qa, ensure_ascii=False, indent=1), encoding="utf-8")
        stats["qa"] = {"body": qa["stats"]["body"],
                       "issues": len(qa["issues"]),
                       "short": sum(1 for p in qa["paragraphs"] if p["short"]),
                       "no_terminal": sum(1 for p in qa["paragraphs"]
                                          if p["end_state"] not in ("ok", "formula")),
                       "lower_start": sum(1 for p in qa["paragraphs"]
                                          if not p["starts_upper"]),
                       "fig_match": qa["figures"]["match"]}
    except Exception as e:  # noqa: BLE001 - QA 失败不阻塞
        stats["qa_error"] = str(e)[:120]
    stats["duration_sec"] = round(time.time() - started, 2)

    return {
        "status": "success", "run_id": run_id or ("v2_" + time.strftime("%m%d%H%M%S")),
        "mode": "parse_only", "document_json": str(doc_json.resolve()),
        "en_md": str(en_md.resolve()), "export_dir": str(out.resolve()),
        "error": None, "note": "P14 管线完成（本地骨架 + mineru md + 双通道验证 + 仲裁）",
        "parse_source": "p14", "latex": bool(re.search(r"\$", md_text)),
        "paths": {"out_dir": str(out), "images": str(images_dir),
                  "verify": str(work / "verify.json"),
                  "char_conflicts": str(work / "char_conflicts.json"),
                  "qa_report": str(out / "qa_report.json")},
        "stats": stats,
        "warnings": [k for k in ("paddle_error", "figures_error")
                     if k in stats],
    }
