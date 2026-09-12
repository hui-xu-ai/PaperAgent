# -*- coding: utf-8 -*-
r"""调研探针：用文献内解析出的 DOI，能否从公开元数据源临时补全 papers_meta？

对比三个源（都不需要 Key）：
- Crossref  : https://api.crossref.org/works/<doi>
- OpenAlex  : https://api.openalex.org/works/doi:<doi>
- Semantic Scholar: https://api.semanticscholar.org/graph/v1/paper/DOI:<doi>

用法：python tools\dbg_doi_meta_probe.py [doi]
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

DOI = sys.argv[1] if len(sys.argv) > 1 else "10.1002/adma.202407106"
UA = "PaperAgent/0.3 (metadata probe; mailto:noreply@example.com)"


def _get(url: str, timeout: int = 25) -> tuple[int, dict | list | str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="replace")
            return r.status, json.loads(raw)
    except urllib.error.HTTPError as e:
        return e.code, f"HTTPError: {e.reason}"
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def crossref(doi: str) -> None:
    print("\n=== Crossref ===")
    st, data = _get("https://api.crossref.org/works/" + urllib.parse.quote(doi))
    print("status:", st)
    if st != 200 or not isinstance(data, dict):
        print("  ", str(data)[:200])
        return
    m = data.get("message", {})
    print("  title      :", (m.get("title") or [""])[0][:90])
    print("  container  :", (m.get("container-title") or [""])[0][:70])
    print("  ISSN       :", m.get("ISSN"))
    print("  type       :", m.get("type"))
    yr = (m.get("published-print") or m.get("published-online")
          or m.get("issued") or {}).get("date-parts", [[None]])[0][0]
    print("  year       :", yr)
    print("  is-ref-by  :", m.get("is-referenced-by-count"), "（被引次数）")
    print("  authors    :", ", ".join(
        f"{a.get('family','')} {a.get('given','')}".strip()
        for a in (m.get("author") or [])[:4]))
    print("  abstract   :", (m.get("abstract") or "")[:120].replace("\n", " "))


def openalex(doi: str) -> None:
    print("\n=== OpenAlex ===")
    st, data = _get("https://api.openalex.org/works/doi:" + urllib.parse.quote(doi))
    print("status:", st)
    if st != 200 or not isinstance(data, dict):
        print("  ", str(data)[:200])
        return
    print("  title      :", str(data.get("title"))[:90])
    src = ((data.get("primary_location") or {}).get("source") or {})
    print("  journal    :", src.get("display_name"))
    print("  issn_l     :", src.get("issn_l"), " issn:", src.get("issn"))
    print("  year       :", data.get("publication_year"))
    print("  cited_by   :", data.get("cited_by_count"), "（被引次数）")
    print("  type       :", data.get("type"))
    inv = data.get("abstract_inverted_index")
    print("  abstract   :", "有（inverted index，可还原）" if inv else "无")


def s2(doi: str) -> None:
    print("\n=== Semantic Scholar ===")
    fields = "title,abstract,year,venue,citationCount,externalIds,publicationTypes"
    st, data = _get(f"https://api.semanticscholar.org/graph/v1/paper/DOI:{urllib.parse.quote(doi)}"
                    f"?fields={fields}")
    print("status:", st)
    if st != 200 or not isinstance(data, dict):
        print("  ", str(data)[:200])
        return
    for k in ("title", "venue", "year", "citationCount"):
        print(f"  {k:11s}:", str(data.get(k))[:80])
    print("  abstract   :", (data.get("abstract") or "")[:120].replace("\n", " "))


def main() -> None:
    print(f"DOI = {DOI}")
    for fn in (crossref, openalex, s2):
        t0 = time.perf_counter()
        fn(DOI)
        print(f"  用时 {time.perf_counter() - t0:.2f}s")


if __name__ == "__main__":
    main()
