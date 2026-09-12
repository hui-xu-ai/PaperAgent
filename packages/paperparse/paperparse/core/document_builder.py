#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: skill/src/paperparse/core/document_builder.py
功能: S6 文档构建：DOI 文件夹命名规则（/→_）、参考文献解析（[n] 标记分组）、
      汇总单一事实源 ArticleDocument（图注↔图片关联、audit 摘要）、
      文档级净化：片段重复检查 dedupe_paragraphs（用户问题 2）
对外接口: doi_dir_name / extract_references / build_document / save_document / load_document / dedupe_paragraphs
版本: v1.3.0 (2026-08-19)
版本历史:
  v1.3.0 去重统一为"全文段落重复清理+删除清单反馈"：dedupe_paragraphs 输出删除清单 log
          （removed/kept/reason/section），build_document 记录到 audit.warnings
  v1.2.0 强化 dedupe_paragraphs：近重复检测（词集 Dice≥0.92）+ Abstract 位置锚定
          （保留 ## Abstract 后的正确副本，删除错位前置副本）
  v1.1.0 新增 dedupe_paragraphs（长正文段归一化去重，build_document 自动调用）
  v1.0.0 初始版本
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from paperparse.middleware.errors import PaperError
from paperparse.middleware.schema import (
    ArticleDocument,
    ArticleMetadata,
    Figure,
    LayoutInfo,
    Paragraph,
    Reference,
    StitchResult,
    TextBlock,
    validate_doc,
)

__all__ = ["doi_dir_name", "extract_references", "build_document",
           "save_document", "load_document", "dedupe_paragraphs",
           "mark_abstract_section"]

_REF_START_RE = re.compile(r"^\[(\d+)\]")
_BOILER = ("wiley-vch", "downloaded from", "www.advancedsciencenews")


def doi_dir_name(doi: str | None) -> str:
    """[全局] DOI → 文件夹名（/ → _）；无 DOI 时回退 paper_<hash8>（命名规则接口，可替换）"""
    if doi and re.fullmatch(r"10\.\d{4,9}/[\w.\-()/:;]+", doi):
        return doi.replace("/", "_")
    digest = hashlib.sha256((doi or "unknown").encode("utf-8")).hexdigest()[:8]
    return "paper_%s" % digest


def _pdf_md5_dirname(pdf_path: str | None) -> str:
    """[全局] PDF 全文 md5 作为目录名（用户决策：无 DOI 用 md5(PDF 全文) 命名目录，
    兼容同一 PDF 不同文件名、防重复）。读文件哈希；失败（文件不存在/IO）回退文件名尾 8 位。"""
    if pdf_path:
        p = Path(pdf_path)
        try:
            if p.exists():
                digest = hashlib.md5(p.read_bytes()).hexdigest()
                if len(digest) >= 8:
                    return digest  # 32 位 md5 作为目录名（稳定、跨文件名一致）
        except OSError:  # noqa: BLE001 - 读失败回退文件名
            pass
    # 回退：无 md5 时用文件名净化（保底）
    stem = Path(pdf_path).stem if pdf_path else ""
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" .")
    stem = re.sub(r"[^\w.\-]", "_", stem)
    return stem[:120] or "paper"


def output_dir_name(doi: str | None, pdf_path: str | None = None) -> str:
    r"""[全局] 输出目录/变体命名统一规则（P2-3 需求；2026-08-29 T5：无 DOI 改 md5 命名）：
    有效 DOI → DOI（/ → _，限长 120）；**无 DOI → md5(PDF 全文)**（用户决策：无 DOI 时
    用 md5 命名目录，兼容各种 PDF，同内容不同文件名不重复），读不到 md5 再回退文件名。

    净化规则：非法文件名字符 [<>:"/\\|?*] 与控制符 → "_"，首尾空格/点去除，
    其余非 [\w.\-] → "_"，超长截断。
    """
    if doi and re.fullmatch(r"10\.\d{4,9}/[\w.\-()/:;]+", doi):
        return doi.replace("/", "_")[:120]
    if pdf_path:
        return _pdf_md5_dirname(pdf_path)
    return "paper"


def extract_references(blocks: list[TextBlock], layout: LayoutInfo) -> list[Reference]:
    """[全局] 从参考文献区解析引用条目：按 "[n]" 标记切分（块内可能含多个引用），无标记碎片并入前条

    参数:
        blocks: 文本块（reading_order 后）
        layout: LayoutInfo（reference_zone_pages）
    返回:
        按编号排序的 Reference 列表（可能为空 → 调度层记 PAPER-0103 警告）
    """
    ref_pages = set(layout.reference_zone_pages)
    if not ref_pages:
        return []
    ordered = sorted((b for b in blocks
                      if b.page in ref_pages and b.order is not None),
                     key=lambda b: b.order)

    chunks: list[str] = []
    for b in ordered:
        t = b.text.strip()
        if not t or any(x in t.lower() for x in _BOILER):
            continue
        chunks.append(t)
    text = " ".join(chunks)

    refs: list[Reference] = []
    for part in re.split(r"(?=\[\d+\])", text):
        part = part.strip()
        if not part:
            continue
        m = _REF_START_RE.match(part)
        if m:
            num = int(m.group(1))
            refs.append(Reference(ref_id="R%d" % num, number=num, raw_text=part))
        elif refs:                       # 无 [n] 前缀的碎片（跨块续接）并入前一条
            refs[-1].raw_text = (refs[-1].raw_text + " " + part).strip()

    refs.sort(key=lambda r: r.number)
    return refs


def build_document(metadata: ArticleMetadata,
                   stitch_result: StitchResult,
                   figures: list[Figure] | None = None,
                   references: list[Reference] | None = None,
                   run_id: str = "",
                   warnings: list[str] | None = None) -> ArticleDocument:
    """[全局] 汇总单一事实源 ArticleDocument

    参数:
        metadata: S5 产物
        stitch_result: S3 产物（段落）
        figures: S4 产物（默认空）
        references: 参考文献（默认空）
        run_id: 本次运行 ID（审计关联）
        warnings: 文档级警告摘要（不含全文）
    返回:
        ArticleDocument
    """
    doc = ArticleDocument(
        metadata=metadata,
        paragraphs=stitch_result.paragraphs,
        figures=figures or [],
        references=references or [],
    )
    doc.audit.run_id = run_id
    doc.audit.warnings = list(warnings or [])
    # 文档级净化（用户问题：摘要特征识别 + 全文重复段清理 + 删除清单反馈）
    # 先标记摘要段（section="Abstract"），使去重时能"位置锚定"：
    # 近重复片段中优先保留 ## Abstract 后的正确位置副本，删除错位副本。
    marked = mark_abstract_section(doc)
    if marked:
        doc.audit.warnings.append("摘要段特征识别 %d 段" % marked)
    dedup_log: list[dict] = []
    deduped = dedupe_paragraphs(doc.paragraphs, log=dedup_log)
    if deduped:
        doc.audit.warnings.append("段落重复清理 %d 段: %s" % (
            deduped, ", ".join(r["removed"] for r in dedup_log)))
    doc.audit.warnings.append("删除清单: %s" % _json_dumps(dedup_log) if dedup_log
                              else "删除清单: 无重复删除")
    return doc


def _json_dumps(obj) -> str:
    """[局部] json.dumps（UTF-8 安全）"""
    import json as _json
    return _json.dumps(obj, ensure_ascii=False)


_DEDUPE_LIG = str.maketrans({"\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl"})
_DEDUPE_MIN = 200          # 参与重复检查的最小段落长度（排除标题/短句）
_DEDUPE_SIM = 0.90         # 近重复判定阈值（归一化词集 Dice 相似度；与 mark_abstract 一致）
_LATEX_RE = [
    re.compile(r"\$\$.*?\$\$", re.S),
    re.compile(r"\$.*?\$", re.S),
    re.compile(r"\\\(.*?\\\)", re.S),
    re.compile(r"\\\[.*?\\\]", re.S),
]


def _dedupe_key(text: str) -> str:
    """[局部] 段落重复判定键：剥离 LaTeX/HTML → 连字展开 → 小写 → 折叠空白

    v2.2: 增强剥离 LaTeX 命令/花括号/上下标（mineru 融合版含 \\mathrm{}、^_ 等残留，
    只剥 $..$ 会导致融合后摘要段与 metadata.abstract 归一化不等、mark_abstract_section 失效）。
    """
    t = text.translate(_DEDUPE_LIG)
    t = re.sub(r"<[^>]+>", "", t)
    for pat in _LATEX_RE:
        t = pat.sub(" ", t)
    t = re.sub(r"\\[a-zA-Z]+", " ", t)      # \mathrm \mathsf 等 LaTeX 命令
    t = re.sub(r"[{}^_~]", " ", t)          # 花括号/上下标/波浪号
    t = re.sub(r"\s+", " ", t).strip().lower()
    return t


def _dedupe_tokens(key: str) -> frozenset[str]:
    """[局部] 归一化键 → 词集合（近重复相似度用）；过滤纯数字/符号 token：
    OCR 空格差异（±0.5 vs ± 0.5、μm vs μ m、90% vs >90%）会拆出不同数字 token，
    剔除后摘要等长段相似度不被数字噪声拉低，mark_abstract_section/dedupe 恢复命中。"""
    return frozenset(w for w in key.split() if re.search(r"[a-z]", w))


def _dedupe_dice(a: frozenset[str], b: frozenset[str]) -> float:
    """[局部] 两个词集合的 Dice 相似度（0~1；全空集合视为 1）"""
    if not a and not b:
        return 1.0
    inter = len(a & b)
    return 2.0 * inter / (len(a) + len(b)) if (len(a) + len(b)) else 0.0


def dedupe_paragraphs(paragraphs: list[Paragraph], log: list[dict] | None = None) -> int:
    """[全局] 片段重复性检查（用户问题 2 + 强化）：
      **长正文段**（>200 字符，排除标题/图注/短句）归一化后检测两类重复——
        级1（精确）：归一化后完全相同 → 保留正确位置副本，删除错位重复段；
        级2（近重复）：归一化词集 Dice ≥ 阈值（默认 0.90）的大段 → 判定为识别重复。
      **位置锚定（保留正确位置）**：重复冲突中若含 section=="Abstract"（## Abstract 后的
      正确位置）副本 → 保留摘要副本、删除其它错位副本；否则保留最先出现、删除后续重复。
      **删除清单反馈**：每次删除写入 log=[{"removed","kept","reason","section"}]，
      供用户/审计查看删除了哪些内容。

    参数:
        paragraphs: 段落列表（原地删除重复段；section 需先经 mark_abstract_section）
        log: 可选；接收删除清单（每项 {"removed":para_id,"kept":para_id,"reason":str,"section":str}）
    返回:
        删除的段落数
    """
    seen: dict[str, str] = {}               # 归一化键 → 保留的 para_id
    seen_tokens: dict[str, frozenset[str]] = {}
    kept_by_id: dict[str, int] = {}         # para_id → out 索引（摘要顶替用）
    removed = 0
    out: list[Paragraph] = []

    def _record(removed_id: str, kept_id: str, reason: str, section: str) -> None:
        nonlocal removed
        removed += 1
        if log is not None:
            log.append({"removed": removed_id, "kept": kept_id,
                        "reason": reason, "section": section})

    for p in paragraphs:
        if p.is_heading or p.is_caption:
            out.append(p)
            continue
        key = _dedupe_key(p.text_en)
        if len(key) < _DEDUPE_MIN:
            out.append(p)
            continue

        dup_of: str | None = None
        if key in seen:
            dup_of = seen[key]
        else:
            new_tokens = _dedupe_tokens(key)
            for k, kt in seen_tokens.items():
                if _dedupe_dice(new_tokens, kt) >= _DEDUPE_SIM:
                    dup_of = k
                    break

        if dup_of is None:
            seen[key] = p.para_id
            seen_tokens[key] = _dedupe_tokens(key)
            kept_by_id[p.para_id] = len(out)
            out.append(p)
            continue

        # 位置锚定：摘要副本（正确位置）优先于已保留的错位副本
        if p.section == "Abstract":
            idx = kept_by_id.get(dup_of)
            if idx is not None:
                out[idx] = p                # 用摘要副本顶替错位副本
                kept_by_id[p.para_id] = idx
                del kept_by_id[dup_of]
                _record(dup_of, p.para_id, "摘要错位副本（应位于 ## Abstract 后）→ 删除错位、保留正确位置摘要",
                        p.section)
            else:
                _record(p.para_id, dup_of, "摘要重复副本 → 删除", p.section)
            continue

        _record(p.para_id, dup_of, "与先前段落近/完全重复 → 删除位置靠后的重复段", p.section)
    paragraphs[:] = out
    return removed


def mark_abstract_section(doc: ArticleDocument) -> int:
    """[全局] 摘要段特征识别（用户问题 1）：与 metadata.abstract 归一化**相近**的首页正文段
    → section 标记为 "Abstract"（摘要段明确归属 "## Abstract" 之后；渲染层已有去重，
    不会重复输出）

    v2.2: 匹配由"归一化完全相等"放宽为"归一化词集 Dice ≥ 0.9"——LaTeX 融合
    （build_mineru_doc）后摘要段 text_en 为 mineru 版（含 $/\\mathrm 等），与
    metadata.abstract（本地版）不再完全相等，原精确匹配导致摘要段未标记、
    渲染去重失效、摘要重复（用户复现）。

    参数:
        doc: ArticleDocument（原地修改段落 section）
    返回:
        标记的段落数
    """
    an = _dedupe_key(doc.metadata.abstract or "")
    if not an:
        return 0
    a_tokens = _dedupe_tokens(an)
    n = 0
    for p in doc.paragraphs:
        if p.is_heading or p.is_caption:
            continue
        pt = _dedupe_tokens(_dedupe_key(p.text_en))
        if pt and _dedupe_dice(pt, a_tokens) >= 0.9:   # 宽松匹配：容 LaTeX 融合差异
            p.section = "Abstract"
            n += 1
    return n


def save_document(doc: ArticleDocument, path: str | Path) -> Path:
    """[全局] document.json 落盘（UTF-8）"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc.model_dump(mode="json"), ensure_ascii=False, indent=1),
                 encoding="utf-8")
    return p


def load_document(path: str | Path) -> ArticleDocument:
    """[全局] 从 document.json 恢复（翻译/重渲染用）"""
    p = Path(path)
    if not p.exists():
        raise PaperError("PAPER-0001", stage="S6", path=str(p))
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return validate_doc(data)
    except Exception as exc:
        raise PaperError("PAPER-0501", stage="S6", path=str(p),
                         detail={"reason": f"document.json 解析失败: {exc}"}) from exc
