#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文献元数据补全与渲染（L1/L3 编译提示词共用）。

背景（2026-09-12 用户反馈）：**L1 编译的元数据块缺研究单位、缺通信作者标注、缺关键词**。
根因三处：
  1. 提示词（`compile._prompt_l1`）只印 标题/期刊/年份/关键词/摘要 —— 作者与单位根本没印；
  2. `papers_meta` 的 `affiliations_json`/`keywords_json` 恒空：DOI 补全（`doi_meta`）只取
     Crossref 的题录字段，**从未取单位/关键词**（OpenAlex 有 `authorships[].raw_affiliation_strings`
     / `institutions` / `is_corresponding` / `keywords`，此前只当 Crossref 失败的兜底）；
  3. 解析产物 `document.json` 的 `metadata.affiliations`/`keywords` 也恒空（P14 只抽题录）。

本模块提供：
  - `guess_affiliations(doc)` / `guess_keywords(doc)` / `guess_corresponding(doc, authors)`：
    从**首页前部段落**做本地兜底（0 API 成本，覆盖无 DOI / API 无单位字段的文献）；
  - `meta_block(meta, doc)`：统一的元数据块文本（作者带 `*` 通信标注 + 研究单位 + 关键词），
    L1/L3 共用 ⇒ 口径一致、前缀字节稳定。
"""
from __future__ import annotations

import re

__all__ = ["guess_affiliations", "guess_keywords", "guess_corresponding", "meta_block"]

# 研究单位行：以 <sup>x</sup> / a) / 机构关键词开头
_SUP_HEAD_RE = re.compile(r"^\s*(?:<sup>[^<]{1,12}</sup>|\(?[a-hA-H]\)|[a-hA-H][\.\)])\s*\S")
_ORG_RE = re.compile(
    r"(universit|institut|laborator|academy|college|school|department|faculty|"
    r"center|centre|hospital|corporation|company|ltd|inc\b)", re.IGNORECASE)
# 作者行：含 2 个以上 <sup> 标记且以人名样式开头
_AUTHOR_SUP_RE = re.compile(r"<sup>[^<]{1,12}</sup>")
_KEYWORDS_HEAD_RE = re.compile(r"^\s*(?:<sup>[^<]*</sup>\s*)?key\s?words?\s*[:：]\s*(.+)$",
                               re.IGNORECASE | re.DOTALL)
_CORRESP_TEXT_RE = re.compile(
    r"(corresponding author|correspondence to|to whom correspondence|"
    r"\*+\s*e-?mail|e-?mail address)", re.IGNORECASE)
_MARK_RE = re.compile(r"\\?[\*\u2020\u2021\u00a7]")
# 人名形态（拒绝公式/乱码：`}$`、`\eta` 之类）
_NAME_RE = re.compile(r"^[A-Z][\w.'\-]*(?:\s+[A-Z][\w.'\-]*){0,3}$")
_BAD_CHARS_RE = re.compile(r"[${}\\]")


def guess_authors(doc, limit: int = 30) -> list[str]:
    """[全局] 本地兜底：首页"作者行"（含上标标记、逗号分隔的姓名串）"""
    for t in _front(doc, 8):
        if t.startswith("#") or not _AUTHOR_SUP_RE.search(t):
            continue
        if len(t) > 250 or ". " in t:      # 摘要/正文段不参与（作者行没有句号句）
            continue
        raw = re.sub(r"<sup>[^<]*</sup>", " ", t)
        raw = re.sub(r"[\d\*\u2020\u2021\u00a7]", " ", raw)
        raw = re.sub(r"\bet al\.?", " ", raw, flags=re.IGNORECASE)
        parts = [p.strip(" ,.;") for p in re.split(r"[,;，；]|\band\b", raw)]
        names = [p for p in parts if _NAME_RE.match(p) or
                 (2 <= len(p) <= 60 and p.count(" ") <= 4 and p[:1].isupper())]
        names = [n for n in names if len(n) >= 3 and not _ORG_RE.search(n)]
        if len(names) >= 2:
            out = []
            for n in names:
                if n not in out:
                    out.append(n)
            return out[:limit]
    return []


def _clean(s: str) -> str:
    """去标记/压缩空白（保留原文可读性）"""
    t = re.sub(r"<sup>[^<]*</sup>", " ", s or "")
    t = re.sub(r"<[^>]{1,12}>", " ", t)
    t = re.sub(r"\$\^\{[^}]*\}\$", " ", t)      # LaTeX 上标（cej 作者行的 $^{a,1}$）
    t = t.replace("\u00a0", " ")
    t = re.sub(r"\s+", " ", t).strip(" ,;.")
    return t.strip()


def _front(doc, n: int = 14) -> list[str]:
    """首页前部段落的纯文本（标题 + 作者 + 单位 + 关键词区通常在这里）"""
    out = []
    for p in (doc.paragraphs or [])[:n]:
        t = (p.text_en or "").strip()
        if t:
            out.append(t)
    return out


def guess_affiliations(doc, limit: int = 8) -> list[str]:
    """[全局] 本地兜底：首页前部"以单位标记开头 + 含机构词"的段落 = 研究单位"""
    out: list[str] = []
    for t in _front(doc):
        if t.startswith("#"):
            continue
        if not _SUP_HEAD_RE.match(t):
            continue
        body = _clean(t)
        if not body or not _ORG_RE.search(body):
            continue
        if len(body) > 400 or len(body) < 8:
            continue
        if body not in out:
            out.append(body)
        if len(out) >= limit:
            break
    return out


def guess_keywords(doc, limit: int = 15) -> list[str]:
    """[全局] 本地兜底：首页 "Keywords:" 行（分号/逗号分隔，无分隔符时按大写词切）"""
    for t in _front(doc, 18):
        m = _KEYWORDS_HEAD_RE.match(t)
        if not m:
            continue
        raw = m.group(1).strip()
        if not raw:
            continue
        parts = [x for x in re.split(r"[;；,，•·|/]", raw) if x.strip()]
        if len(parts) <= 1:
            # Elsevier 常把关键词用空格连排："Ionic polymer sensor Ionic liquid Heating time"
            parts = re.split(r"(?<=[a-z\)])\s+(?=[A-Z])", raw)
        kws = [_clean(x) for x in parts]
        kws = [k for k in kws if 2 <= len(k) <= 60]
        if kws:
            return kws[:limit]
    return []


def guess_corresponding(doc, authors: list[str] | None = None) -> list[str]:
    """[全局] 本地兜底：首页作者行的 `*`/`†` 标记 = 通信作者。

    识别两种形态：① 作者行内标记（"…, Dezhi Wu\\\\*"）；② 独立声明行
    （"Corresponding author: x@y" / "*Corresponding author"）→ 取其中的邮箱或整行。
    """
    authors = [a for a in (authors or []) if a]
    marked: list[str] = []

    def _ok_name(s: str) -> bool:
        s = s.strip()
        return bool(s) and not _BAD_CHARS_RE.search(s) and bool(_NAME_RE.match(s))

    for t in _front(doc, 12):
        if _CORRESP_TEXT_RE.search(t) and len(t) < 300:
            # 独立声明行：优先取邮箱；否则取整行文本
            mail = re.search(r"[\w.\-+]+@[\w.\-]+", t)
            marked.append(mail.group(0) if mail else _clean(t)[:120])
            continue
        # 作者行（含上标标记）里的 */† 标记；公式段（含 $ { } \）不参与。
        # 注意：先归一化"转义星号"（Wiley 的 `\*` 通信标记）再判坏字符，否则会被 \\ 误杀。
        probe = _MARK_RE.sub("*", t)
        if _BAD_CHARS_RE.search(re.sub(r"<sup>[^<]*</sup>", "", probe)):
            continue
        if _MARK_RE.search(probe) and (_AUTHOR_SUP_RE.search(t) or _MARK_RE.search(probe)):
            for seg in re.split(r"[,;，；]| and ", probe):
                if _MARK_RE.search(seg):
                    name = _clean(_MARK_RE.sub("", seg))
                    if _ok_name(name) and name not in marked:
                        marked.append(name)
    if not marked and authors:
        for a in authors:
            if _MARK_RE.search(a):
                name = _clean(_MARK_RE.sub("", a))
                if _ok_name(name) and name not in marked:
                    marked.append(name)
    return marked[:8]


def _fmt_list(items: list[str], sep: str = "；") -> str:
    return sep.join(str(x).strip() for x in items if str(x).strip())


def meta_block(meta, doc=None, abstract_chars: int = 4000) -> str:
    """[全局] 统一"元数据"块（L1/L3 提示词共用）。

    优先级：`meta`（papers_meta，bib/DOI 权威）→ `doc`（解析产物 metadata + 首页段落兜底）。
    作者带 `*` = 通信作者标注；研究单位逐条列出；关键词逗号分隔。
    """
    def _get(obj, name, default=None):
        if obj is None:
            return default
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default) or default

    title = _get(meta, "title", "") or _get(doc, "title", "")
    authors = list(_get(meta, "authors", []) or []) or list(_get(doc, "authors", []) or [])
    authors = [_clean(a) for a in authors if _clean(a)]
    if not authors and doc is not None:
        authors = guess_authors(doc)
    affils = list(_get(meta, "affiliations", []) or []) or list(_get(doc, "affiliations", []) or [])
    affils = [_clean(a) for a in affils if _clean(a)]
    keywords = list(_get(meta, "keywords", []) or []) or list(_get(doc, "keywords", []) or [])
    keywords = [_clean(k) for k in keywords if _clean(k)]
    corr = _get(meta, "corresponding", []) or []
    if doc is not None:
        if not affils:
            affils = guess_affiliations(doc)
        if not keywords:
            keywords = guess_keywords(doc)
        if not corr and not getattr(meta, "doi", ""):
            corr = guess_corresponding(doc, authors)
        elif not corr:
            corr = guess_corresponding(doc, authors)
    corr = [_clean(c) for c in corr if _clean(c)]

    # 作者行：通信作者加 * （先按全名匹配，退化到姓氏匹配）
    a_line = _fmt_list(authors, ", ")
    if authors and corr:
        marked = []
        for a in authors:
            hit = any(a == c or (c and (c in a or a in c)) for c in corr)
            marked.append(a + ("*" if hit else ""))
        a_line = _fmt_list(marked, ", ")
    lines = ["## 元数据", f"标题：{title}"]
    if a_line:
        lines.append("作者：" + a_line + ("（* 为通信作者）" if corr else ""))
    if corr:
        lines.append("通信作者：" + _fmt_list(corr, "；"))
    if affils:
        lines.append("研究单位：" + _fmt_list(affils, "；"))
    journal = _get(meta, "journal", "") or _get(doc, "journal", "")
    year = _get(meta, "year", "") or _get(doc, "year", "")
    if journal or year:
        lines.append(f"期刊：{journal} {year}".strip())
    if keywords:
        lines.append("关键词：" + _fmt_list(keywords, ", "))
    abstract = (_get(meta, "abstract", "") or _get(doc, "abstract", "") or "")[:abstract_chars]
    lines.append(f"摘要：{abstract}")
    return "\n".join(lines)
