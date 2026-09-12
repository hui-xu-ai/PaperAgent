# -*- coding: utf-8 -*-
"""卡片式用户工作产物（KB-DESIGN v0.6 §3.1 D7）。

kb/<DOI>/cards/：translate-<slug>.md / summary-<slug>.md / qa-<slug>.md / note-<slug>.md
- frontmatter：type/doi/para_ids/tags/created
- 编译/问答按需读取（按 type/para_ids/tags），不整读 en.md → 省 token
- 用户内容：系统永不覆盖
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from .doi import doi_to_dirname

CARD_TYPES = ("card-translate", "card-summary", "card-qa", "card-note")
_TYPE_PREFIX = {"card-translate": "translate", "card-summary": "summary",
                "card-qa": "qa", "card-note": "note"}

_TEMPLATE = """---
type: {card_type}
doi: {doi}
scope: {scope}
para_ids: [{para_ids}]
tags: [{tags}]
created: {created}
---

{content}
"""


def _slug(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^\w\u4e00-\u9fff\-]+", "-", (text or "").strip()).strip("-")
    return (slug or "card")[:max_len].lower()


def _cards_dir(kb_dir: Path, doi: str) -> Path:
    d = kb_dir / doi_to_dirname(doi) / "cards"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_card(kb_dir: Path, doi: str, card_type: str, content: str,
               title: str = "", para_ids: list[str] | None = None,
               tags: list[str] | None = None,
               slug: str | None = None, scope: str = "") -> dict:
    """写一张卡片（新增；同 slug 覆盖需显式）。返回 {path, slug}。"""
    if card_type not in CARD_TYPES:
        raise ValueError(f"未知卡片类型: {card_type}（可选 {CARD_TYPES}）")
    if not content.strip():
        raise ValueError("卡片内容不能为空")
    d = _cards_dir(kb_dir, doi)
    name = f"{_TYPE_PREFIX[card_type]}-{slug or _slug(title or content[:40])}.md"
    path = d / name
    path.write_text(_TEMPLATE.format(
        card_type=card_type, doi=doi, scope=scope,
        para_ids=", ".join(para_ids or []),
        tags=", ".join(tags or []),
        created=datetime.now().strftime("%Y-%m-%d"),
        content=content.strip()), encoding="utf-8")
    return {"path": str(path.relative_to(kb_dir)), "slug": path.stem}


def list_cards(kb_dir: Path, doi: str | None = None,
               card_type: str | None = None) -> list[dict]:
    """列出卡片（可选按类型过滤）。"""
    root = kb_dir if doi is None else kb_dir / doi_to_dirname(doi)
    if not root.exists():
        return []
    out = []
    for p in sorted(root.rglob("cards/*.md")):
        text = p.read_text(encoding="utf-8", errors="replace")
        fm = _frontmatter(text)
        if card_type and fm.get("type") != card_type:
            continue
        out.append({"path": str(p.relative_to(kb_dir)), "doi": fm.get("doi", ""),
                    "type": fm.get("type", ""), "para_ids": fm.get("para_ids", []),
                    "tags": fm.get("tags", []), "created": fm.get("created", "")})
    return out


def read_cards_for_compile(kb_dir: Path, doi: str,
                           types: list[str] | None = None,
                           limit_chars: int = 4000) -> str:
    """按需读取卡片（编译/问答输入）：按类型+段落过滤，预算截断。"""
    d = kb_dir / doi_to_dirname(doi) / "cards"
    if not d.exists():
        return ""
    want = set(types or [])
    parts = []
    total = 0
    for p in sorted(d.glob("*.md")):
        text = p.read_text(encoding="utf-8", errors="replace")
        fm = _frontmatter(text)
        if want and fm.get("type") not in want:
            continue
        body = _body(text)
        if not body:
            continue
        head = f"[{fm.get('type','')}] {body[:600]}"
        if total + len(head) > limit_chars:
            break
        parts.append(head)
        total += len(head)
    return "\n\n".join(parts)


def _frontmatter(text: str) -> dict:
    fm: dict = {}
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return fm
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k = k.strip()
        v = v.strip()
        if v.startswith("[") and v.endswith("]"):
            fm[k] = [x.strip() for x in v[1:-1].split(",") if x.strip()]
        else:
            fm[k] = v
    return fm


def _body(text: str) -> str:
    m = re.match(r"^---\n.*?\n---\n", text, re.S)
    return text[m.end():].strip() if m else text.strip()
