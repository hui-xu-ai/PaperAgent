# -*- coding: utf-8 -*-
r"""只读探针：把"DOI→Crossref 元数据 + journals.db 的 IF/分区"代入真实评分函数，看能到几级。

用法：python tools\dbg_score_sim.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, str(Path("packages/paperkb").resolve()))
JOURNALS_REF = Path("data/reference/journals.db")


def _get(url: str):
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> None:
    print("== live 后端 journals 状态 ==")
    try:
        print("  stats  :", _get("http://127.0.0.1:8900/api/kb-meta/journals/stats"))
    except Exception as e:  # noqa: BLE001
        print("  stats 查询失败:", e)
    for nm in ("Advanced Materials",):
        try:
            print(f"  lookup {nm!r}:",
                  _get("http://127.0.0.1:8900/api/kb-meta/journals/lookup?name="
                       + urllib.parse.quote(nm)))
        except Exception as e:  # noqa: BLE001
            print("  lookup 失败:", e)

    print("\n== 参考库（已导入过的那份）里该刊的 jcr + cas ==")
    con = sqlite3.connect(f"file:{JOURNALS_REF.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    jcr = con.execute("select * from jcr where journal_name='ADVANCED MATERIALS'").fetchone()
    cas = con.execute("select * from cas where journal_name = 'ADVANCED MATERIALS'").fetchone()
    print("  jcr:", dict(jcr) if jcr else None)
    print("  cas:", dict(cas) if cas else None)

    print("\n== 代入真实评分函数 ==")
    from paperkb.models import PaperMeta
    from paperkb.score import value_score

    journal_info = {"jcr": dict(jcr) if jcr else None, "cas": dict(cas) if cas else None}
    cases = [
        ("只有 DOI 元数据（Crossref: journal+year+被引10）",
         dict(journal_info=journal_info, times_cited=10, year="2024")),
        ("同上但被引 3",
         dict(journal_info=journal_info, times_cited=3, year="2024")),
        ("同上但被引 26",
         dict(journal_info=journal_info, times_cited=26, year="2024")),
        ("无 journals.db（IF 不可用）+ 被引10",
         dict(journal_info=None, times_cited=10, year="2024")),
        ("无任何元数据（现状）",
         dict(journal_info=None, times_cited=0, year="")),
    ]
    for label, kw in cases:
        meta = PaperMeta(doi="10.1002/adma.202407106", title="T",
                         journal="ADVANCED MATERIALS", year=kw["year"],
                         times_cited=kw["times_cited"], source_file="crossref:doi")
        r = value_score(meta, journal_info=kw["journal_info"],
                        has_bib=bool(meta.source_file), current_year=2026)
        parts = {k: f"{v['value']}({'可用' if v['available'] else '不可用'})"
                 for k, v in r["parts"].items()}
        print(f"  {label:38s} → score={r['score']:>4}  level={r['level']}")
        print(f"      parts: {parts}")
    con.close()


if __name__ == "__main__":
    import urllib.parse  # noqa: E402
    main()
