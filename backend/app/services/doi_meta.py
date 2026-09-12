# -*- coding: utf-8 -*-
"""DOI → 元数据自动补全（2026-09-12）。

背景（用户提问）："有没有能根据文献内解析的 doi 号，临时补全元数据的方法？"
答案：有，且**免费无需 Key**。实测（`tools/dbg_doi_meta_probe.py`）三个源都能拿到
journal / ISSN / year / 被引次数 / 摘要：
- Crossref（`api.crossref.org`，1.3s）
- OpenAlex（`api.openalex.org`，1.2s）
- Semantic Scholar（可作备选，但 venue 字段质量较差，实测把 Advanced Materials 写成
  "Advances in Materials"）

与 bib 的关系（用户第 3 问）：**字段重叠但不相同**——
- overlap：doi / title / authors / journal / year / issn / abstract / times_cited → 两边都有；
- 只有 bib 有：keywords、wos_categories（学科分类）、wos_id（UT 号）、references（参考文献）、
  funding、affiliations；
- 只有我们填：paper_id（关联解析篇）、kind（资源类型）、journal_override（人工纠正）。
所以策略是"**只补空字段**"：先自动补上让评分能算，bib 一到就用权威值覆盖书目字段，
而上面三个应用自维护字段由 `db.upsert_meta` 的合并语义保住。
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

_UA = "PaperAgent/0.3 (metadata enrich; +https://localhost)"
_TIMEOUT = 20
_TAG_RE = re.compile(r"<[^>]+>")


def _get_json(url: str, timeout: int = _TIMEOUT) -> Any | None:
    req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                              "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        logger.info("元数据源 %s 返回 %s", url.split("?")[0], e.code)
    except Exception as e:  # noqa: BLE001 - 网络失败不阻塞主流程
        logger.warning("元数据源请求失败 %s: %s", url.split("?")[0], e)
    return None


def _clean_abstract(raw: str) -> str:
    """Crossref 的摘要带 JATS XML（`<jats:p>`），去掉标签只留正文。"""
    if not raw:
        return ""
    text = _TAG_RE.sub(" ", raw)
    text = re.sub(r"\s+", " ", text).strip()
    # Crossref 常以 "Abstract" 开头，去掉这个冗词
    return re.sub(r"^Abstract\s*", "", text, flags=re.I)[:4000]


def _from_crossref(doi: str) -> dict | None:
    data = _get_json("https://api.crossref.org/works/" + urllib.parse.quote(doi))
    m = (data or {}).get("message") or {}
    if not m:
        return None
    date = (m.get("published-print") or m.get("published-online")
            or m.get("issued") or {}).get("date-parts") or [[None]]
    year = date[0][0] if date and date[0] else None
    return {
        "source": "crossref",
        "doi": doi,
        "title": (m.get("title") or [""])[0].strip(),
        "journal": (m.get("container-title") or [""])[0].strip(),
        "issn": (m.get("ISSN") or [""])[0],
        "eissn": (m.get("ISSN") or ["", ""])[1] if len(m.get("ISSN") or []) > 1 else "",
        "year": str(year or ""),
        "times_cited": int(m.get("is-referenced-by-count") or 0),
        "abstract": _clean_abstract(m.get("abstract") or ""),
        "authors": [f"{a.get('given','')} {a.get('family','')}".strip()
                    for a in (m.get("author") or []) if a.get("family") or a.get("given")],
        "type": m.get("type") or "",
    }


def _from_openalex(doi: str) -> dict | None:
    data = _get_json("https://api.openalex.org/works/doi:" + urllib.parse.quote(doi))
    if not data or not data.get("title"):
        return None
    src = ((data.get("primary_location") or {}).get("source") or {})
    inv = data.get("abstract_inverted_index") or {}
    abstract = ""
    if inv:   # OpenAlex 用"词 → 位置列表"的倒排表示，需要还原
        pos: dict[int, str] = {}
        for word, idxs in inv.items():
            for i in idxs:
                pos[i] = word
        abstract = " ".join(pos[i] for i in sorted(pos))[:4000]
    # 2026-09-12（用户反馈"L1 缺研究单位/通信作者/关键词"）：OpenAlex 是这三项的主要来源
    authorships = data.get("authorships") or []
    affils: list[str] = []
    for a in authorships:
        for raw in (a.get("raw_affiliation_strings") or []):
            s = (raw or "").strip()
            if s and s not in affils:
                affils.append(s)
    # 规范化机构名只补"原始串里没有的"（否则同一单位会重复出现两次）
    joined = " || ".join(affils).lower()
    for a in authorships:
        for inst in (a.get("institutions") or []):
            name = (inst.get("display_name") or "").strip()
            if name and name.lower() not in joined and name not in affils:
                affils.append(name)
    corresponding = []
    for a in authorships:
        if a.get("is_corresponding"):
            name = ((a.get("author") or {}).get("display_name") or "").strip()
            if name and name not in corresponding:
                corresponding.append(name)
    kws = [str(k.get("display_name") or "").strip()
           for k in (data.get("keywords") or []) if k.get("display_name")]
    return {
        "source": "openalex",
        "doi": doi,
        "title": str(data.get("title") or "").strip(),
        "journal": str(src.get("display_name") or "").strip(),
        "issn": str(src.get("issn_l") or ""),
        "eissn": (src.get("issn") or ["", ""])[1] if len(src.get("issn") or []) > 1 else "",
        "year": str(data.get("publication_year") or ""),
        "times_cited": int(data.get("cited_by_count") or 0),
        "abstract": abstract,
        "authors": [((a.get("author") or {}).get("display_name") or "").strip()
                    for a in authorships if (a.get("author") or {}).get("display_name")],
        "affiliations": affils[:12],
        "keywords": kws[:12],
        "corresponding": corresponding[:8],
        "type": str(data.get("type") or ""),
    }


def fetch_doi_metadata(doi: str) -> dict | None:
    """DOI → 元数据 dict（Crossref 题录优先，OpenAlex 兜底 + **补单位/关键词/通信作者**）。

    2026-09-12（用户反馈）此前只有在 Crossref 拿不到题录时才查 OpenAlex ⇒
    `affiliations`/`keywords`/`corresponding` 从未被取到 → L1 元数据缺研究单位与关键词。
    现在 Crossref 成功也**再查一次 OpenAlex**（免费、~1s）并合并这三项。
    """
    doi = (doi or "").strip()
    if not doi:
        return None
    got = _from_crossref(doi)
    alt = _from_openalex(doi)
    if got and (got.get("journal") or got.get("title")):
        if alt:
            for field in ("affiliations", "keywords", "corresponding"):
                if not got.get(field) and alt.get(field):
                    got[field] = alt[field]
            # 作者：Crossref 为空时用 OpenAlex 的
            if not got.get("authors") and alt.get("authors"):
                got["authors"] = alt["authors"]
            got.setdefault("sources", []).append("openalex")
        return got
    return alt or got


def enrich_paper_meta(doi: str, paper_id: int | None = None) -> dict:
    """把 DOI 抓到的元数据**只补空字段**写进 papers_meta（bib 权威值不覆盖）。

    返回摘要：{ok, doi, rid, filled:[...], kept:[...], source, score, level}
    """
    from paperkb import api as kbapi
    from paperkb.doi import normalize_doi
    from paperkb.models import PaperMeta
    from paperkb.score import value_score

    doi = (doi or "").strip()
    if not doi:
        return {"ok": False, "reason": "no_doi"}
    got = fetch_doi_metadata(doi)
    if not got:
        return {"ok": False, "reason": "no_metadata_from_sources", "doi": doi}

    # 触发 paperkb 懒初始化：本函数直接用门面的 `_need_store()`，绕过了 KbMetaService 的
    # 懒初始化 ⇒ 后端刚启动就调用会抛"paperkb 未初始化"（实测踩到）。
    from .kbmeta_service import get_kbmeta

    get_kbmeta().ensure()

    store = kbapi._need_store()  # noqa: SLF001 - 与门面共享同一 KBStore 单例
    key = normalize_doi(doi) or doi
    old = store.get_meta(key) or store.get_meta(doi)

    def _fill(cur: str, new: str) -> tuple[str, bool]:
        """只在现值空时用新值（返回 (值, 是否用了新值)）。"""
        cur = (cur or "").strip()
        if cur:
            return cur, False
        return (new or "").strip(), bool((new or "").strip())

    base = old or PaperMeta(doi=doi)
    filled: list[str] = []
    values: dict = {"doi": doi, "rid": getattr(base, "rid", "") or ""}

    for field in ("title", "journal", "issn", "eissn", "year", "abstract"):
        val, used = _fill(getattr(base, field, ""), got.get(field, ""))
        values[field] = val
        if used:
            filled.append(field)
    # 被引次数：0 视为"未有值"，允许补
    if int(getattr(base, "times_cited", 0) or 0) <= 0 and int(got.get("times_cited") or 0) > 0:
        values["times_cited"] = int(got["times_cited"])
        filled.append("times_cited")
    else:
        values["times_cited"] = int(getattr(base, "times_cited", 0) or 0)
    if not getattr(base, "authors", None) and got.get("authors"):
        values["authors"] = list(got["authors"])
        filled.append("authors")
    # 2026-09-12（用户反馈"L1 缺研究单位/通信作者/关键词"）：这三项此前完全没写库
    if not getattr(base, "affiliations", None) and got.get("affiliations"):
        values["affiliations"] = list(got["affiliations"])
        filled.append("affiliations")
    if not getattr(base, "keywords", None) and got.get("keywords"):
        values["keywords"] = list(got["keywords"])
        filled.append("keywords")
    if not getattr(base, "corresponding", None) and got.get("corresponding"):
        values["corresponding"] = list(got["corresponding"])
        filled.append("corresponding")
    # 来源标记：用于区分"bib 权威" vs "DOI 临时补全"，同时是评分里 has_bib 的判据
    values["source_file"] = (getattr(base, "source_file", "") or ""
                             or f"{got.get('source','doi')}:{doi}")
    if paper_id and not getattr(base, "paper_id", None):
        values["paper_id"] = paper_id
        filled.append("paper_id")

    meta = base.model_copy(update=values)
    new_rid = store.upsert_meta(meta, rid=getattr(meta, "rid", "") or "")

    info = kbapi.journals_lookup(meta.journal) if meta.journal else None
    scored = value_score(meta, journal_info=info,
                         has_bib=bool(meta.source_file), current_year=None)
    return {"ok": True, "doi": doi, "rid": new_rid, "source": got.get("source"),
            "filled": filled, "kept": [f for f in ("title", "journal", "year",
                                                   "times_cited", "abstract")
                                       if f not in filled],
            "journal_matched": bool(info),
            "score": scored["score"], "level": scored["level"]}
