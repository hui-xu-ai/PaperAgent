#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: skill/src/paperparse/core/metadata.py
功能: S5 文献元数据提取（确定性规则，零 token）——行级适配：
      标题（首页 title 行候选，过滤出版商标签，按字号/长度择优）
      文章类型（Research Article 等标签行 → frontmatter type + tags）
      作者行（逗号分隔人名行）/ 机构行（meta 分类 + 大学/研究所关键字）
      摘要（首页正文行连续收集至首个编号标题）
      关键词（Keywords 行）/ DOI（正则 + 文件名回退）/ 年份（PDF 内嵌元数据）
      缺失项不抛错，由调度层记 PAPER-0102 警告
对外接口: extract_metadata
版本: v1.1.0 (2026-08-19)
版本历史:
  v1.1.0 行级重构适配：article_type/affiliations 提取、标题候选择优
  v1.0.0 初始版本（块级）
"""
from __future__ import annotations

import re
from pathlib import Path

from paperparse.core.block_classify import AFFILIATION_RE, is_author_line
from paperparse.middleware.schema import ArticleMetadata, TextBlock

__all__ = ["extract_metadata"]

DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
KEYWORDS_RE = re.compile(r"^keywords?\s*[:：]", re.IGNORECASE)
ABSTRACT_RE = re.compile(r"^abstract\b", re.IGNORECASE)
_HEADING_START_RE = re.compile(r"^\d+(\.\d+)*\.?\s+[A-Z]|^abstract$|^introduction$",
                               re.IGNORECASE)
# 出版商标签（RESEARCH ARTICLE / FULL PAPER / COMMUNICATION ...）
_ARTICLE_TYPE_RE = re.compile(
    r"^(research article|full paper|communication|letter|short communication|"
    r"review article|original article|perspective|review)$", re.IGNORECASE)
# 标题候选需排除的标签行
_TITLE_BOILER_RE = re.compile(
    r"^(research article|full paper|communication|letter|short communication|"
    r"review article|original article|www\.|doi[:：]|received[:：])", re.IGNORECASE)
_LIGATURES = str.maketrans({"\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl"})


def _clean_text(text: str) -> str:
    """[局部] 文本清理：连字归一化 + 空白折叠（frontmatter 用）"""
    return re.sub(r"\s+", " ", text.translate(_LIGATURES)).strip()


# ---------------- P-ENHANCE R06：骨架驱动元数据增强 ----------------

_HTML_RE = re.compile(r"<[^>]+>")
# 名字尾随上标数字/逗号/星号（"Ko1"、"Huh1,4"、"Koh2*"、"Koh2\\*"）剥离
_AUTHOR_SUFFIX_RE = re.compile(r"[\d,\s\*\\]+$")
# <sup>...</sup> 私有区占位（保真上标内容，避免 strip 后无法区分紧贴的字母上标）
_SUP_L, _SUP_R, _SUP_COMMA = "\ue000", "\ue001", "\ue002"
_SUP_BLOCK_RE = re.compile(r"\ue000[^\ue001]*\ue001")
# 重音/组合符号归一化（"Ce´dric"→"Cedric"；名字校验用）
_ACCENT_RE = re.compile(r"[\u0300-\u036f\u00b4\u00a8\u0060\u00af]")
# 紧贴名字尾部的字母上标（无 sup 标签时兜底，"Jho a" / "Wanga" 型）
_TRAIL_SUP_RE = re.compile(r"\s[a-z]\s*$")


def _norm_name(p: str) -> str:
    """[局部] 名字归一化：去重音/组合修饰符（校验与展示统一）"""
    return _ACCENT_RE.sub("", p).strip()


def _strip_html(text: str) -> str:
    """[局部] 去除 <sup> 等 HTML 标签"""
    return _HTML_RE.sub("", text or "")


def _extract_authors(text: str) -> list[str]:
    """[局部] 作者行提取（骨架版）：支持 <sup> 上标（数字/字母/混合/特殊符）。

    <sup> 内容占位为私有区块（块内逗号先替换，保证块不被 split 拆开）；
    独立上标块 part（"Li,<sup>#</sup> Suqian Ma"）剥块后余下为新名字。
    """
    from paperparse.core.block_classify import is_author_line
    t = re.sub(r"<sup>([^<]+)</sup>",
               lambda m: _SUP_L + m.group(1).replace(",", _SUP_COMMA) + _SUP_R,
               text or "")
    t = _HTML_RE.sub("", t).strip()
    if not t or len(t) > 600:
        return []
    # " & "（"X, Y & Z" 最后一组）按作者分隔
    t = re.sub(r"\s*&\s*", ", ", t)
    parts = [p.strip() for p in t.split(",") if p.strip(" *")]
    if len(parts) < 2:
        return []
    merged: list[str] = []
    for p in parts:
        if p.startswith(_SUP_L):
            # 上标块独立成 part（属于前名；"Li,<sup>#</sup> Suqian"）→ 剥块后余下为新名字
            rest = _SUP_BLOCK_RE.sub("", p).strip(" ,")
            if rest:
                merged.append(rest)
            continue
        if merged and re.fullmatch(r"[\da-z\s\*\\#]+", p):
            merged[-1] = merged[-1] + "," + p      # 无 sup 标签的碎片兜底
            continue
        merged.append(p)
    parts = merged
    if not 2 <= len(parts) <= 20:          # 合并后按作者数检查（上标碎片不占名额）
        return []
    cleaned = []
    for p in parts:
        p2 = re.sub(r"^\\\*", "", p)                      # 名前 "\*"（LaTeX 星号）
        p2 = _SUP_BLOCK_RE.sub("", p2)                    # 剥上标块
        p2 = _AUTHOR_SUFFIX_RE.sub("", p2).strip(" *")    # 剥数字尾（无块时兜底）
        p2 = _TRAIL_SUP_RE.sub("", p2)                    # 剥尾单字母上标（"Jho a" 兜底）
        p2 = _norm_name(p2)
        if p2.lower().startswith("and "):
            p2 = p2[4:].strip()
        if not p2:
            return []
        cleaned.append(p2)
    if not is_author_line(", ".join(cleaned)):
        return []
    return cleaned


def _parse_keywords(text: str) -> list[str]:
    """[局部] 关键词行解析（R10 双格式）：
    - 逗号分隔（Springer/Wiley）优先
    - 无逗号 → Elsevier 空格分隔（首字母大写片段切分："Ionic polymer sensor | Ionic liquid | ..."）
    截断清理："laser-" 类以 - 结尾短项删除。"""
    t = _strip_html(text).strip()
    if not t or t.lower().startswith(("received", "revised", "accepted", "published")):
        return []
    t = re.sub(r"^keywords?\s*[:：]?\s*", "", t, flags=re.IGNORECASE)   # 去 "Keywords:" 前缀
    kws = [k.strip() for k in re.split(r"[,;，；]", t) if k.strip()]
    if len(kws) < 2:
        # Elsevier：空格分隔、每关键词首字母大写（"Ionic polymer sensor Ionic liquid"）
        kws = [k.strip() for k in re.split(r"(?<=[a-z])\s+(?=[A-Z])", t) if k.strip()]
    kws = [k for k in kws if not (k.endswith("-") and len(k) < 12)]
    return kws


def enhance_with_skeleton(meta: ArticleMetadata, skeleton) -> None:
    """[全局] 布局骨架校正元数据（R06）：title/authors/keywords 以骨架（MinerU content_list）
    为准覆盖/补全——本地 pymupdf 行级提取在这些字段上不可靠（作者上标数字、关键词区混入
    参考文献、标题断行等）。

    参数:
        meta: ArticleMetadata（原地修改）
        skeleton: LayoutSkeleton（含 items 角色标注与 section_tree）
    """
    if not skeleton or not skeleton.items:
        return
    items = skeleton.items
    title_items = [i for i in items if i.role == "title"]

    # 1) title：骨架 title 角色（权威）
    if title_items:
        t = _clean_text(_strip_html(title_items[0].text))
        if len(t) > 20:
            meta.title = t

    # 2) authors：骨架首页 title 之后的 items 中第一条作者行
    if not meta.authors:
        base = items.index(title_items[0]) + 1 if title_items else 1
        for it in items[base: base + 10]:
            if it.page != 1 or it.role not in ("body", "text"):
                continue
            names = _extract_authors(it.text)
            if names:
                meta.authors = names
                break

    # 3) keywords：Keywords heading 节内非参考文献的逗号分隔行；
    #    R10：heading 文本自带关键词（Elsevier "Keywords: a b c"）也解析
    if not meta.keywords:
        idx_text = {i.index: i.text for i in items}
        for s in skeleton.section_tree:
            hlow = str(s.get("heading", "")).lower()
            if not hlow.startswith("keyword"):
                continue
            # ① heading 文本自身（"Keywords: Ionic polymer sensor ..."）
            kws = _parse_keywords(str(s.get("heading", "")))
            if kws:
                meta.keywords = kws
                return
            # ② 节内 items（非参考文献逗号行）
            for idx in s.get("items", []):
                t = idx_text.get(idx, "")
                if not t or t.strip().startswith("["):
                    continue          # 参考文献行（[26] ...）
                kws = _parse_keywords(t)
                if kws:
                    meta.keywords = kws
                    return
            break


def extract_metadata(blocks: list[TextBlock], source_pdf: str = "",
                     pdf_meta: dict | None = None) -> ArticleMetadata:
    """[全局] 提取元数据

    参数:
        blocks: 行级文本块（建议为全部块：kind 由 block_classify 赋值）
        source_pdf: 源 PDF 路径（DOI 文件名回退用）
        pdf_meta: pymupdf doc.metadata（可选，取年份）
    返回:
        ArticleMetadata（缺失字段留空/None，由调度层记警告）
    """
    ordered = sorted((b for b in blocks if b.order is not None), key=lambda b: b.order)
    front = [b for b in ordered if b.page == 1]

    meta = ArticleMetadata(source_pdf=str(source_pdf))

    # 1) 文章类型：首页标签行（RESEARCH ARTICLE 等）→ frontmatter type + tags
    for b in front:
        t = b.text.strip()
        if _ARTICLE_TYPE_RE.match(t):
            meta.article_type = t.title()
            break

    # 2) 标题：首页 title 行（过滤标签），连续多行按 y 相邻 + 字号连续合并为完整标题
    candidates = [b for b in front
                  if b.kind == "title" and not _TITLE_BOILER_RE.match(b.text.strip())]
    if candidates:
        rows = sorted(candidates, key=lambda b: (b.bbox[1], b.bbox[0]))
        parts = [rows[0].text.strip()]
        prev = rows[0]
        for b in rows[1:]:
            gap = b.bbox[1] - prev.bbox[3]
            if gap < 40 and (b.font_size or 0) >= (prev.font_size or 0) * 0.85:
                parts.append(b.text.strip())
                prev = b
            else:
                break
        meta.title = _clean_text(" ".join(parts))
    if not meta.title:
        for b in front:
            t = b.text.strip()
            if b.kind in ("heading", "body") and len(t) > 30 and b.bbox[1] < 200 \
                    and not _ARTICLE_TYPE_RE.match(t):
                meta.title = _clean_text(t)
                break

    # 3) 作者行与机构行：标题区（title 行）下方的 meta 行（作者可跨多行，按 y 排序收集）
    title_rows = [b for b in front if b.kind == "title"]
    title_bottom = max((b.bbox[3] for b in title_rows), default=0.0)
    author_parts: list[tuple[float, str]] = []
    for b in front:
        if b.bbox[1] < title_bottom - 5 or b.bbox[1] > 420:
            continue
        t = b.text.strip()
        if is_author_line(t):
            parts = [p.strip(" *") for p in t.split(",") if p.strip(" *")]
            author_parts.extend((b.bbox[1], p) for p in parts)
            continue
        if AFFILIATION_RE.search(t) and len(t) < 150 and t not in meta.authors:
            meta.affiliations.append(_clean_text(t))
    meta.authors = [p for _, p in sorted(author_parts, key=lambda x: x[0])]

    # 4) 摘要：A B S T R A C T 装饰标题（或作者区后首个长正文行）之后、
    #    首个编号标题之前的连续正文行
    if not meta.abstract:
        collecting = False
        abstract_lines: list[str] = []
        for b in front:
            t = b.text.strip()
            if not t:
                continue
            if b.kind in ("meta", "caption", "title"):
                continue
            if b.kind == "heading":
                # P12F：ABSTRACT 装饰标题 → 开始收集（Elsevier 摘要区在页面中部，
                # 旧 y<300 硬限制失效）；其他标题 → 结束
                if "abstract" in t.replace(" ", "").lower():
                    collecting = True
                    continue
                if collecting:
                    break
                continue
            if b.kind == "body":
                if meta.title and t == meta.title:
                    continue
                if collecting:
                    abstract_lines.append(t)
                elif len(t) > 50 and b.bbox[1] < 300:
                    collecting = True
                    abstract_lines.append(t)
        if abstract_lines:
            meta.abstract = " ".join(abstract_lines).strip()

    # 5) 关键词：全篇搜索 "Keywords:" 行或 Keywords 标题后的正文行（连字归一化）
    for b in ordered:
        t = b.text.strip()
        if KEYWORDS_RE.match(t):
            raw = re.split(r"[:：]", t, maxsplit=1)[1]
            meta.keywords = [_clean_text(k) for k in re.split(r"[,;，；]", raw) if _clean_text(k)]
            break
        if t.lower().rstrip(":").rstrip(".") == "keywords" and not meta.keywords:
            nxt = [x for x in ordered
                   if x.page == b.page and (x.order or 0) > (b.order or 0) and x.kind == "body"]
            if nxt:
                raw = nxt[0].text
                meta.keywords = [_clean_text(k) for k in re.split(r"[,;，；]", raw) if _clean_text(k)]
            break

    # 6) DOI：首页文本正则 + 文件名回退（10.1002_adma.202407106.pdf）
    if not meta.doi:
        front_text = " ".join(b.text for b in front)
        m = DOI_RE.search(front_text)
        if m:
            meta.doi = m.group(0)
    if not meta.doi and source_pdf:
        name = Path(source_pdf).stem
        if name.startswith("10."):
            meta.doi = name.replace("_", "/")

    # 7) 年份：PDF 内嵌元数据创建日期
    if pdf_meta:
        for key in ("creationDate", "modDate"):
            raw = pdf_meta.get(key) or ""
            m = re.search(r"(\d{4})", raw)
            if m and 1950 <= int(m.group(1)) <= 2100:
                meta.year = int(m.group(1))
                break

    return meta
