#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: paperparse/core/structure_profile.py
功能: P13-D1 文献结构画像（PDF 侧）：给 fused blocks 标文献结构角色——
      frontmatter: title/authors/affiliation/abstract/keywords/doi/publication
      body: heading/paragraph/equation/caption/figure/table/reference
      干扰: header/footer/footnote/page_number
      融合三信号（由强到弱）：
        1) MinerU content_list 骨架（layout_skeleton 角色 + 章节树）——权威结构标注
           （figure/equation/caption/table/reference/title 直接采信）；
        2) frontmatter 区（title 后 → 首个编号标题前）本地规则**覆盖**骨架的
           paragraph/heading 粗分类——作者行/机构行/Keywords/Abstract 标题/DOI
           （骨架对首页常见粗标：作者行→body、A R T I C L E I N F O→heading）；
        3) 通道 kind（header/footer/footnote 等）与本地兜底。
      Abstract 区序列填充：Abstract 标题块之后的连续 paragraph → abstract
      （到下一个 heading 止），解决骨架章节树只登记 heading 不识别摘要正文。
      输出 StructureProfile（每块 role + 证据）→ 落盘 structure_profile.json，
      供 D2 分区拼接 / D3 双通道段落边界对比 / D5 AI 仲裁上下文使用。
对外接口: build_structure_profile / StructureProfile
版本: v1.1.0 (2026-08-24)

映射说明（实测确认）：mineru_client._items_to_blocks 的 block_id "B%04d" 使用
content_list 原始下标 i（跳过条目不递增），layout_skeleton.SkeletonItem.index 也是
原 content_list 下标 → mineru 块可直接按 block_id 数值 ↔ skeleton item 映射。
paddleocr 补入块（Dxxxx）无 skeleton 对应 → 用通道 kind + 本地规则。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from paperparse.core.block_classify import is_author_line

__all__ = ["build_structure_profile", "StructureProfile", "FRONTMATTER_ROLES"]

# 骨架角色 → 文献结构角色（粗分已由 layout_skeleton 完成，此处细化）
_SKELETON_ROLE_MAP = {
    "title": "title",
    "heading": "heading",
    "equation": "equation",
    "caption": "caption",
    "figure": "figure",
    "table": "table",
    "reference": "reference",
    "body": "paragraph",
}
# 通道 kind → 文献结构角色
_KIND_ROLE_MAP = {
    "title": "title", "heading": "heading", "equation": "equation",
    "caption": "caption", "figure": "figure", "table": "table",
    "reference": "reference", "header": "header", "footer": "footer",
    "footnote": "footnote",
}
# 干扰类角色（不进正文拼接/复核重点；D11 用户决策 footer 不作精度要求）
NOISE_ROLES = frozenset({"header", "footer", "footnote", "page_number"})
# frontmatter 角色（首页区块，独立拼接区）
FRONTMATTER_ROLES = frozenset({"title", "authors", "affiliation", "abstract",
                               "keywords", "doi", "publication"})
# 骨架粗分类中**可被 frontmatter 本地规则覆盖**的角色（正文/标题——首页常粗标）
_OVERRIDABLE_SKELETON = frozenset({"paragraph", "heading"})

_AFFILIATION_RE = re.compile(
    r"\b(university|institute|school|college|laboratory|academy|centre|center)\b",
    re.IGNORECASE)
_DOI_LINE_RE = re.compile(r"^doi\s*[:：]", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_ORCID_RE = re.compile(r"orcid", re.IGNORECASE)
_KEYWORDS_RE = re.compile(r"^keywords?\s*[:：]", re.IGNORECASE)
_ABSTRACT_RE = re.compile(r"^abstract\b", re.IGNORECASE)
_PAGE_NUM_RE = re.compile(r"^\s*\d{1,3}\s*$")
# 编号标题（"1. Introduction"）
_NUM_HEAD_RE = re.compile(r"^\d+(\.\d+)*\.?\s+[A-Z]")
# 全大写刊头/标签
_MASKED_CAPS_RE = re.compile(r"^(?:[A-Z]{2,}(?:\s+[A-Z]){2,}|[A-Z](?:\s+[A-Z]){2,})")
# 装饰节标题（"A B S T R A C T" → 去空格小写匹配）
_DECOR_HEADINGS = frozenset({
    "abstract", "introduction", "conclusion", "resultsanddiscussion", "results",
    "discussion", "materialsandmethods", "references", "acknowledgements",
    "acknowledgment", "supplementarymaterials", "conflictofinterest",
    "dataavailability", "graphicalabstract", "highlights"})
# 版权行（© 20xx）→ publication
_COPYRIGHT_RE = re.compile(r"©\s*\d{4}|rights\s+are\s+reserved", re.IGNORECASE)
# Received/Available online 等出版时间线 → publication
_PUB_LINE_RE = re.compile(
    r"^(received|revised|accepted|published|available online|contents lists|"
    r"journal homepage)\b", re.IGNORECASE)


@dataclass
class ProfileItem:
    """[全局] 一块的结构画像条目"""
    block_id: str
    page: int
    role: str                 # 文献结构角色
    section: str = ""         # 所属章节（heading 文本）
    evidence: str = ""        # 信号来源（skeleton/kind/local/abstract_region）

    def to_dict(self) -> dict:
        return {"block_id": self.block_id, "page": self.page, "role": self.role,
                "section": self.section, "evidence": self.evidence}


@dataclass
class StructureProfile:
    """[全局] 结构画像结果"""
    items: list = field(default_factory=list)           # list[ProfileItem]
    frontmatter: dict = field(default_factory=dict)     # role → [block_id, ...]
    sections: list = field(default_factory=list)        # [{"heading", "blocks"}]
    stats: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"items": [i.to_dict() for i in self.items],
                "frontmatter": self.frontmatter,
                "sections": self.sections, "stats": self.stats}

    def role_of(self, block_id: str) -> str:
        """[全局] 查询某块角色（无则 "paragraph"）"""
        for it in self.items:
            if it.block_id == block_id:
                return it.role
        return "paragraph"


def _local_frontmatter_role(text: str) -> str:
    """[局部] frontmatter 区本地细分（作者/机构/关键词/摘要标题/DOI/出版信息）"""
    t = (text or "").strip()
    if not t:
        return ""
    if _PAGE_NUM_RE.match(t):
        return "page_number"
    if _COPYRIGHT_RE.search(t) and len(t) < 200:
        return "publication"
    if _PUB_LINE_RE.match(t) or _DOI_LINE_RE.match(t):
        return "publication" if not _DOI_LINE_RE.match(t) else "doi"
    if _EMAIL_RE.search(t) or _ORCID_RE.search(t):
        return "publication"
    if _KEYWORDS_RE.match(t):
        return "keywords"
    if is_author_line(t):
        return "authors"
    if _AFFILIATION_RE.search(t) and not t.rstrip().endswith((".", "!", "?")):
        return "affiliation"
    # 装饰节标题（"A B S T R A C T" / "A R T I C L E I N F O"）
    compact = re.sub(r"\s+", "", t).lower()
    if len(t) < 60 and re.fullmatch(r"(?:[A-Z]\s*){3,}", t.strip()):
        return "abstract" if compact == "abstract" else (
            "publication" if "articleinfo" in compact else "")
    if _ABSTRACT_RE.match(t) and len(t) < 40:
        return "abstract"
    return ""


def _local_other_role(block) -> str:
    """[局部] 非 frontmatter 区本地兜底（编号标题/全大写刊头）"""
    t = (block.text or "").strip()
    if _NUM_HEAD_RE.match(t) or (_MASKED_CAPS_RE.match(t) and len(t) < 60):
        return "heading"
    return ""


def _is_abstract_heading(role: str, text: str) -> bool:
    """[局部] 是否 Abstract 标题块（角色已标 abstract，或装饰/英文 heading 文本匹配）"""
    if role == "abstract":
        return True
    compact = re.sub(r"\s+", "", (text or "")).lower()
    return compact in ("abstract",) or bool(_ABSTRACT_RE.match(text or ""))


def build_structure_profile(blocks: list, skeleton=None) -> StructureProfile:
    """[全局] 主入口：fused blocks（+可选骨架）→ StructureProfile

    参数:
        blocks: fused TextBlock 列表（mineru 主 + paddleocr 补入）
        skeleton: LayoutSkeleton（可选；无则纯本地规则）
    返回:
        StructureProfile（items 按输入顺序；frontmatter/sections 汇总）
    """
    # 1) 骨架索引：content_list 下标 → SkeletonItem（含角色/章节）
    sk_by_index: dict[int, object] = {}
    section_of_index: dict[int, str] = {}
    if skeleton:
        for it in getattr(skeleton, "items", []) or []:
            sk_by_index[it.index] = it
        for sec in getattr(skeleton, "section_tree", []) or []:
            for idx in sec.get("items", []):
                section_of_index[idx] = sec.get("heading", "")

    # 2) 第一遍：骨架/kind/本地兜底 → 基础角色
    base: list[tuple] = []          # (block, role, evidence, section)
    for b in blocks:
        role, ev, sec = "", "", ""
        if b.source == "mineru" and b.block_id.startswith("B") and b.block_id[1:].isdigit():
            cidx = int(b.block_id[1:]) - 1
            skit = sk_by_index.get(cidx)
            if skit is not None:
                role = _SKELETON_ROLE_MAP.get(getattr(skit, "role", ""), "")
                ev = "skeleton:%s" % getattr(skit, "role", "")
                sec = section_of_index.get(cidx, "")
        if not role:
            role = _KIND_ROLE_MAP.get(b.kind, "")
            if role:
                ev = "kind:%s" % b.kind
        if not role:
            r = _local_frontmatter_role(b.text or "")
            if r in FRONTMATTER_ROLES or r in NOISE_ROLES:
                role, ev = r, "local"
            else:
                role = _local_other_role(b) or "paragraph"
                ev = "local"
        base.append((b, role, ev, sec))

    # 3) frontmatter 区判定：title 之后 → 首个编号标题（或非 frontmatter heading）之前
    #    无骨架/无 title 时：首页首个非空 body 块视为 title（对齐 block_classify 语义；
    #    不覆盖骨架已标的 figure/equation 等结构角色）
    if not any(r == "title" for _, r, _, _ in base):
        for i, (b, r, _, _) in enumerate(base):
            if b.page == 1 and r in ("paragraph", "body") and (b.text or "").strip():
                base[i] = (b, "title", "local_first_block", "")
                break
    fm_start = next((i for i, (b, r, _, _) in enumerate(base)
                     if r == "title" and b.page == 1), -1)
    fm_end = len(base)
    for i in range(fm_start + 1, len(base)):
        b, r, _, _ = base[i]
        if r == "heading" and (_NUM_HEAD_RE.match((b.text or "").strip())
                               or re.sub(r"\s+", "", (b.text or "").lower()) == "introduction"):
            fm_end = i
            break
    # frontmatter 区内：本地细分**覆盖**骨架的 paragraph/heading 粗分类
    if fm_start >= 0:
        for i in range(fm_start + 1, fm_end):
            b, r, ev, sec = base[i]
            if r not in _OVERRIDABLE_SKELETON:
                continue
            lr = _local_frontmatter_role(b.text or "")
            if lr in FRONTMATTER_ROLES:
                base[i] = (b, lr, "local_override", "")

    # 4) Abstract 区序列填充：Abstract 标题之后的连续 paragraph → abstract
    #    （骨架章节树只登记 heading，摘要正文常被留作 paragraph）
    abstract_mode = False
    for i, (b, r, ev, sec) in enumerate(base):
        if _is_abstract_heading(r, b.text or ""):
            abstract_mode = True
            base[i] = (b, "abstract", "abstract_heading", "")
            continue
        if abstract_mode:
            if r == "heading":
                abstract_mode = False
            elif r == "paragraph" and (b.text or "").strip():
                base[i] = (b, "abstract", "abstract_region", "")
            elif r in FRONTMATTER_ROLES:
                continue          # 摘要区内的关键词等 frontmatter 保持
            else:
                abstract_mode = False

    # 5) 汇总
    items: list[ProfileItem] = []
    frontmatter: dict[str, list] = {}
    sections: dict[str, list] = {}
    stats = {"total": len(blocks), "frontmatter": 0, "paragraph": 0,
             "structure": 0, "noise": 0, "other": 0}
    for b, r, ev, sec in base:
        if r in NOISE_ROLES:
            stats["noise"] += 1
        elif r in FRONTMATTER_ROLES:
            stats["frontmatter"] += 1
            frontmatter.setdefault(r, []).append(b.block_id)
        elif r in ("heading", "equation", "caption", "figure", "table", "reference"):
            stats["structure"] += 1
        elif r == "paragraph":
            stats["paragraph"] += 1
        else:
            stats["other"] += 1
        if r == "heading" and sec:
            sections.setdefault(sec, []).append(b.block_id)
        elif sec and r not in NOISE_ROLES and r not in FRONTMATTER_ROLES:
            sections.setdefault(sec, []).append(b.block_id)
        items.append(ProfileItem(block_id=b.block_id, page=b.page, role=r,
                                 section=sec, evidence=ev))

    fm: dict[str, list] = {r: frontmatter.get(r, []) for r in
                           ("title", "authors", "affiliation", "abstract",
                            "keywords", "doi", "publication")}
    sec_list = [{"heading": h, "blocks": blks} for h, blks in sections.items()]
    return StructureProfile(items=items, frontmatter=fm,
                            sections=sec_list, stats=stats)
