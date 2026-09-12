# -*- coding: utf-8 -*-
"""WOS 导出 bib 轻量解析器（零外部依赖）。

支持 Web of Science 导出格式：@article{WOS:xxxx, Field = {value}, ...}
- 嵌套花括号（值内含 {} 如 {[}52105299{]}）、多行值、值内逗号
- 转义（反斜杠 + 字符，如 \\& \\%）保留原样（WOS 导出自带）
不依赖 bibtexparser：WOS 字段（Cited-References 长串/Affiliation）需特判，
自写解析器更可控。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .doi import extract_doi_from_text, normalize_doi
from .models import CitedRef, PaperMeta


@dataclass
class RawRecord:
    """原始 bib 记录（解析中间态）。"""
    key: str
    fields: dict = field(default_factory=dict)


def parse_bib(text: str) -> list[RawRecord]:
    """解析 bib 文本 → RawRecord 列表。容错：跳过损坏块（不抛错整批失败）。"""
    records: list[RawRecord] = []
    i, n = 0, len(text)
    while i < n:
        at = text.find("@", i)
        if at < 0:
            break
        m = re.match(r"@(\w+)\s*\{\s*([^,]*?),", text[at:])
        if not m:
            i = at + 1
            continue
        etype, key = m.group(1).lower(), m.group(2).strip()
        pos = at + m.end()
        fields: dict[str, str] = {}
        while pos < n:
            # 找字段名 = {
            fm = re.match(r"\s*([A-Za-z][\w\-]*)\s*=\s*\{", text[pos:])
            if not fm:
                # 跳过空白与逗号
                nxt = text.find(",", pos)
                if nxt < 0:
                    break
                pos = nxt + 1
                if re.match(r"\s*\}?\s*$", text[pos:]):
                    break
                continue
            fname = fm.group(1).lower()
            vstart = pos + fm.end()
            value, vend = _read_braced(text, vstart)
            if vend < 0:
                break
            fields[fname] = value
            pos = vend
            # 字段结束后：逗号 → 下一字段；} → 记录结束
            nxt = text.find(",", pos)
            if nxt < 0:
                break
            after = text[pos:nxt]
            if "}" in after:          # 记录闭合（} 在逗号前）
                break
            pos = nxt + 1
            if text[pos:].lstrip().startswith("}"):
                break
        records.append(RawRecord(key=key, fields=fields))
        # 找记录结尾的 "}"（独立行）
        endm = re.search(r"\n\s*\}", text[pos:])
        if endm:
            i = pos + endm.end()
        else:
            i = n
    return records


def _read_braced(text: str, start: int) -> tuple[str, int]:
    """从 start（{ 之后的第一个字符）读 {value}，处理嵌套花括号与转义。

    depth 初始 1（start 已在 { 之后）；返回 (value, 结束 } 的后一位置)。
    """
    depth = 1
    out: list[str] = []
    i = start
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            out.append(text[i:i + 2])   # 转义保留（\& \{ 等）
            i += 2
            continue
        if c == "{":
            depth += 1
            if depth > 1:
                out.append(c)
            i += 1
            continue
        if c == "}":
            depth -= 1
            if depth == 0:
                return "".join(out), i + 1
            out.append(c)
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out), -1


# ---------------------------------------------------------------- 字段映射

def _split_list(value: str) -> list[str]:
    """WOS 列表字段（关键词等，以 ; 或换行分隔）。"""
    items = re.split(r"[;\n]+", value)
    return [x.strip() for x in items if x.strip()]


def _split_authors(value: str) -> list[str]:
    """WOS Author 字段：以 ' and ' 连接（作者名含逗号，不能按逗号拆）。"""
    items = re.split(r"\s+and\s+", value, flags=re.I)
    return [x.strip().rstrip(",") for x in items if x.strip()]


def _split_affiliations(value: str) -> list[str]:
    """Affiliation 字段（换行分隔的机构条目）。"""
    lines = [ln.strip() for ln in value.splitlines() if ln.strip()]
    return lines


def _parse_cited_refs(value: str) -> list[CitedRef]:
    """Cited-References 字段：每行 'Author Year JOURNAL, V卷, DOI x' → CitedRef。"""
    refs: list[CitedRef] = []
    for line in value.splitlines():
        line = line.strip()
        if not line:
            continue
        doi = extract_doi_from_text(line)
        brief = line
        # 尽力拆分 author/journal/year（WOS 格式：Author YYYY, JOURNAL[, V卷], [DOI x]）
        m = re.match(r"^(.*?)\s(\d{4}),\s*(.*)$", line)
        author, year, rest = ("", "", line)
        if m:
            author = m.group(1).strip()
            year = m.group(2)
            rest = m.group(3).strip()
        # rest 形如 "JOURNAL[, V9][, DOI 10.xxx]"
        journal = rest
        volume = ""
        dm = re.search(r",\s*V([\d\w\-\.]+)\s*$", rest)
        if dm:
            volume = dm.group(1)
            journal = rest[: dm.start()].rstrip(",").strip()
        refs.append(CitedRef(author=author, journal=journal, year=year,
                             volume=volume, doi=normalize_doi(doi), raw=brief))
    return refs


def raw_to_meta(rec: RawRecord) -> PaperMeta:
    """RawRecord → PaperMeta（WOS 字段名映射）。"""
    f = rec.fields
    return PaperMeta(
        doi=normalize_doi(f.get("doi", "")),
        title=(f.get("title") or "").strip(),
        abstract=(f.get("abstract") or "").strip(),
        authors=_split_authors(f.get("author", "")),
        affiliations=_split_affiliations(f.get("affiliation", "")),
        journal=(f.get("journal") or "").strip(),
        year=(f.get("year") or "").strip(),
        month=(f.get("month") or "").strip(),
        issn=(f.get("issn") or "").strip(),
        eissn=(f.get("eissn") or "").strip(),
        keywords=_split_list(f.get("keywords", "")),
        research_areas=_split_list(f.get("research-areas", "")),
        wos_categories=_split_list(f.get("web-of-science-categories", "")),
        funding=(f.get("funding-text") or f.get("funding-acknowledgement") or "").strip(),
        times_cited=_int(f.get("times-cited", "0")),
        wos_id=(f.get("unique-id") or "").strip(),
        references=_parse_cited_refs(f.get("cited-references", "")),
        source_file=f.get("_source_file", ""),
    )


def _int(value: str) -> int:
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return 0


def parse_bib_file(path: str | object, source_file: str = "") -> list[PaperMeta]:
    """解析 bib 文件 → PaperMeta 列表（source_file 记录来源）。"""
    from pathlib import Path

    p = Path(path)
    text = p.read_text(encoding="utf-8", errors="replace")
    recs = parse_bib(text)
    metas: list[PaperMeta] = []
    for rec in recs:
        if rec.key.lower().startswith("comment"):
            continue
        meta = raw_to_meta(rec)
        meta.source_file = source_file or p.name
        metas.append(meta)
    return metas
