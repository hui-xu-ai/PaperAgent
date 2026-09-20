# -*- coding: utf-8 -*-
"""文献阅读日记：从 paperkb 现有数据推导「每日阅读活动」+ 用户笔记持久化。

数据源（全部来自现有 paperkb 数据，不新增/不改用户元数据）：
- papers_meta（store.list_meta）：doi/title/journal/year/imported_at → 导入日期与文献列表
- knowledge_base/<doi_to_dirname(doi)>/：en.md→已解析；en_zh.md/zh.md/document.json(text_zh)
  →已翻译；kb 目录存在且已编译(_note/_details/_wiki/summary 等)→已纳入
- 纳入日期：kb 目录 mtime（编译/同步写入时间）
- 用户笔记：knowledge_base/_diary/<YYYY-MM-DD>.md（文件式 markdown，Obsidian 可管理）

与 backend 路由/KbMetaService 解耦：本模块只依赖 paperkb.db.KBStore 与 Roots，
由 backend 薄封装注入（★不 import paperkb.api——避免与 api.py 的并行改动冲突）。

函数签名约定（task 指定）：diary_days(store, roots, month=None) /
diary_day(store, roots, date) / diary_note_write(roots, date, text) / diary_export(roots)。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from .config import Roots
from .doi import doi_to_dirname

# 全量扫描上限（与后端 list_papers 同量级；5000 篇量级为轻量扫描）
_FULL_SCAN = 1_000_000
# 用户笔记目录（属于知识库，Obsidian 可管理）：knowledge_base/_diary/
_DIARY_DIRNAME = "_diary"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# 视为「已编译」的 kb 目录内产物文件名（D16：summary.md 不再是编译产物，六维在 _note.md）
_COMPILED_MARKERS = ("_note.md", "_wiki.md", "_relations.md")


# ------------------------------------------------------------------ 工具
def _date_from_str(s: str) -> str:
    """任意时间字符串 → 'YYYY-MM-DD'（解析失败返回 ''）。"""
    if not s:
        return ""
    d = str(s).strip()[:10]
    return d if _DATE_RE.fullmatch(d) else ""


def _mtime_date(p: Path) -> str:
    """路径 mtime → 'YYYY-MM-DD'。"""
    try:
        return datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d")
    except (OSError, ValueError):
        return ""


def _kb_dir(roots: Roots, doi: str) -> Path:
    return roots.kb_dir / doi_to_dirname(doi)


def _lib_dir(roots: Roots, doi: str) -> Path:
    return roots.library_dir / doi_to_dirname(doi)


def _paper_last_date(roots: Roots, doi: str) -> str:
    """无 imported_at 时的兜底活动日期：kb/library 任一目录 mtime，无则 ''。"""
    for d in (_kb_dir(roots, doi), _lib_dir(roots, doi)):
        if d.is_dir():
            return _mtime_date(d)
    return ""


def _translated_paragraphs(roots: Roots, doi: str) -> int:
    """读 document.json 统计已译段落数（kb 优先，回退 library）。"""
    for base in (roots.kb_dir, roots.library_dir):
        p = base / doi_to_dirname(doi) / "document.json"
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            return 0
        paras = data.get("paragraphs") or []
        return sum(1 for x in paras if (x.get("text_zh") or "").strip())
    return 0


def _paper_state(store, roots: Roots, doi: str, meta: dict | None = None) -> dict:
    """单篇在阅读日记中的状态（parsed/translated/in_kb/compiled + status）。"""
    kd = _kb_dir(roots, doi)
    has_kb_dir = kd.is_dir()
    en_md = (kd / "en.md").exists() or (_lib_dir(roots, doi) / "en.md").exists()
    # 已编译：kb 目录出现编译产物标记，或 notes_fts 已索引进该篇（双通道判定）
    compiled = has_kb_dir and any((kd / m).exists() for m in _COMPILED_MARKERS)
    if not compiled:
        try:
            compiled = bool(store.notes_files(doi))
        except Exception:  # noqa: BLE001 - FTS 未初始化等异常不阻断
            compiled = False
    translated_paras = _translated_paragraphs(roots, doi)
    translated = translated_paras > 0 or (kd / "en_zh.md").exists() or (kd / "zh.md").exists()
    in_kb = has_kb_dir and (en_md or compiled)

    # 派生 status（尽力而为的阶梯；布尔字段承载精确真值）
    if translated:
        status = "translated"
    elif in_kb:
        status = "parsed"
    elif en_md:
        status = "parsed"
    else:
        status = "pending"

    return {
        "doi": doi,
        "title": (meta or {}).get("title", "") if meta else "",
        "journal": (meta or {}).get("journal", "") if meta else "",
        "year": (meta or {}).get("year", "") if meta else "",
        "status": status,
        "parsed": bool(en_md),
        "translated": bool(translated),
        "in_kb": bool(in_kb),
        "compiled": bool(compiled),
        "translated_paragraphs": translated_paras,
    }


def _iter_papers(store) -> list[dict]:
    """store.list_meta → [{doi,title,journal,year,imported_at}]（含 imported_at）。"""
    metas = store.list_meta(limit=_FULL_SCAN)
    out = []
    for m in metas:
        d = getattr(m, "model_dump", None)
        row = d(mode="json") if d else dict(m)
        out.append({
            "doi": row.get("doi") or "",
            "title": row.get("title") or "",
            "journal": row.get("journal") or "",
            "year": row.get("year") or "",
            "imported_at": row.get("imported_at") or "",
        })
    return out


# ------------------------------------------------------------------ 公开 API
def diary_days(store, roots: Roots, month: str | None = None) -> dict:
    """所有有活动的日期（导入/纳入/笔记任一）＋每月聚合（供月历圆点）。

    month（可选，'YYYY-MM'）：返回该月内的 days（months 仍按全量聚合）。
    """
    day: dict[str, dict] = {}

    def _touch(date: str, key: str) -> None:
        if not date:
            return
        ent = day.setdefault(date, {"date": date, "imports": 0, "kb": 0,
                                    "notes": 0, "events": 0})
        ent[key] += 1
        ent["events"] += 1

    # 导入日（解析/翻译状态今日归属于导入日——task 约定）
    for p in _iter_papers(store):
        date = _date_from_str(p["imported_at"]) or _paper_last_date(roots, p["doi"])
        _touch(date, "imports")

    # 纳入日（kb 目录 mtime；跳过 _diary/_index 等系统子目录）
    if roots.kb_dir.exists():
        for d in roots.kb_dir.iterdir():
            if d.is_dir() and not d.name.startswith("_"):
                _touch(_mtime_date(d), "kb")

    # 笔记日（仅当笔记有实际内容才算——空/被删的文件不算，避免"空白日记"误标）
    diary_dir = roots.kb_dir / _DIARY_DIRNAME
    if diary_dir.exists():
        for f in sorted(diary_dir.glob("*.md")):
            if _DATE_RE.fullmatch(f.stem):
                try:
                    if f.read_text(encoding="utf-8", errors="replace").strip():
                        _touch(f.stem, "notes")
                except OSError:
                    pass

    if month:
        day = {k: v for k, v in day.items() if k.startswith(month)}

    # 月度聚合（供月历圆点）
    months: dict[str, int] = {}
    for d in day.values():
        m = d["date"][:7]
        months[m] = months.get(m, 0) + d["events"]

    ordered = sorted(day.values(), key=lambda x: x["date"], reverse=True)
    return {"days": ordered,
            "months": [{"month": m, "count": c} for m, c in
                       sorted(months.items(), reverse=True)]}


def diary_day(store, roots: Roots, date: str) -> dict:
    """某天详情：导入文献列表（title/doi/status/parsed/translated/in_kb）+ 当天用户笔记。"""
    if not _DATE_RE.fullmatch(date):
        raise ValueError(f"非法日期（需 YYYY-MM-DD）: {date!r}")
    imports = []
    for p in _iter_papers(store):
        d = _date_from_str(p["imported_at"]) or _paper_last_date(roots, p["doi"])
        if d == date:
            imports.append(_paper_state(store, roots, p["doi"], meta=p))
    note = _read_note(roots, date)
    return {"date": date, "imports": imports, "note": note}


def diary_note_write(roots: Roots, date: str, text: str) -> dict:
    """写 _diary/<date>.md（用户当天阅读心得）。空内容 → 删除该天日记（不留空白）。

    用户主动创建：仅在写入了非空内容时才生成文件；清空内容即删除该天日记。
    """
    if not _DATE_RE.fullmatch(date):
        raise ValueError(f"非法日期（需 YYYY-MM-DD）: {date!r}")
    content = (text or "").strip()
    d = roots.kb_dir / _DIARY_DIRNAME
    p = d / f"{date}.md"
    if not content:
        if p.exists():
            p.unlink()
        return {"ok": True, "date": date, "file": "", "deleted": True, "empty": True}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"ok": True, "date": date, "file": str(p), "deleted": False}


def diary_export(roots: Roots) -> dict:
    """整本日记导出 markdown（按日期倒序拼接）。"""
    diary_dir = roots.kb_dir / _DIARY_DIRNAME
    if not diary_dir.exists():
        return {"count": 0, "text": "# 文献阅读日记\n\n（暂无笔记）"}
    blocks: list[str] = []
    count = 0
    for f in sorted(diary_dir.glob("*.md"), reverse=True):
        if not _DATE_RE.fullmatch(f.stem):
            continue
        try:
            content = f.read_text(encoding="utf-8").strip()
        except OSError:
            content = ""
        blocks.append(f"## {f.stem}\n\n{content}")
        count += 1
    body = "\n\n".join(blocks) if blocks else "（暂无笔记）"
    return {"count": count, "text": f"# 文献阅读日记\n\n{body}"}


def _read_note(roots: Roots, date: str) -> str:
    """读取 _diary/<date>.md，无则 ''。"""
    p = roots.kb_dir / _DIARY_DIRNAME / f"{date}.md"
    try:
        if p.exists():
            return p.read_text(encoding="utf-8").rstrip("\n")
    except OSError:
        pass
    return ""
