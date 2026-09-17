# -*- coding: utf-8 -*-
"""WoS bib 文件解析器（零依赖，字符级解析）。

基于 paperkb.bib_parser 的模式，适配 paperlit 的 Paper 模型。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from ..models import CitedRef, Paper

logger = logging.getLogger(__name__)


def parse_bib(text: str) -> list[dict]:
    """解析 BibTeX 文本，返回原始字段字典列表。"""
    records = []
    pos = 0
    while pos < len(text):
        at_idx = text.find("@", pos)
        if at_idx == -1:
            break
        m = re.match(r"@(\w+)\s*\{\s*([^,]*?),", text[at_idx:])
        if not m:
            pos = at_idx + 1
            continue
        entry_type = m.group(1).lower()
        cite_key = m.group(2).strip()
        if entry_type == "comment":
            pos = at_idx + m.end()
            continue

        brace_start = at_idx + m.end() - 1
        fields = _read_fields(text, brace_start)
        fields["_type"] = entry_type
        fields["_key"] = cite_key
        records.append(fields)
        pos = brace_start + 1
        depth = 0
        while pos < len(text):
            if text[pos] == "{":
                depth += 1
            elif text[pos] == "}":
                if depth == 0:
                    pos += 1
                    break
                depth -= 1
            pos += 1
    return records


def _read_fields(text: str, start: int) -> dict:
    """从 { 开始读取所有 field = {value} 对。"""
    fields = {}
    pos = start + 1
    while pos < len(text):
        fm = re.match(r"\s*(\w[\w-]*)\s*=\s*", text[pos:])
        if not fm:
            if text[pos] == "}":
                break
            pos += 1
            continue
        field_name = fm.group(1).lower()
        val_start = pos + fm.end()
        if val_start >= len(text):
            break
        if text[val_start] == "{":
            value, end_pos = _read_braced(text, val_start)
            fields[field_name] = value
            pos = end_pos + 1
        elif text[val_start] == '"':
            end_quote = text.find('"', val_start + 1)
            if end_quote == -1:
                pos = val_start + 1
                continue
            fields[field_name] = text[val_start + 1:end_quote]
            pos = end_quote + 1
        else:
            em = re.match(r"([^,}\s]+)", text[val_start:])
            if em:
                fields[field_name] = em.group(1)
                pos = val_start + em.end()
            else:
                pos = val_start + 1
    return fields


def _read_braced(text: str, start: int) -> tuple[str, int]:
    """读取 {…} 内容，处理嵌套花括号。返回 (内容, 结束位置)。"""
    depth = 0
    pos = start
    while pos < len(text):
        if text[pos] == "{":
            depth += 1
        elif text[pos] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:pos], pos
        pos += 1
    return text[start + 1:], len(text) - 1


def _clean_doi(raw: str) -> str:
    """清理 WoS bib 中的 DOI 格式（去除 `{[}...].` 包装、取第一个 DOI）。"""
    if not raw:
        return ""
    # 去除 WoS 特殊包装
    raw = raw.replace("{[}", "").replace("]}", "").replace("{", "").replace("}", "")
    # 取第一个 DOI（WoS 有时会重复列出）
    m = re.search(r"10\.\d{4,}/[^\s,;]+", raw)
    if m:
        return m.group(0).rstrip(".")
    return raw.strip().rstrip(".")


def _parse_cited_refs(value: str) -> list[CitedRef]:
    """解析 Cited-References 字段。"""
    refs = []
    for line in value.split("\n"):
        line = line.strip()
        if not line:
            continue
        ref = CitedRef(raw=line)
        m = re.match(
            r"^(.+?),\s*(\d{4})\s*,\s*(.+?)(?:\s*,\s*V(\w+))?"
            r"(?:\s*,\s*(?:P|p)\s*\d+)?\s*"
            r"(?:,\s*DOI\s+(.+))?$",
            line, re.IGNORECASE
        )
        if m:
            ref.author = m.group(1).strip()
            ref.year = m.group(2).strip()
            ref.journal = m.group(3).strip()
            ref.volume = m.group(4) or ""
            ref.doi = _clean_doi(m.group(5) or "")
        else:
            doi_m = re.search(r"10\.\d{4,}/[^\s,;]+", line)
            if doi_m:
                ref.doi = doi_m.group(0).rstrip(".")
        refs.append(ref)
    return refs


def raw_to_paper(fields: dict, source_file: str = "",
                 journal_mapper=None) -> Paper:
    """将原始 bib 字段映射为 Paper 模型。

    Args:
        fields: bib 字段字典
        source_file: 来源文件名
        journal_mapper: 期刊映射器（可选，用于自动补全期刊全称）
    """
    doi = _clean_doi(fields.get("doi", ""))
    title = fields.get("title", "").strip()
    abstract = fields.get("abstract", "").strip()

    authors_raw = fields.get("author", "")
    authors = [a.strip() for a in authors_raw.split(" and ") if a.strip()]

    affiliations_raw = fields.get("affiliation", "")
    affiliations = [a.strip() for a in affiliations_raw.split("\n")
                    if a.strip()]

    keywords_raw = fields.get("keywords", "")
    keywords = _split_list(keywords_raw)

    research_areas = _split_list(fields.get("research-areas", ""))
    wos_categories = _split_list(fields.get("web-of-science-categories", ""))

    cited_refs_raw = fields.get("cited-references", "")
    references = _parse_cited_refs(cited_refs_raw) if cited_refs_raw else []

    times_cited = 0
    tc_raw = fields.get("times-cited", "0")
    try:
        times_cited = int(tc_raw)
    except (ValueError, TypeError):
        pass

    # 期刊名规范化（如果有映射器）
    journal_raw = fields.get("journal", "").strip()
    journal = journal_mapper.normalize(journal_raw) if journal_mapper else journal_raw

    return Paper(
        doi=doi,
        title=title,
        abstract=abstract,
        authors=authors,
        affiliations=affiliations,
        journal=journal,
        year=fields.get("year", fields.get("pubyear", "")).strip(),
        issn=fields.get("issn", "").strip(),
        keywords=keywords,
        research_areas=research_areas,
        wos_categories=wos_categories,
        times_cited=times_cited,
        wos_id=fields.get("unique-id", "").strip(),
        references=references,
        source_main="wos",
        source_file=source_file,
    )


def _split_list(value: str) -> list[str]:
    if not value:
        return []
    if ";" in value:
        return [x.strip() for x in value.split(";") if x.strip()]
    return [x.strip() for x in value.split("\n") if x.strip()]


def parse_bib_file(path: str | Path, source_file: str = "",
                   journal_mapper=None) -> list[Paper]:
    """解析单个 bib 文件，返回 Paper 列表。

    Args:
        path: bib 文件路径
        source_file: 来源文件名
        journal_mapper: 期刊映射器（可选，用于自动补全期刊全称）
    """
    path = Path(path)
    if not source_file:
        source_file = path.name
    text = path.read_text(encoding="utf-8", errors="replace")
    raw_records = parse_bib(text)
    papers = []
    for rec in raw_records:
        if rec.get("_type") == "comment":
            continue
        paper = raw_to_paper(rec, source_file=source_file,
                             journal_mapper=journal_mapper)
        if paper.doi or paper.title:
            papers.append(paper)
    logger.info("parsed %s: %d records -> %d valid papers",
                source_file, len(raw_records), len(papers))
    return papers


def ingest_papers(papers: list[Paper], store: LitStore,
                  source_file: str = "", import_refs: bool = True) -> dict:
    """将去重后的文献列表入库，同时建立引用关系。

    对于参考文献中 DOI 不在库内的，创建占位记录（is_reference=True）。

    Args:
        import_refs: 是否导入参考文献（默认导入）

    Returns:
        {"new_papers": int, "new_refs": int, "new_citations": int}
    """
    from ..models import Citation

    new_papers = 0
    new_refs = 0
    new_citations = 0

    for paper in papers:
        if not paper.doi:
            continue
        is_new = store.upsert_paper(paper)
        if is_new:
            new_papers += 1

        if import_refs:
            for ref in paper.references:
                if not ref.doi:
                    continue
                cit = Citation(
                    citing_doi=paper.doi,
                    cited_doi=ref.doi,
                    source="wos",
                    cited_brief=ref.raw[:200],
                )
                if store.upsert_citation(cit):
                    new_citations += 1

                if not store.paper_exists(ref.doi):
                    stub = Paper(
                        doi=ref.doi,
                        title="",
                        is_reference=True,
                        source_main="wos_ref",
                        source_file=source_file,
                    )
                    if ref.author:
                        stub.authors = [ref.author]
                    if ref.year:
                        stub.year = ref.year
                    if ref.journal:
                        stub.journal = ref.journal
                    store.upsert_paper(stub)
                    new_refs += 1
        else:
            # 不导入参考文献，但仍记录引用关系（用于图谱）
            for ref in paper.references:
                if not ref.doi:
                    continue
                cit = Citation(
                    citing_doi=paper.doi,
                    cited_doi=ref.doi,
                    source="wos",
                    cited_brief=ref.raw[:200],
                )
                if store.upsert_citation(cit):
                    new_citations += 1

    logger.info("ingest: %d new papers, %d new refs, %d new citations",
                new_papers, new_refs, new_citations)
    return {
        "new_papers": new_papers,
        "new_refs": new_refs,
        "new_citations": new_citations,
    }
