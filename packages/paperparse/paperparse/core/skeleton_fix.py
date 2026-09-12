#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: skill/src/paperparse/core/skeleton_fix.py
功能: 骨架驱动标题校正（P-ENHANCE R02 / S6 构建后 pass）：
      MinerU content_list（text_level>=2）是权威章节标题源；本地 pymupdf 标题常被
      误分类（全大写 "INTRODUCTION" 被判 meta 剔除 / 乱拼段被判 heading / 标题与
      正文粘连成段）。本 pass 以 LayoutSkeleton heading 序列为准，分三阶段：
      1) 降级：本地 is_heading 段与骨架 heading 无匹配 → 降级 body（防乱拼段/图注
         误作标题；adma 编号标题 "1. Introduction" 等与骨架匹配者保留）
      2) 校正/拆分：骨架 heading 匹配 document 段——整段/前缀短剩余 → is_heading；
         标题+正文粘连（前缀匹配但剩余长）→ 拆为 heading 段 + 正文段
      3) 插入：未匹配的骨架 heading 按锚点插入（锚 = 该节首个正文条目文本；找不到
         插 References 前/末尾）
对外接口: apply_skeleton_headings
版本: v0.2.0 (2026-08-22)
"""
from __future__ import annotations

import re
from typing import Any

__all__ = ["apply_skeleton_headings"]

_CAPTION_RE = re.compile(r"^(?:figure|fig\.?|table|scheme)\s*\d+", re.IGNORECASE)
_MAX_HEAD_TAIL = 30        # 标题段允许的"剩余"长度（超过视为标题+正文粘连，需拆分）


def _norm(s: str) -> str:
    """[局部] 归一化：小写 + 去非字母数字（容 OCR/空格差异）"""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _new_heading_para(doc: Any, text: str):
    """[局部] 构造 heading 段落（para_id 复用 P9xx 高位，避免与已有冲突）"""
    from paperparse.middleware.schema import Paragraph
    used = {p.para_id for p in doc.paragraphs}
    n = 900
    while "P%03d" % n in used:
        n += 1
    return Paragraph(para_id="P%03d" % n, order=0, section=text,
                     text_en=text, is_heading=True, confidence=0.9,
                     source_block_ids=[], coords={"pages": []})


def _is_caption_para(p: Any) -> bool:
    """[局部] 段落是否图注（is_caption 或文本以 Figure/Fig./Table 开头）"""
    if p.is_caption:
        return True
    return bool(_CAPTION_RE.match((p.text_en or "").strip()))


def _match_kind(pn: str, hn: str) -> str:
    """[局部] 段落文本与骨架 heading 的匹配类型：
    exact(整段) / head(前缀+短剩余) / glue(前缀+长剩余=粘连需拆分) / none"""
    if not hn:
        return "none"
    if pn == hn:
        return "exact"
    if pn.startswith(hn):
        tail = len(pn) - len(hn)
        return "head" if tail <= _MAX_HEAD_TAIL else "glue"
    return "none"


def apply_skeleton_headings(doc: Any, skeleton: Any, min_len: int = 3) -> int:
    """[全局] 用骨架 heading 序列校正 document 章节标题（三阶段）

    参数:
        doc: ArticleDocument（原地修改 paragraphs）
        skeleton: LayoutSkeleton（或含 section_tree/items 的对象）
    返回:
        修复数（降级 + 校正 + 拆分 + 插入合计）
    """
    if not skeleton or not skeleton.section_tree:
        return 0
    sk_heads = [s["heading"] for s in skeleton.section_tree]
    sk_norms = [_norm(h) for h in sk_heads]

    paras = doc.paragraphs
    fixed = 0

    # ---------- Phase 1: 降级本地误判 heading ----------
    for p in paras:
        if not p.is_heading:
            continue
        if p.para_id.startswith("P9"):      # 骨架插入段，跳过
            continue
        pn = _norm(p.text_en or "")
        if not pn:
            continue
        if not any(pn == hn or pn.startswith(hn) for hn in sk_norms):
            p.is_heading = False            # 非骨架权威标题 → 降级正文
            fixed += 1

    # ---------- Phase 2: 校正 / 拆分 ----------
    for hs, hn in zip(sk_heads, sk_norms):
        if not hn or len(hn) < min_len:
            continue
        for i, p in enumerate(paras):
            if p.is_heading and p.para_id.startswith("P9"):
                continue
            if _is_caption_para(p):
                continue                    # 图注保护
            pn = _norm(p.text_en or "")
            if not pn:
                continue
            kind = _match_kind(pn, hn)
            if kind == "exact" or kind == "head":
                if not p.is_heading:
                    p.is_heading = True
                    p.section = hs
                    fixed += 1
                break
            if kind == "glue":
                # 标题+正文粘连 → 拆段：heading 单独 + 剩余正文
                raw = p.text_en or ""
                cut = _find_cut(raw, hs)
                if cut <= 0:
                    if not p.is_heading:
                        p.is_heading = True
                        fixed += 1
                    break
                rest = raw[cut:].strip()
                p.text_en = hs
                p.is_heading = True
                p.section = hs
                np_ = _new_heading_para(doc, rest)   # para_id 自动唯一（P9xx 自增）
                np_.is_heading = False
                np_.section = hs
                np_.coords = dict(p.coords or {})
                paras.insert(i + 1, np_)
                fixed += 2
                break

    # ---------- Phase 3: 插入缺失 heading（相对锚点：插到骨架序中下一个已存在标题之前） ----------
    sk_norm_set = set(sk_norms)
    # 已存在锚段：校正/拆分后的 heading 段（非本轮插入的 P9xx）
    anchor_index: dict[str, int] = {}
    for i, p in enumerate(paras):
        if not p.is_heading:
            continue
        pn = _norm(p.text_en or "")
        if pn in sk_norm_set and pn not in anchor_index:
            anchor_index[pn] = i
    # References 兜底锚（文档末尾区域）
    ref_idx = next((i for i, p in enumerate(paras)
                    if p.is_heading and _norm(p.text_en or "").startswith("reference")),
                   len(paras))

    missing: list[tuple[int, int, str]] = []   # (骨架序, 插入位置, heading)
    for k, (hs, hn) in enumerate(zip(sk_heads, sk_norms)):
        if not hn or len(hn) < min_len:
            continue
        if hn in anchor_index:
            continue
        # 下一个已存在锚（骨架序 k 之后）
        pos = ref_idx
        for j in range(k + 1, len(sk_norms)):
            hj = sk_norms[j]
            if hj in anchor_index:
                pos = anchor_index[hj]
                break
        missing.append((k, pos, hs))

    # 从后往前插入：同位置按骨架序（先插序大者，保持序小者在后）
    for k, pos, hs in sorted(missing, key=lambda x: (x[1], x[0]), reverse=True):
        paras.insert(pos, _new_heading_para(doc, hs))
        fixed += 1

    # ---------- Phase 4（R10）：前部区块重排 ----------
    # 本地 stitch 阅读序可能把 front matter heading（Abstract/ARTICLE INFO/Keywords）排到
    # 正文后（cej 实测 ABSTRACT 在 1. Introduction 后）→ 移到首个编号标题前
    _reorder_front_headings(paras, sk_heads)

    # order 重排（渲染按列表顺序）
    for i, p in enumerate(paras):
        p.order = i
    return fixed


_FRONT_KEYS = ("abstract", "article info", "keywords", "highlights",
               "graphical abstract")


def _reorder_front_headings(paras: list, sk_heads: list[str]) -> int:
    """[局部] 前部区块重排：Abstract/ARTICLE INFO/Keywords 等 front heading 若位于
    首个编号标题（1. Introduction 等）之后 → 移到其前（R10 ABSTRACT 错位修复）"""
    first_num = next(
        (i for i, p in enumerate(paras)
         if p.is_heading and re.match(r"^\d", _norm(p.text_en or ""))),
        len(paras))
    if first_num >= len(paras):
        return 0
    moved = 0
    for hs in sk_heads:
        hlow = _norm(hs)
        if not any(hlow.startswith(_norm(k)) or _norm(k).startswith(hlow)
                   for k in _FRONT_KEYS):
            continue
        idx = next((i for i, p in enumerate(paras)
                    if p.is_heading and _norm(p.text_en or "") == hlow), None)
        if idx is not None and idx > first_num:
            paras.insert(first_num, paras.pop(idx))
            moved += 1
    return moved


def _find_cut(raw: str, heading: str) -> int:
    """[局部] 在粘连段中找到 heading 原文结束位置（前缀匹配；回退归一化长度）"""
    h = heading.strip()
    if raw.startswith(h):
        return len(h)
    # 容空格/大小写差异：在 raw 中找归一化后的 heading 位置
    rn = _norm(raw)
    hn = _norm(h)
    pos = rn.find(hn)
    if pos < 0:
        return -1
    # 映射回 raw 下标（按归一化字符计数近似：逐字符对齐太复杂，用比例）
    if not rn:
        return -1
    return int(pos * len(raw) / max(1, len(rn))) if len(rn) > 0 else -1
