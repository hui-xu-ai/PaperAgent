# -*- coding: utf-8 -*-
"""Semantic Scholar API 客户端（补充 TL;DR、h-index、引用上下文）。

免费 API：无 key 1 req/s，有 key 100 req/s。批量查询每次最多 500 篇。
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

_UA = "PaperLit/0.1"
_TIMEOUT = 20
_BASE = "https://api.semanticscholar.org/graph/v1"
_BATCH_SIZE = 50
_FIELDS = "paperId,externalIds,title,abstract,year,citationCount," \
          "authors,authors.name,authors.affiliations,authors.hIndex," \
          "venue,journal,openAccessPdf,tldr"


def _get_json(url: str, timeout: int = _TIMEOUT,
              api_key: str = "") -> dict | None:
    headers = {"User-Agent": _UA, "Accept": "application/json"}
    if api_key:
        headers["x-api-key"] = api_key
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            logger.warning("Semantic Scholar 速率限制")
        else:
            logger.info("SS %s → HTTP %s", url.split("?")[0], e.code)
    except Exception as e:
        logger.warning("SS 请求失败 %s: %s", url.split("?")[0], e)
    return None


def _post_json(url: str, body: dict, timeout: int = _TIMEOUT,
               api_key: str = "") -> dict | None:
    headers = {
        "User-Agent": _UA,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if api_key:
        headers["x-api-key"] = api_key
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            logger.warning("SS 速率限制（POST）")
        else:
            logger.info("SS POST %s → HTTP %s", url.split("?")[0], e.code)
    except Exception as e:
        logger.warning("SS POST 失败 %s: %s", url.split("?")[0], e)
    return None


def _parse_paper(data: dict) -> dict:
    if not data or not data.get("title"):
        return {}
    ext = data.get("externalIds") or {}
    authors = []
    affiliations: list[str] = []
    for a in (data.get("authors") or []):
        name = (a.get("name") or "").strip()
        if name:
            authors.append(name)
        for aff in (a.get("affiliations") or []):
            if aff and aff not in affiliations:
                affiliations.append(aff)

    tldr_data = data.get("tldr") or {}
    tldr = tldr_data.get("text", "") if isinstance(tldr_data, dict) else ""

    journal = data.get("venue") or ""
    j = data.get("journal") or {}
    if j.get("name"):
        journal = j["name"]

    return {
        "doi": ext.get("DOI", ""),
        "title": str(data.get("title") or "").strip(),
        "abstract": str(data.get("abstract") or "")[:4000],
        "tldr": tldr,
        "authors": authors,
        "affiliations": affiliations[:12],
        "journal": str(journal).strip(),
        "year": str(data.get("year") or ""),
        "times_cited": int(data.get("citationCount") or 0),
        "source": "semantic_scholar",
        "ss_id": str(data.get("paperId") or ""),
    }


def fetch_one(doi: str, api_key: str = "") -> dict | None:
    """单篇 DOI 查询。"""
    doi = doi.strip()
    if not doi:
        return None
    encoded = urllib.parse.quote(doi)
    url = f"{_BASE}/paper/DOI:{encoded}?fields={_FIELDS}"
    data = _get_json(url, api_key=api_key)
    result = _parse_paper(data or {})
    return result if result.get("title") else None


def fetch_batch(dois: list[str], api_key: str = "",
                rate_limit: float = 1.0) -> dict[str, dict]:
    """批量 DOI 查询（每次最多 500 个）。"""
    if not dois:
        return {}

    results: dict[str, dict] = {}
    clean_dois = [d.strip() for d in dois if d and d.strip()]

    for i in range(0, len(clean_dois), _BATCH_SIZE):
        batch = clean_dois[i:i + _BATCH_SIZE]
        body = {"ids": [f"DOI:{d}" for d in batch]}
        url = f"{_BASE}/paper/batch?fields={_FIELDS}"

        data = _post_json(url, body, api_key=api_key)
        if data and isinstance(data, list):
            for item in data:
                parsed = _parse_paper(item or {})
                if parsed.get("doi"):
                    results[parsed["doi"].lower()] = parsed

        if i + _BATCH_SIZE < len(clean_dois):
            time.sleep(1.0 / rate_limit)

    logger.info("SS batch: %d queried, %d found", len(clean_dois), len(results))
    return results


def search_papers(query: str, limit: int = 20,
                  api_key: str = "") -> list[dict]:
    """关键词搜索文献。"""
    params = urllib.parse.urlencode({
        "query": query,
        "limit": str(min(limit, 100)),
        "fields": _FIELDS,
    })
    url = f"{_BASE}/paper/search?{params}"
    data = _get_json(url, api_key=api_key)
    if not data or "data" not in data:
        return []
    results = []
    for item in data["data"]:
        parsed = _parse_paper(item or {})
        if parsed.get("doi"):
            results.append(parsed)
    return results
