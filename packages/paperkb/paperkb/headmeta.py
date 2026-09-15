# -*- coding: utf-8 -*-
"""文献头部元数据（YAML frontmatter）**唯一装配入口**。

2026-09-16 用户要求（逐字）："元数据，按照作者、通讯作者、研究单位、年份、期刊、
影响因子、JCR分区、中科院分区、DOI、被引、关键词排列。模板统一更换成这个样式。
元数据显示，模板中重复的这个：`[!info] 文献信息`，这部分直接删除。"

设计要点（为什么要有这个模块）：
- 变体（`zh.md`/`en_zh.md`）此前**由模板自己**从 `doc.metadata`（= document.json）拼 frontmatter，
  而 document.json 里 **没有期刊/年份/被引/影响因子**（那些在 `papers_meta` + `journals.db`）⇒
  同一文件里 frontmatter 与正文 callout 各说各话（实测 `期刊: ""` / `被引：0` 而 `_note.md`
  显示 `Advanced Materials 2024（Q1 JIF 26.8 中科院1区）/ 被引 11`）。
- 现在：**装配只在这里做一次**（`head_fields` → 有序字段），模板只负责原样输出
  `render_frontmatter(...)` 的字符串，不再自己拼字段。
- `journal_info` 由调用方（`api.py` 门面）查 `journals.db` 后传入 —— 本模块**不碰数据库**，
  保持纯函数、可单测。

顺序即用户给定顺序（空值不出行，不造 `—` 占位）。
"""
from __future__ import annotations

import json

__all__ = ["FIELD_ORDER", "head_fields", "render_frontmatter", "journal_info"]

# **用户给定顺序**（逐字）：作者、通讯作者、研究单位、年份、期刊、影响因子、JCR分区、
# 中科院分区、DOI、被引、关键词
FIELD_ORDER = ("作者", "通讯作者", "研究单位", "年份", "期刊", "影响因子",
               "JCR分区", "中科院分区", "DOI", "被引", "关键词")


def _get(meta, key: str, default=None):
    """`meta` 可为 PaperMeta 对象、dict 或 None（统一取值）。"""
    if meta is None:
        return default
    if isinstance(meta, dict):
        v = meta.get(key, default)
    else:
        v = getattr(meta, key, default)
    return default if v is None else v


def _list(meta, key: str) -> list[str]:
    v = _get(meta, key, []) or []
    if isinstance(v, str):
        v = [v]
    return [str(x).strip() for x in v if str(x).strip()]


def journal_info(journals, journal: str, issn: str = "", eissn: str = "",
                 year: int | None = None) -> dict | None:
    """查期刊指标（ISSN 优先 → 期刊名）；`journals` 为 JournalsDB 或 None。

    返回 `{"jif": 26.8, "jcr": "Q1", "cas": "1区"}`（取不到的键缺省），无命中返回 None。
    """
    if journals is None:
        return None
    info = None
    try:
        if issn or eissn:
            info = journals.lookup_issn(issn, eissn)
        if info is None and journal:
            info = journals.lookup(journal, year=year)
    except Exception:  # noqa: BLE001 - 指标表缺失/损坏不影响头部其余字段
        return None
    if not info:
        return None
    j = info.get("jcr") or {}
    c = info.get("cas") or {}
    out: dict = {}
    try:
        jif = float(j.get("jif") or 0)
    except (TypeError, ValueError):
        jif = 0.0
    if jif > 0:
        out["jif"] = int(jif) if float(jif).is_integer() else jif
    q = str(j.get("quartile") or "").strip()
    if q:
        out["jcr"] = q
    try:
        zone = int(c.get("zone") or 0)
    except (TypeError, ValueError):
        zone = 0
    if zone:
        out["cas"] = f"{zone}区"
    return out or None


def head_fields(meta, info: dict | None = None) -> dict:
    """→ 有序字段 dict（键 = `FIELD_ORDER` 的中文名；拿不到的字段**不出现**）。

    作者：全量，通信作者标 `*`（与 `_note.md` 的 `_render_note` 同一判据）；
    通讯作者/研究单位多值用 `；` 连接（与 `_note.md` 一致，便于人读）。
    """
    authors = _list(meta, "authors")
    corr = _list(meta, "corresponding")

    def _is_corr(a: str) -> bool:
        return any(a == c or (c and (c in a or a in c)) for c in corr)

    out: dict = {}
    if authors:
        out["作者"] = ", ".join(a + ("*" if _is_corr(a) else "") for a in authors)
    if corr:
        out["通讯作者"] = "；".join(corr)

    affils: list[str] = []
    for a in _list(meta, "affiliations"):
        if a not in affils:                       # 只去完全重复项，不改写来源文本
            affils.append(a)
    if affils:
        out["研究单位"] = "；".join(affils)

    year = str(_get(meta, "year", "") or "").strip()
    if year:
        out["年份"] = int(year) if year.isdigit() else year

    journal = str(_get(meta, "journal", "") or "").strip()
    if journal:
        out["期刊"] = journal

    info = info or {}
    if info.get("jif"):
        out["影响因子"] = info["jif"]
    if info.get("jcr"):
        out["JCR分区"] = info["jcr"]
    if info.get("cas"):
        out["中科院分区"] = info["cas"]

    doi = str(_get(meta, "doi", "") or "").strip()
    if doi:
        out["DOI"] = doi

    cited = _get(meta, "times_cited", None)
    if isinstance(cited, int):
        out["被引"] = cited

    keywords = _list(meta, "keywords")
    if keywords:
        out["关键词"] = ", ".join(keywords)
    return out


def _yaml_scalar(value) -> str:
    """标量 → YAML 行内文本（字符串一律 JSON 双引号转义；数字/布尔裸写）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def render_frontmatter(meta, *, info: dict | None = None, title: str = "",
                       tags: list[str] | None = None, source: str = "pdf",
                       created: str = "") -> str:
    """渲染完整 YAML frontmatter 块（含首尾 `---`）。

    `title` 首位（与旧模板一致，Obsidian 亦以 title 属性承载标题）；其余按 `FIELD_ORDER`
    排列；`tags`/`source`/`created` 末尾固定输出（与旧模板字段名保持一致，不破坏既有工具）。
    """
    lines = ["---"]
    fields = head_fields(meta, info)
    if title:
        lines.append(f"title: {_yaml_scalar(title)}")
    for key in FIELD_ORDER:
        if key in fields:
            lines.append(f"{key}: {_yaml_scalar(fields[key])}")
    lines.append("tags: " + json.dumps(list(tags or []), ensure_ascii=False))
    if source:
        lines.append(f"source: {source}")
    if created:
        lines.append(f"created: {created}")
    lines.append("---")
    return "\n".join(lines) + "\n"
