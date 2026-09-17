# -*- coding: utf-8 -*-
"""OpenAlex API 客户端（批量 DOI 补全主力）。

OpenAlex 免费、覆盖 2.5 亿+ 文献，支持批量 filter 查询（每次最多 50 DOI）。
polite pool：请求头加 mailto 可达 10 req/s（无 mailto 仅 1 req/s）。
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

_UA = "PaperLit/0.1 (literature search; mailto:user@example.com)"
_TIMEOUT = 30
_BASE = "https://api.openalex.org"
_BATCH_SIZE = 50


def _get_json(url: str, timeout: int = _TIMEOUT) -> dict | None:
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        logger.info("OpenAlex %s → HTTP %s", url.split("?")[0], e.code)
    except Exception as e:
        logger.warning("OpenAlex 请求失败 %s: %s", url.split("?")[0], e)
    return None


def _reconstruct_abstract(inv: dict) -> str:
    """OpenAlex 倒排索引抽象 → 正常文本。"""
    if not inv:
        return ""
    pos: dict[int, str] = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))[:4000]


def _parse_work(data: dict) -> dict:
    """将 OpenAlex work 对象解析为统一字段字典。"""
    if not data or not data.get("title"):
        return {}

    src = ((data.get("primary_location") or {}).get("source") or {})
    authorships = data.get("authorships") or []

    affiliations: list[str] = []
    for a in authorships:
        for raw in (a.get("raw_affiliation_strings") or []):
            s = (raw or "").strip()
            if s and s not in affiliations:
                affiliations.append(s)
    joined = " || ".join(affiliations).lower()
    for a in authorships:
        for inst in (a.get("institutions") or []):
            name = (inst.get("display_name") or "").strip()
            if name and name.lower() not in joined and name not in affiliations:
                affiliations.append(name)

    corresponding: list[str] = []
    for a in authorships:
        if a.get("is_corresponding"):
            name = ((a.get("author") or {}).get("display_name") or "").strip()
            if name and name not in corresponding:
                corresponding.append(name)

    kws = [str(k.get("display_name") or "").strip()
           for k in (data.get("keywords") or []) if k.get("display_name")]

    concepts = [str(c.get("display_name") or "").strip()
                for c in (data.get("concepts") or [])
                if c.get("score", 0) >= 0.3 and c.get("display_name")]

    doi_raw = data.get("doi") or ""
    doi = re.sub(r"^https?://doi\.org/", "", doi_raw).strip()

    return {
        "doi": doi,
        "title": str(data.get("title") or "").strip(),
        "abstract": _reconstruct_abstract(data.get("abstract_inverted_index") or {}),
        "authors": [
            ((a.get("author") or {}).get("display_name") or "").strip()
            for a in authorships
            if (a.get("author") or {}).get("display_name")
        ],
        "affiliations": affiliations[:12],
        "corresponding": corresponding[:8],
        "journal": str(src.get("display_name") or "").strip(),
        "year": str(data.get("publication_year") or ""),
        "issn": str(src.get("issn_l") or ""),
        "times_cited": int(data.get("cited_by_count") or 0),
        "keywords": kws[:12],
        "research_areas": concepts[:10],
        "type": str(data.get("type") or ""),
        "openalex_id": str(data.get("id") or ""),
        "source": "openalex",
    }


def fetch_one(doi: str) -> dict | None:
    """单篇 DOI 查询。"""
    doi = doi.strip()
    if not doi:
        return None
    encoded = urllib.parse.quote(doi)
    data = _get_json(f"{_BASE}/works/doi:{encoded}")
    result = _parse_work(data or {})
    return result if result.get("title") else None


def fetch_batch(dois: list[str], rate_limit: float = 5.0) -> dict[str, dict]:
    """批量 DOI 查询（每次最多 50 个）。

    Args:
        dois: DOI 列表
        rate_limit: 每秒最大请求数

    Returns:
        {doi: parsed_dict} 映射（查不到的 DOI 不在结果中）
    """
    if not dois:
        return {}

    results: dict[str, dict] = {}
    clean_dois = [d.strip() for d in dois if d and d.strip()]

    for i in range(0, len(clean_dois), _BATCH_SIZE):
        batch = clean_dois[i:i + _BATCH_SIZE]
        doi_filter = "|".join(batch)
        params = urllib.parse.urlencode({
            "filter": f"doi:{doi_filter}",
            "per_page": str(len(batch)),
        })
        url = f"{_BASE}/works?{params}"

        data = _get_json(url)
        if data and "results" in data:
            for work in data["results"]:
                parsed = _parse_work(work)
                if parsed.get("doi"):
                    results[parsed["doi"].lower()] = parsed

        if i + _BATCH_SIZE < len(clean_dois):
            time.sleep(1.0 / rate_limit)

    logger.info("OpenAlex batch: %d queried, %d found", len(clean_dois), len(results))
    return results


def search_works(query: str, per_page: int = 25,
                 year_from: int | None = None) -> list[dict]:
    """关键词搜索文献（发现新文献用）。"""
    params: dict[str, str] = {
        "search": query,
        "per_page": str(min(per_page, 200)),
    }
    if year_from:
        params["filter"] = f"from_publication_year:{year_from}"

    url = f"{_BASE}/works?{urllib.parse.urlencode(params)}"
    data = _get_json(url)
    if not data or "results" not in data:
        return []

    results = []
    for work in data["results"]:
        parsed = _parse_work(work)
        if parsed.get("doi"):
            results.append(parsed)
    return results
