# -*- coding: utf-8 -*-
"""依附资料（SI / 审稿意见 / 书章 / 数据）的落位、列取、安全读取与索引（P0-B step3）。

用户模型（2026-09-11 拍板）：
- **依附资料挂父资源目录**：`library/<父资源目录>/attachments/{si,review,data}/`；
- **无父资源**的零散资料才放独立根：`attachments/<RID>/<kind>/`；
- 资料是用户资料：任何清理都要过 `layout.assert_safe_to_clear`。

本模块只依赖 `paperkb.layout`/`doi` 与传入的 Roots，不 import backend。

索引约定（A5）：附件文本进 `notes_fts`，`doi 列 = 父 RID`，`filename 列 = 附件相对路径`
（`attachments/si/xxx.md`）——这样 `notes_for(rid)` 单篇查询照旧可用，召回结果又自带
"是哪个附件"的信息，`read_attachment` 可直接用该相对路径读回。
"""
from __future__ import annotations

import hashlib
import mimetypes
import re
from pathlib import Path

from . import layout
from .doi import doi_to_dirname, is_doi, make_rid
from .textseg import boundary_trim

# 文本抽取上限（防超大 PDF/日志把索引撑爆）
TEXT_MAX_CHARS = 200_000
# 单附件大小上限（导入拒绝，防误传巨型数据包）
MAX_FILE_BYTES = 200 * 1024 * 1024

_TEXT_SUFFIXES = (".md", ".markdown", ".txt", ".csv", ".json", ".bib", ".tex",
                  ".xml", ".yaml", ".yml", ".log", ".tsv", ".rst", ".py", ".r")
_PDF_SUFFIXES = (".pdf",)
_DOCX_SUFFIXES = (".docx",)
_UNSAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(name: str, fallback: str = "attachment.bin") -> str:
    """附件文件名清洗（去路径分隔与穿越，保留中文/空格）。"""
    base = Path((name or "").replace("\\", "/")).name
    base = _UNSAFE_NAME.sub("_", base).strip().strip(".")
    if not base or base in (".", ".."):
        return fallback
    return base[:120]


# ---------------------------------------------------------------- 资源根解析
def library_candidates(roots, rid: str) -> list[Path]:
    """RID / 键 → 候选资源目录名（**顺序即优先级**；不猜不存在的路径）。

    三种写法都必须命中同一目录，否则附件会落到平行目录（用户两处都找不到）：
    - RID：`doi-10.1002_adma…`（make_rid 产物）
    - **裸 DOI**：`10.1002/adma…`（前端按 DOI 传、历史调用方一律传 DOI）
    - 目录名：`10.1002_adma…`（doi_to_dirname 产物）
    """
    r = (rid or "").strip()
    cands: list[str] = []
    if r:
        cands.append(r)
        if r.startswith("doi-"):
            rest = r[4:]
            cands.append(rest)                     # 已按目录名书写的历史 RID
            cands.append(doi_to_dirname(rest))     # 规范 DOI → 目录名
        elif is_doi(r):
            cands.append(doi_to_dirname(r))        # 裸 DOI（含 /）→ 目录名
    out: list[Path] = []
    seen: set[str] = set()
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(Path(roots.library_dir) / c)
    return out


def resource_dir(roots, rid: str) -> Path:
    """资源的真实根目录（存在优先；都不存在时返回首选路径，不落盘）。"""
    cands = library_candidates(roots, rid)
    for c in cands:
        if c.is_dir():
            return c
    return cands[0] if cands else Path(roots.library_dir) / (rid or "nd-untitled")


def standalone_dir(roots, rid: str) -> Path:
    """无父资源资料的目录根：`<项目根>/attachments/<RID>`。"""
    lib = Path(roots.library_dir)
    attach_root = lib.parent / layout.STANDALONE_ATTACH_ROOT
    return attach_root / ((rid or "").strip() or "nd-untitled")


def attachment_root(roots, rid: str) -> tuple[Path, bool]:
    """附件落位根 + 是否挂在父资源下。

    返回 `(path, parented)`：父资源目录存在 → `(library/<父目录>/attachments, True)`；
    否则 → `(attachments/<RID>, False)`。
    """
    for c in library_candidates(roots, rid):
        if c.is_dir():
            return layout.attachments_dir(c), True
    return standalone_dir(roots, rid), False


# ---------------------------------------------------------------- 列取
def _iter_files(base: Path) -> list[dict]:
    """列 `<base>/**` 下的文件；kind = 相对路径首段（si/review/data…）。"""
    out: list[dict] = []
    if not base.is_dir():
        return out
    for p in sorted(base.rglob("*")):
        if not p.is_file() or p.name.startswith("."):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        rel = p.relative_to(base).as_posix()
        seg = rel.split("/")
        out.append({"path": rel, "name": p.name, "size": st.st_size,
                    "mtime": st.st_mtime,
                    "kind": (seg[0] if len(seg) > 1 else "data"),
                    "suffix": p.suffix.lower()})
    return out


def list_attachments(roots, rid: str) -> dict:
    """列某资源的全部附件（父目录优先；独立根仅作后备，都有则合并去重）。"""
    r = (rid or "").strip()
    files: list[dict] = []
    seen: set[str] = set()
    base, parented = attachment_root(roots, r)
    for item in _iter_files(base):
        if item["path"] not in seen:
            seen.add(item["path"])
            files.append(item)
    if parented:
        alt = standalone_dir(roots, r)
        for item in _iter_files(alt):
            if item["path"] not in seen:
                seen.add(item["path"])
                files.append(item)
    by_kind: dict[str, int] = {}
    for f in files:
        by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + 1
    return {"rid": r, "ok": True, "root": str(base if parented else standalone_dir(roots, r)),
            "parented": parented, "count": len(files), "by_kind": by_kind,
            "files": files}


# ---------------------------------------------------------------- 安全路径解析
def resolve_attachment(roots, rid: str, rel: str) -> Path | None:
    """相对路径 → 真实文件（**白名单校验**，越权一律返回 None）。

    规则：拒绝绝对路径 / `..` / 盘符；解析后必须落在该资源的 `attachments/`
    （或独立根 `<RID>/`）之内。符号链接按解析后路径判定。
    """
    raw = (rel or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        return None
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    # 索引/召回返回的是库内路径（`attachments/si/x.md`），而列表返回 `si/x.md`：
    # 两种写法都接受，这里统一剥掉前导 `attachments/`（比较基准本来就是 attachments 根）。
    while parts and parts[0] == layout.STANDALONE_ATTACH_ROOT and len(parts) > 1:
        parts = parts[1:]
    bases = [attachment_root(roots, rid)[0], standalone_dir(roots, rid),
             Path(roots.library_dir).parent / layout.STANDALONE_ATTACH_ROOT]
    for base in bases:
        try:
            rb = base.resolve()
            cand = (rb / Path(*parts)).resolve()
        except OSError:
            continue
        if cand.is_file() and (cand == rb or rb in cand.parents):
            return cand
    return None


def read_attachment(roots, rid: str, rel: str, max_chars: int = 20000,
                    offset: int = 0) -> dict:
    """读附件文本片段（受白名单约束；二进制返回可读说明而非乱码）。

    `offset`：起始字符位置（**支持续读**——旧实现只能看前 max_chars 字，长 SI 后段
    永远读不到）；切点按行/句边界收尾，不把句子拦腰截断。
    返回 `next_offset`（=0 表示已到末尾）。
    """
    p = resolve_attachment(roots, rid, rel)
    if p is None:
        return {"ok": False, "error": f"附件不存在或路径未被允许: {rel}"}
    text = extract_text(p)
    if text is None:
        return {"ok": False, "error": f"非文本附件（{p.suffix or '未知类型'}），无法按文本读取",
                "path": rel, "size": p.stat().st_size}
    n = max(200, int(max_chars or 20000))
    off = max(0, int(offset or 0))
    seg = text[off:]
    piece = seg if len(seg) <= n else boundary_trim(seg, n)
    end = off + len(piece)
    return {"ok": True, "rid": rid, "path": rel, "chars": len(text),
            "offset": off, "next_offset": end if end < len(text) else 0,
            "truncated": end < len(text), "text": piece}


# ---------------------------------------------------------------- 文本抽取（零 API 成本）
def extract_text(path: Path) -> str | None:
    """抽取纯文本；不支持的二进制返回 None。**纯本地、不调 LLM。**"""
    suf = path.suffix.lower()
    try:
        if suf in _TEXT_SUFFIXES or suf == "":
            return path.read_text(encoding="utf-8", errors="replace")[:TEXT_MAX_CHARS]
        if suf in _PDF_SUFFIXES:
            try:
                import fitz  # PyMuPDF
            except ImportError:
                return None
            parts: list[str] = []
            with fitz.open(str(path)) as doc:
                for page in doc:
                    parts.append(page.get_text())
                    if sum(len(x) for x in parts) > TEXT_MAX_CHARS:
                        break
            return "\n".join(parts)[:TEXT_MAX_CHARS]
        if suf in _DOCX_SUFFIXES:
            try:
                import docx  # python-docx
            except ImportError:
                return None
            d = docx.Document(str(path))
            return "\n".join(p.text for p in d.paragraphs)[:TEXT_MAX_CHARS]
    except Exception:  # noqa: BLE001 - 抽取失败不阻断导入（文件已落位）
        return None
    return None


# ---------------------------------------------------------------- 导入
def import_attachment(roots, rid: str, kind: str, filename: str,
                      data: bytes, store=None, index: bool = True) -> dict:
    """轻量导入一份附件：落位 → 抽取文本 → 索引（**0 API 成本**）。

    - kind 限定在 `layout.ATTACHMENT_KINDS`（si/review/data/note/…），其余归 `data`；
    - 同名重复导入 → 自动加 `-2`/`-3` 后缀（**不覆盖用户已有资料**）；
    - `store` 给出时把文本写进 `notes_fts`（key=父 RID，filename=附件相对路径）。
    """
    r = (rid or "").strip()
    if not r:
        return {"ok": False, "error": "缺少父资源（rid）"}
    k = (kind or "data").strip().lower()
    if k not in layout.ATTACHMENT_KINDS:
        k = "data"
    if not data:
        return {"ok": False, "error": "空文件"}
    if len(data) > MAX_FILE_BYTES:
        return {"ok": False, "error": f"文件过大（>{MAX_FILE_BYTES // (1024 * 1024)}MB）"}

    base, parented = attachment_root(roots, r)
    target_dir = base / k
    target_dir.mkdir(parents=True, exist_ok=True)
    name = safe_filename(filename)
    target = target_dir / name
    n = 2
    while target.exists():
        target = target_dir / f"{Path(name).stem}-{n}{Path(name).suffix}"
        n += 1
    target.write_bytes(data)
    rel = target.relative_to(base).as_posix()

    text = extract_text(target)
    indexed = False
    if index and store is not None and text:
        try:
            # 索引里的 filename 用库内路径 `attachments/<kind>/<名>`（父资源目录为基准）：
            # 与 kb_recall 的 file 字段、db._index_attachment_dir 全量重扫保持一致。
            store.index_attachment(r, f"attachments/{rel}", text)
            indexed = True
        except Exception:  # noqa: BLE001 - 索引失败不影响落位
            indexed = False
    return {"ok": True, "rid": r, "kind": k, "path": rel, "name": target.name,
            "bytes": len(data), "parented": parented, "root": str(base),
            "text_chars": len(text or ""), "indexed": indexed,
            "md5": hashlib.md5(data).hexdigest()[:12]}


def rid_for_upload(kind: str, *, doi: str = "", isbn: str = "", cnki: str = "",
                   cstr: str = "", arxiv: str = "", report_no: str = "",
                   fingerprint: str = "", parent_rid: str = "") -> str:
    """按类型生成 RID（薄封装 `doi.make_rid`，供 API 层调用）。"""
    return make_rid(kind, doi=doi, isbn=isbn, cnki=cnki, cstr=cstr, arxiv=arxiv,
                    report_no=report_no, fingerprint=fingerprint,
                    parent_rid=parent_rid)
