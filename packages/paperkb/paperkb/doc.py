# -*- coding: utf-8 -*-
"""document.json 轻量读取（paperkb 自解析，不依赖 paperparse——保持解耦）。

document.json = 干净版段落结构（与 en.md 一一对应，非 mineru_full.md 拼接版）：
  metadata / paragraphs[{para_id,section,text_en,text_zh,is_heading,is_caption}] /
  sections / figures / ai_summary
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Para:
    para_id: str = ""
    section: str = ""
    text_en: str = ""
    text_zh: str = ""
    is_heading: bool = False
    is_caption: bool = False


@dataclass
class PaperDoc:
    doi: str = ""
    title: str = ""
    paragraphs: list[Para] = field(default_factory=list)
    sections: list[dict] = field(default_factory=list)   # [{section, count}]
    figures: list[dict] = field(default_factory=list)
    ai_summary: dict = field(default_factory=dict)
    source_path: str = ""

    # ---- 便捷
    def body_paras(self) -> list[Para]:
        return [p for p in self.paragraphs
                if not p.is_heading and not p.is_caption and (p.text_en or p.text_zh)]

    def text_en_all(self, limit_chars: int = 0) -> str:
        parts = [p.text_en for p in self.body_paras() if p.text_en]
        text = "\n".join(parts)
        if limit_chars and len(text) > limit_chars:
            text = text[:limit_chars]
        return text

    def skeleton(self) -> str:
        """段落骨架（章节 + para_id + 首句）——省 token 的编译输入。"""
        lines = []
        cur = ""
        for p in self.paragraphs:
            if p.is_heading:
                cur = p.text_en.strip() or cur
                lines.append(f"## {cur}")
                continue
            if p.is_caption or not (p.text_en or p.text_zh):
                continue
            head = (p.text_en or p.text_zh or "").strip()[:80]
            lines.append(f"[{p.para_id}] {head}")
        return "\n".join(lines)


def read_document(path: str | Path) -> PaperDoc:
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    doc = PaperDoc(source_path=str(p))
    md = data.get("metadata") or {}
    doc.doi = md.get("doi") or ""
    doc.title = md.get("title") or ""
    doc.ai_summary = data.get("ai_summary") or {}
    doc.sections = data.get("sections") or []
    doc.figures = data.get("figures") or []
    for raw in data.get("paragraphs") or []:
        doc.paragraphs.append(Para(
            para_id=raw.get("para_id") or "",
            section=raw.get("section") or "",
            text_en=raw.get("text_en") or "",
            text_zh=raw.get("text_zh") or "",
            is_heading=bool(raw.get("is_heading")),
            is_caption=bool(raw.get("is_caption")),
        ))
    return doc


def find_document_in_kb(kb_dir: Path, doi: str, store=None) -> Path | None:
    """在 kb/<资源目录>/ 下找 document.json（原文层四件之一）。

    P0-B step4：键不再限于 DOI——RID / 目录名 / md5 目录都能命中
    （无 DOI 文献同样能编译）；`store` 给出时启用映射表与兜底。
    """
    from .resource import find_doc

    return find_doc(kb_dir, doi, store)
