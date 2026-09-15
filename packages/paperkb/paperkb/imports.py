# -*- coding: utf-8 -*-
"""M5 导入流程：markdown 导入 + 简版 document.json 分段器 + kb 原文层四件物理复制 + 一致性校验。

设计依据（KB-DESIGN v0.6）：
- 途径 B（markdown 导入）：选 md 文件 → 按 DOI/文件名识别 → library/<DOI>/en.md +
  生成**简版 document.json**（本地分段器，无 LLM，D12）→ kb 纳入 + 入编译队列。
- 简版 document.json 对齐真实段落体系（与解析产物同构）：标题段保留 "# " 前缀且
  is_heading=False；章节标题 is_heading=True（text_en 保留原 "#+ " 前缀，
  section=剥前缀文本）；参考文献条目归入 section="References"（render 时剔除，
  与解析链一致——一致性校验两侧都跳过 References）；ai_summary 留空（翻译/编译时补）。
- kb 原文层四件物理复制（D10/D14/D17）：source.pdf/en.md/document.json/images/
  从 library 复制进 kb；**冻结原则**：kb 原文层放入后系统不自动改动，force=True
  （手动"重新同步原文"）才覆盖。
- 一致性校验（D17）：en.md 段落 ↔ document.json paragraphs 对齐检查（跳过
  参考文献段与纯图行；装饰标题 "A B S T R A C T" ↔ "Abstract" 等价，同 render_clean）。
"""
from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path

from .config import Roots
from .doi import doi_to_dirname, extract_doi_from_text, normalize_doi

logger = logging.getLogger(__name__)

# 章节标题块（整块单行且以 # 开头）
_HEAD_RE = re.compile(r"^(#{1,6})\s+(.+)$")
# 参考文献条目（render_clean 的剔除特征：<sup>[N]</sup> / 裸 [N] 行首）
_REF_ENTRY_RE = re.compile(r"^(?:<sup>)?\[\d+\](?:</sup>)?\s+\S")
# 图注启发（导入 md 无 is_caption 标注，按 "Fig./Figure/Table N" 开头判定）
_CAPTION_RE = re.compile(r"^(?:Fig(?:ure)?\.?\s*\d+|Table\s+\d+)\b", re.I)
# 纯图行（render_clean 在 caption 前插入 ![](...)，document.json 无对应段）
_IMAGE_LINE_RE = re.compile(r"^!\[[^\]]*\]\([^)]*\)\s*$")
# 装饰标题（render_clean：全大写单空格分隔 → 去空格 title()）
_DECORATIVE_RE = re.compile(r"^[A-Z0-9](?: [A-Z0-9])+$")


# ---------------------------------------------------------------- 分段器

def segment_markdown(md_text: str, *, doi: str = "",
                     meta: dict | None = None) -> dict:
    """md → 简版 document.json（本地分段器，无 LLM，D12）。

    meta: papers_meta 字典（bib 权威，v0.6 4.0）——有则 metadata 从它补全；
          无则 metadata 仅 md 提取 title，标记 needs_bib（"待 bib 确认"）。
    """
    blocks = [b for b in re.split(r"\n\s*\n", md_text) if b.strip()]
    paragraphs: list[dict] = []
    sections: list[dict] = []
    section_counts: dict[str, int] = {}
    cur_section = ""
    title = ""
    refs_zone = False
    n = 0
    for b in blocks:
        lines = [ln for ln in b.splitlines() if ln.strip()]
        if not lines:
            continue
        first = lines[0].strip()
        # 纯图行（![](...)）：不占段落（与解析产物一致——图挂在 figures，render 插回）
        if len(lines) == 1 and _IMAGE_LINE_RE.match(first):
            continue
        head = _HEAD_RE.match(first) if len(lines) == 1 else None
        if head is not None and head.group(1) == "#" and not title:
            # H1 标题段：is_heading=False + text_en 保留 "# "（与真实 document.json 一致）
            title = head.group(2).strip()
            n += 1
            paragraphs.append(_para(f"P{n:03d}", n, "", b.rstrip("\n"),
                                    heading=False, caption=False, refs=False))
            continue
        if head is not None:
            # 章节标题（## 及以上）
            sec = head.group(2).strip()
            is_refs_heading = sec.lower() == "references"
            cur_section = "References" if is_refs_heading else sec
            refs_zone = is_refs_heading
            n += 1
            paragraphs.append(_para(f"P{n:03d}", n, cur_section, b.rstrip("\n"),
                                    heading=True, caption=False, refs=False))
            continue
        # 正文 / 图注 / 参考文献条目
        is_ref = refs_zone or bool(_REF_ENTRY_RE.match(first))
        is_cap = (not is_ref) and bool(_CAPTION_RE.match(first))
        if is_ref and not refs_zone:
            cur_section = "References"
            refs_zone = True
        sec = "References" if is_ref else cur_section
        n += 1
        paragraphs.append(_para(f"P{n:03d}", n, sec, b.rstrip("\n"),
                                heading=False, caption=is_cap, refs=is_ref))
        if not is_ref and not is_cap:
            section_counts[sec] = section_counts.get(sec, 0) + 1
    sections = [{"section": k, "count": v} for k, v in section_counts.items()
                if k and v > 0]
    # metadata：bib 权威（meta 有则补全），无 bib → 缺标记
    metadata: dict = {"doi": doi, "title": title, "source": "markdown-import"}
    if meta:
        for k in ("journal", "year", "issn", "eissn", "keywords",
                  "abstract", "authors", "research_areas", "times_cited"):
            v = meta.get(k)
            if v:
                metadata[k] = v
        metadata["meta_source"] = "papers_meta"
    else:
        metadata["meta_source"] = "md_only"
        metadata["needs_bib"] = True            # "待 bib 确认"标记（v0.6 4.0）
    return {
        "metadata": metadata,
        "paragraphs": paragraphs,
        "sections": sections,
        "figures": [],
        "ai_summary": {},
    }


def _para(pid: str, order: int, section: str, text: str, *,
          heading: bool, caption: bool, refs: bool) -> dict:
    return {
        "para_id": pid,
        "order": order,
        "section": section,
        "text_en": text,
        "text_zh": "",
        "source_block_ids": [],
        "confidence": 1.0,
        "needs_ai_check": False,
        "is_calibrated": False,
        "is_heading": heading,
        "is_caption": caption,
        "is_reference": refs,
        "coords": {"pages": [], "source": "markdown-import"},
    }


def _md_title(md_text: str) -> str:
    """取 md 首个 H1 标题（无则空）。"""
    for ln in md_text.splitlines():
        m = _HEAD_RE.match(ln.strip())
        if m and m.group(1) == "#":
            return m.group(2).strip()
    return ""


def detect_doi(md_path: str | Path, text: str = "") -> str:
    """从文件名 + 内容前 2000 字符提取 DOI（含下划线目录名形态）。"""
    p = Path(md_path)
    text = text or ""
    for cand in (p.stem, p.name, text[:2000]):
        d = extract_doi_from_text(cand)
        if d:
            return normalize_doi(d)
    m = re.match(r"^(10\.\d{4,9})_([A-Za-z0-9.\-]+)$", p.stem)
    if m:
        return normalize_doi(f"{m.group(1)}/{m.group(2)}")
    return ""


# ---------------------------------------------------------------- 导入（途径 B）

def import_markdown(md_path: str | Path, roots: Roots, *,
                    meta: dict | None = None, doi: str = "") -> dict:
    """写 library/<DOI>/en.md + document.json（不碰 kb；kb 纳入由调用方 sync）。

    返回 {doi, dir, title, paragraphs, segments}。
    """
    p = Path(md_path)
    if not p.exists():
        raise FileNotFoundError(f"markdown 文件不存在: {p}")
    text = p.read_text(encoding="utf-8", errors="replace")
    doi = normalize_doi(doi) or detect_doi(p, text)
    if not doi:
        raise ValueError(
            "无法识别 DOI：文件名/内容无 DOI 且无匹配元数据。请先导入 bib，"
            "或用 WOS 检索式补元数据后再导入。")
    title = _md_title(text)
    doc = segment_markdown(text, doi=doi, meta=meta)
    if not title:
        title = doc["metadata"]["title"]
    d = roots.library_dir / doi_to_dirname(doi)
    d.mkdir(parents=True, exist_ok=True)
    (d / "en.md").write_text(text, encoding="utf-8")
    (d / "document.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"doi": doi, "dir": str(d), "title": title,
            "paragraphs": len(doc["paragraphs"]),
            "sections": len(doc["sections"])}


# ---------------------------------------------------------------- kb 原文层四件

# kb 原文层四件（D10/D17）：存在才复制；source.pdf/images 可缺
_KB_SOURCE_ITEMS = (("source.pdf", False), ("en.md", False),
                    ("document.json", False), ("images", True))


def _is_stale(src: Path, dst: Path) -> bool:
    """[局部] 派生副本是否已陈旧（源比目标新 mtime；目录比内部文件最大 mtime）"""
    try:
        if src.is_dir() != dst.is_dir():
            return True
        if src.is_dir():
            def newest(d: Path) -> float:
                ts = [p.stat().st_mtime for p in d.rglob("*") if p.is_file()]
                return max(ts) if ts else 0.0
            return newest(src) > newest(dst) + 1
        return src.stat().st_mtime > dst.stat().st_mtime + 1
    except OSError:
        return False


def sync_source_to_kb(doi: str, roots: Roots, force: bool = False, store=None) -> dict:
    """library/<资源目录>/ 原文层四件物理复制 → knowledge_base/<资源目录>/。

    冻结原则（D10）：目标已存在且 force=False → 跳过（不自动覆盖）；
    force=True（手动"重新同步原文"）→ 覆盖。复制后跑一致性校验。

    **陈旧刷新（2026-09-12 用户实测）**：`document.json`/`en.md`/`images` 是**派生**产物，
    重解析或复核写回（`review_service` → `_post_parse_clean` → `sanitize_document`）会重写
    library 侧。旧实现"存在即跳过"会让 kb 永远停在旧快照上 ⇒
      ① 重新编译读到旧正文；② 问答前缀（kb 快照）与编译实际发送的字节仍可能不一致。
    故新增：源比目标新时**刷新**派生副本（用户手改过的目标受 `kb_edited` 保护，不覆盖）。

    P0-B step4：键不再限于 DOI——RID / 目录名 / md5 目录（无 DOI 文献）都能解析；
    目标目录名沿用**library 侧真实目录名**（保证两侧一致、不产生平行目录）。
    """
    from .resource import find_doc, resources_dir

    key = (doi or "").strip()
    src = resources_dir(roots.library_dir, key, store)
    if src is None:
        # 兜底：无 DOI 文献的目录名可能是 md5（与 nd- 键无字面关系）→ 用文档内容反查
        doc = find_doc(roots.library_dir, key, store, search_root=roots.library_dir)
        src = doc.parent if doc is not None else None
    if src is None:
        # 兼容旧行为：DOI 键按规则算目录名（library 可能尚未创建）
        src = roots.library_dir / doi_to_dirname(normalize_doi(key))
    if not src.exists():
        raise FileNotFoundError(f"library 无该文献目录: {src}")
    dst = resources_dir(roots.kb_dir, key, store) or (roots.kb_dir / src.name)
    copied: list[str] = []
    skipped: list[str] = []
    refreshed: list[str] = []
    for name, is_dir in _KB_SOURCE_ITEMS:
        s = src / name
        if not s.exists():
            continue
        t = dst / name
        if t.exists() and not force:
            if not _is_stale(s, t):
                skipped.append(name)
                continue
            # 用户在阅读器里手改过 → 用户资产优先，不覆盖（P2）
            if store is not None and hasattr(store, "is_kb_edited") \
                    and store.is_kb_edited(f"{dst.name}/{name}"):
                skipped.append(name)
                logger.info("kb 副本陈旧但用户手改过，保留: %s/%s", dst.name, name)
                continue
            refreshed.append(name)
        else:
            copied.append(name)
        if is_dir:
            if t.exists() and force:
                shutil.rmtree(t, ignore_errors=True)   # 显式重同步才清理目录
            t.mkdir(parents=True, exist_ok=True)
            shutil.copytree(s, t, dirs_exist_ok=True)
        else:
            t.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, t)
    verify = None
    if (dst / "document.json").exists() and (dst / "en.md").exists():
        verify = verify_kb_doc(doi, roots, base="kb")
    # G1 闸门（2026-09-16 方案 A）：**纳入 kb 后必须"原文层四件齐全"**，否则大声告警。
    # 背景：kb 缺 `source.pdf` 曾是真实用户报障（审计 C2/C8；根因是"谁先建 kb 谁定局"的执行时序），
    # 没有闸门时只能靠肉眼发现。这里只**告警不抛错**：source.pdf 在部分老数据里确实合法缺失
    # （纯 md 导入、用户删除），抛错会把整条流水线打死；完整性进返回值，由上层日志/事件暴露。
    missing: list[str] = []
    try:
        st = source_status(doi, roots, store=store)
        kb_files = (st or {}).get("kb") or {}
        missing = [k for k in ("document.json", "en.md", "source.pdf") if not kb_files.get(k)]
    except Exception as e:  # noqa: BLE001 - 闸门自身失败不影响纳入结果
        logger.warning("纳入 kb 完整性检查失败（忽略）: %s", e)
    if missing:
        logger.warning("⚠ 纳入 kb 后仍缺文件 %s（key=%s, kb=%s）——检查解析是否产出该文件、"
                       "以及 _ensure_library_source_pdf 是否在纳入之前执行", missing, key, dst)
    return {"doi": doi, "copied": copied, "skipped": skipped,
            "refreshed": refreshed, "kb_dir": str(dst), "verify": verify,
            "missing": missing}


def resync_source(doi: str, roots: Roots) -> dict:
    """手动"重新同步原文"（用户拍板：默认不动，手动才覆盖，D10/D14）。"""
    return sync_source_to_kb(doi, roots, force=True)


def source_status(doi: str, roots: Roots, store=None) -> dict:
    """library/kb 两侧原文层四件存在性（P0-B step4：键可为 RID/目录名/md5 目录）。"""
    from .resource import candidate_dirnames, resources_dir

    key = (doi or "").strip()
    lib_hit = resources_dir(roots.library_dir, key, store)
    kb_hit = resources_dir(roots.kb_dir, key, store)
    names = candidate_dirnames(key, store)
    d = (lib_hit.name if lib_hit else (kb_hit.name if kb_hit else
                                       (names[1] if len(names) > 1 and names[0].startswith("doi-")
                                        else (names[0] if names else ""))))

    def files(base: Path, hit) -> dict:
        b = hit if hit is not None else (base / d if d else base / "_none")
        return {"exists": b.exists(),
                "source.pdf": (b / "source.pdf").exists(),
                "en.md": (b / "en.md").exists(),
                "document.json": (b / "document.json").exists(),
                "images": (b / "images").is_dir()}
    return {"doi": key, "dir": d,
            "library": files(roots.library_dir, lib_hit),
            "kb": files(roots.kb_dir, kb_hit)}


def kb_status(roots: Roots) -> list[dict]:
    """全部 kb/<DOI>/ 目录状态（原文层四件 + 编译产物）——前端同步总览。"""
    out: list[dict] = []
    if roots.kb_dir.exists():
        for d in sorted(roots.kb_dir.iterdir()):
            # 跳过非文献目录："_" 前缀（_index.md/_qa/_reports 等应用产物）与
            # "." 前缀（.obsidian 用户 vault 配置、.system 主库/暂存）——
            # 2026-09-11 P0-A：此前只过滤 "_"，导致 .obsidian/.system 被当成
            # 文献目录出现在知识库列表与对账结果里。
            if not d.is_dir() or d.name.startswith(("_", ".")):
                continue
            out.append({
                "dir": d.name,
                "source.pdf": (d / "source.pdf").exists(),
                "en.md": (d / "en.md").exists(),
                "document.json": (d / "document.json").exists(),
                "images": (d / "images").is_dir(),
                "note": (d / "_note.md").exists(),
                "details": (d / "_details.md").exists(),
                "wiki": (d / "_wiki.md").exists(),
            })
    return out


# ---------------------------------------------------------------- 一致性校验

def verify_kb_doc(doi: str, roots: Roots, base: str = "kb", store=None) -> dict:
    """en.md ↔ document.json 一致性校验（D17）。

    对齐规则（与 render_clean 同构）：
    - 两侧跳过：References 段（doc 侧 section=="References"/is_reference；md 侧
      References 标题之后）、纯图行（![](...)）、空块
    - 文本归一化：剥 "#+" 前缀、空白塌缩
    - 装饰标题等价："A B S T R A C T" ↔ "Abstract"（render_clean 的 title 化规则）
    """
    from .resource import resources_dir

    key = (doi or "").strip()
    base_dir = (roots.kb_dir if base == "kb" else roots.library_dir)
    d = resources_dir(base_dir, key, store) or (base_dir / doi_to_dirname(normalize_doi(key)))
    dj = d / "document.json"
    em = d / "en.md"
    if not dj.exists() or not em.exists():
        return {"ok": False, "error": "缺少 document.json 或 en.md",
                "expected_count": 0, "found_count": 0, "mismatches": []}
    try:
        data = json.loads(dj.read_text(encoding="utf-8", errors="replace"))
    except ValueError as e:
        return {"ok": False, "error": f"document.json 解析失败: {e}",
                "expected_count": 0, "found_count": 0, "mismatches": []}
    expected = _doc_blocks(data)
    found = _md_blocks(em.read_text(encoding="utf-8", errors="replace"))
    mismatches: list[dict] = []
    n = min(len(expected), len(found))
    for i in range(n):
        pid, etext = expected[i]
        ftext = found[i]
        if not _equiv(etext, ftext):
            mismatches.append({"index": i, "para_id": pid,
                               "expected": etext[:100], "found": ftext[:100]})
            if len(mismatches) >= 10:
                break
    ok = (len(mismatches) == 0 and len(expected) == len(found))
    return {"ok": ok, "expected_count": len(expected), "found_count": len(found),
            "mismatches": mismatches}


def _doc_blocks(data: dict) -> list[tuple[str, str]]:
    """document.json paragraphs → [(para_id, 归一化文本)]（跳过 refs/空段）。"""
    out: list[tuple[str, str]] = []
    for p in data.get("paragraphs") or []:
        t = (p.get("text_en") or "").strip()
        if not t:
            continue
        sec = p.get("section") or ""
        if sec.lower() == "references" or p.get("is_reference") \
                or _REF_ENTRY_RE.match(t):
            continue
        out.append((p.get("para_id") or "?", _norm(t)))
    return out


def _md_blocks(md_text: str) -> list[str]:
    """en.md → 归一化文本块（跳过纯图行/References 区/空块）。"""
    out: list[str] = []
    refs_zone = False
    for b in re.split(r"\n\s*\n", md_text):
        if not b.strip():
            continue
        lines = [ln for ln in b.splitlines() if ln.strip()]
        first = lines[0].strip() if lines else ""
        if _IMAGE_LINE_RE.match(first) and len(lines) == 1:
            continue
        if re.match(r"^#{1,6}\s+references\s*$", first, re.I):
            refs_zone = True
            continue
        if refs_zone:
            if re.match(r"^#{1,6}\s+\S", first):
                refs_zone = False            # 新章节 → 退出 References 区
            else:
                continue
        if _REF_ENTRY_RE.match(first):
            continue
        out.append(_norm(b))
    return out


def _norm(text: str) -> str:
    """剥行首 # 标记 + 空白塌缩（heading 级别差异不参与一致性判定）。"""
    t = re.sub(r"^\s*#{1,6}\s*", "", text, flags=re.M).strip()
    return re.sub(r"\s+", " ", t).strip()


def _equiv(a: str, b: str) -> bool:
    if a == b:
        return True
    # 装饰标题等价（render_clean：去空格 title()）
    if _DECORATIVE_RE.match(a) or _DECORATIVE_RE.match(b):
        da = "".join(a.split()).title()
        db = "".join(b.split()).title()
        return da == db or da == b or db == a
    return False
