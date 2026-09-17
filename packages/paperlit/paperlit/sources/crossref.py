# -*- coding: utf-8 -*-
"""CrossRef API 客户端（DOI 元数据兜底源）。"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

_UA = "PaperLit/0.1 (literature search; mailto:user@example.com)"
_TIMEOUT = 20
_BASE = "https://api.crossref.org"
_TAG_RE = re.compile(r"<[^>]+>")


def _get_json(url: str, timeout: int = _TIMEOUT) -> dict | None:
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        logger.info("CrossRef %s → HTTP %s", url.split("?")[0], e.code)
    except Exception as e:
        logger.warning("CrossRef 请求失败 %s: %s", url.split("?")[0], e)
    return None


def _clean_abstract(raw: str) -> str:
    if not raw:
        return ""
    text = _TAG_RE.sub(" ", raw)
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"^Abstract\s*", "", text, flags=re.I)[:4000]


def _parse_work(m: dict, doi: str) -> dict:
    if not m:
        return {}
    date = (m.get("published-print") or m.get("published-online")
            or m.get("issued") or {}).get("date-parts") or [[None]]
    year = date[0][0] if date and date[0] else None

    return {
        "doi": doi,
        "title": (m.get("title") or [""])[0].strip(),
        "journal": (m.get("container-title") or [""])[0].strip(),
        "issn": (m.get("ISSN") or [""])[0],
        "eissn": (m.get("ISSN") or ["", ""])[1] if len(m.get("ISSN") or []) > 1 else "",
        "year": str(year or ""),
        "times_cited": int(m.get("is-referenced-by-count") or 0),
        "abstract": _clean_abstract(m.get("abstract") or ""),
        "authors": [
            f"{a.get('given', '')} {a.get('family', '')}".strip()
            for a in (m.get("author") or [])
            if a.get("family") or a.get("given")
        ],
        "type": m.get("type") or "",
        "source": "crossref",
    }


def fetch_one(doi: str) -> dict | None:
    """单篇 DOI 查询。"""
    doi = doi.strip()
    if not doi:
        return None
    encoded = urllib.parse.quote(doi)
    data = _get_json(f"{_BASE}/works/{encoded}")
    m = (data or {}).get("message") or {}
    result = _parse_work(m, doi)
    return result if result.get("title") else None


def search_works(query: str, rows: int = 20) -> list[dict]:
    """关键词搜索文献。"""
    params = urllib.parse.urlencode({
        "query": query,
        "rows": str(min(rows, 100)),
    })
    data = _get_json(f"{_BASE}/works?{params}")
    items = (data or {}).get("message", {}).get("items") or []
    results = []
    for item in items:
        doi = item.get("DOI", "")
        parsed = _parse_work(item, doi)
        if parsed.get("title"):
            results.append(parsed)
    return results
