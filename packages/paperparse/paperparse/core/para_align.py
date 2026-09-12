#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: skill/src/paperparse/core/para_align.py
功能: T-A 段落级文字匹配校准（"自我学习强化"的眼睛）：
      输入两个 Markdown（自动版 paper.md vs 手动校准版 paper_手动校准版.md），
      段落级对齐（按空行/标题分块，复用 calibration_md.normalize + Dice 相似度），
      输出对齐报告：匹配段落 / 不匹配区域（错位/缺段/多段）/ 差异精确定位到句子与字符
      （difflib.SequenceMatcher opcodes；中文与英文均处理）。
      报告渲染与缺陷清单在 para_align_report.py（报告层，本模块拆分）。
对外接口: parse_md_paras / align_docs
版本: v1.1.0 (2026-08-18)
版本历史:
  v1.1.0 报告/缺陷清单拆分至 para_align_report.py（单文件 ≤400 行规范）
  v1.0.0 初始版本（T-A）
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from paperparse.core.calibration_md import normalize

__all__ = ["parse_md_paras", "align_docs", "strip_latex"]

MAX_MD_BYTES = 8 * 1024 * 1024      # 大小守卫（同 calibration_md）
MATCH_THRESHOLD = 0.45              # Dice 匹配阈值（同 calibration_md）
DIFF_CONTEXT = 40                   # 差异片段上下文宽度（字符）

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_FRONTMATTER_RE = re.compile(r"^---\s*$")
_CALLOUT_RE = re.compile(r"^>\s?")
_IMAGE_RE = re.compile(r"^!\[[^\]]*\]\([^)]*\)\s*$")
_BOILER_RE = re.compile(
    r"^(search for more papers by this author|full access|research article|"
    r"volume \d+|issue \d+|received:|revised:|accepted:|published online|"
    r"© \d{4}|downloaded from https?://|www\.\S+)", re.IGNORECASE)
# 英文句子边界（句号/问号/感叹号 + 空白 + 大写或数字开头）；中文句号/问号/感叹号
_SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(])|(?<=[。！？])")
# LaTeX 公式片段（自动版高精度解析产出；校准版无 LaTeX → 匹配前剥离，避免全字段误报）
_LATEX_RE = [
    re.compile(r"\$\$.*?\$\$", re.S),
    re.compile(r"\$.*?\$", re.S),
    re.compile(r"\\\(.*?\\\)", re.S),
    re.compile(r"\\\[.*?\\\]", re.S),
    re.compile(r"\\begin\{[a-zA-Z*]+\}.*?\\end\{[a-zA-Z*]+\}", re.S),
]


def strip_latex(text: str) -> str:
    """[全局] 剥离 LaTeX 公式片段（$..$ / $$..$$ / \\(..\\) / \\[..\\] / \\begin..\\end），
    供对齐匹配前归一化（自动版含 LaTeX 公式、校准版无 → 全字段比较会误报差异）"""
    t = text
    for pat in _LATEX_RE:
        t = pat.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


@dataclass
class ParaItem:
    """[全局] 解析出的一个段落（正文或标题）"""
    idx: int = 0
    is_heading: bool = False
    section: str = ""           # 所属章节标题（标题项为自身文本）
    text: str = ""


@dataclass
class ParaDoc:
    """[全局] Markdown 段落解析结果（流式构建，不全文驻留）"""
    path: str = ""
    title: str = ""
    items: list[ParaItem] = field(default_factory=list)
    truncated: bool = False


@dataclass
class DiffOp:
    """[全局] 匹配对内的一处字符级差异（difflib opcode）"""
    op: str = ""                # replace/delete/insert
    auto_snippet: str = ""      # 差异片段（含上下文）
    calib_snippet: str = ""
    auto_sent: str = ""         # 差异所在句子（定位用）
    calib_sent: str = ""


@dataclass
class MatchedPair:
    """[全局] 一对匹配段落"""
    auto_idx: int
    calib_idx: int
    dice: float
    both_heading: bool = False
    diffs: list[DiffOp] = field(default_factory=list)


@dataclass
class AlignRegion:
    """[全局] 一处不匹配区域"""
    region_type: str = ""       # shift（错位/替换）/ extra（自动版多段）/ missing（自动版缺段）
    auto_range: tuple = ()      # (start_idx, end_idx) 含端，空=无
    calib_range: tuple = ()
    auto_texts: list[str] = field(default_factory=list)   # 摘要（前 120 字符）
    calib_texts: list[str] = field(default_factory=list)


@dataclass
class AlignReport:
    """[全局] 对齐报告（to_dict 供 JSON 落盘）"""
    auto_path: str = ""
    calib_path: str = ""
    stats: dict = field(default_factory=dict)
    matched: list[MatchedPair] = field(default_factory=list)
    regions: list[AlignRegion] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """[全局] 报告 → JSON 可序列化 dict"""
        return {
            "auto_path": self.auto_path,
            "calib_path": self.calib_path,
            "stats": self.stats,
            "matched": [
                {"auto_idx": m.auto_idx, "calib_idx": m.calib_idx,
                 "dice": round(m.dice, 3), "both_heading": m.both_heading,
                 "diffs": [d.__dict__ for d in m.diffs]} for m in self.matched],
            "regions": [
                {"region_type": r.region_type, "auto_range": list(r.auto_range),
                 "calib_range": list(r.calib_range),
                 "auto_texts": r.auto_texts, "calib_texts": r.calib_texts}
                for r in self.regions],
            "notes": self.notes,
        }


def _tokens(norm: str) -> set[str]:
    """[局部] 词集合（相似度用）：英文词 + 中文字符及其相邻双字（中文段落 Dice 非零）"""
    toks = set(re.findall(r"[a-z0-9]+", norm))
    cjk = list(re.findall(r"[\u4e00-\u9fff]", norm))
    if cjk:
        toks.update(cjk)
        toks.update(a + b for a, b in zip(cjk, cjk[1:]))
    return toks


def _dice(a_norm: str, b_norm: str) -> float:
    """[局部] Dice 系数 = 2|A∩B| / (|A|+|B|)"""
    ta, tb = _tokens(a_norm), _tokens(b_norm)
    if not ta or not tb:
        return 0.0
    return 2.0 * len(ta & tb) / (len(ta) + len(tb))


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """[局部] 句子边界 span 列表（英文按 [.!?] + 大写开头；中文按 [。！？]）"""
    spans: list[tuple[int, int]] = []
    start = 0
    for m in _SENT_RE.finditer(text):
        end = m.end()
        spans.append((start, end))
        start = end
    if start < len(text):
        spans.append((start, len(text)))
    return spans


def _sent_at(text: str, char_pos: int) -> str:
    """[局部] 字符位置所在句子文本（用于差异定位；无则回退片段）"""
    for s, e in _sentence_spans(text):
        if s <= char_pos < e:
            return text[s:e].strip()[:120]
    return text[max(0, char_pos - DIFF_CONTEXT): char_pos + DIFF_CONTEXT].strip()


def parse_md_paras(path: str | Path) -> ParaDoc:
    """[全局] 流式解析 Markdown → 段落列表（跳过 frontmatter/callout/图片行）

    参数:
        path: Markdown 文件路径（自动版或校准版均可）
    返回:
        ParaDoc（items 按文档顺序编号，idx 从 1 起）
    报错:
        FileNotFoundError: 文件不存在（调用方负责转换为 PAPER-0001）
    """
    p = Path(path)
    doc = ParaDoc(path=str(p))
    doc.truncated = p.stat().st_size > MAX_MD_BYTES

    section = ""
    first_heading_done = False
    in_frontmatter = False
    buf: list[str] = []

    def flush() -> None:
        """[局部] 冲刷缓冲为段落（跳过样板行）"""
        nonlocal buf
        text = " ".join(x.strip() for x in buf).strip()
        if text and not _BOILER_RE.match(text):
            doc.items.append(ParaItem(idx=len(doc.items) + 1,
                                      is_heading=False, section=section, text=text))
        buf = []

    with open(p, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if _FRONTMATTER_RE.match(line):
                in_frontmatter = not in_frontmatter
                continue
            if in_frontmatter:
                continue
            if _CALLOUT_RE.match(line) or _IMAGE_RE.match(line):
                flush()
                continue
            m = _HEADING_RE.match(line)
            if m:
                flush()
                level, heading = len(m.group(1)), m.group(2).strip()
                if not first_heading_done and level == 1:
                    doc.title = heading
                    first_heading_done = True
                    continue
                section = heading
                doc.items.append(ParaItem(idx=len(doc.items) + 1,
                                          is_heading=True, section=heading, text=heading))
                continue
            if not line.strip():
                flush()
                continue
            buf.append(line)
    flush()

    if not doc.items:
        doc.truncated = True
    return doc


def _align_matrix(auto: list[ParaItem], calib: list[ParaItem],
                  threshold: float) -> list[list[float]]:
    """[局部] 段落相似度矩阵（惰性，仅计算到 DP 需要的行；先剥离 LaTeX 公式）"""
    mat = []
    for a in auto:
        an = normalize(strip_latex(a.text))
        row = []
        for c in calib:
            s = _dice(an, normalize(strip_latex(c.text)))
            row.append(s if s >= threshold else s - 1.0)
        mat.append(row)
    return mat


def _trace_path(mat: list[list[float]], n: int, m: int,
                skip_penalty: float = 0.15) -> list[tuple]:
    """[局部] DP 全局对齐回溯 → 路径（(i,j) 匹配点 或 (i,-) 跳 auto 或 (-,j) 跳 calib）"""
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        dp[i][0] = dp[i - 1][0] - skip_penalty
    for j in range(1, m + 1):
        dp[0][j] = dp[0][j - 1] - skip_penalty
    for i in range(1, n + 1):
        row, prev = mat[i - 1], dp[i - 1]
        cur = dp[i]
        for j in range(1, m + 1):
            cur[j] = max(prev[j] - skip_penalty, cur[j - 1] - skip_penalty,
                         prev[j - 1] + row[j - 1])
    path: list[tuple] = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0 and dp[i][j] == dp[i - 1][j - 1] + mat[i - 1][j - 1]:
            path.append((i, j))
            i, j = i - 1, j - 1
        elif i > 0 and dp[i][j] == dp[i - 1][j] - skip_penalty:
            path.append((i, 0))
            i -= 1
        else:
            path.append((0, j))
            j -= 1
    path.reverse()
    return path


def _char_diffs(a_text: str, c_text: str) -> list[DiffOp]:
    """[局部] 匹配对内字符级差异（difflib opcodes，含句子定位；先剥离 LaTeX 公式）"""
    import difflib
    an, cn = normalize(strip_latex(a_text)), normalize(strip_latex(c_text))
    if an == cn:
        return []
    ops: list[DiffOp] = []
    sm = difflib.SequenceMatcher(None, an, cn, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        ops.append(DiffOp(
            op=tag,
            auto_snippet=an[max(0, i1 - DIFF_CONTEXT): i2 + DIFF_CONTEXT],
            calib_snippet=cn[max(0, j1 - DIFF_CONTEXT): j2 + DIFF_CONTEXT],
            auto_sent=_sent_at(an, i1),
            calib_sent=_sent_at(cn, j1),
        ))
    return ops[:20]     # 限制单对差异条数，防报告爆炸


def align_docs(auto_doc: ParaDoc, calib_doc: ParaDoc,
               threshold: float = MATCH_THRESHOLD) -> AlignReport:
    """[全局] 段落级对齐：DP 全局对齐 + Dice 相似度 + 字符级差异定位

    参数:
        auto_doc: parse_md_paras() 自动版产物
        calib_doc: parse_md_paras() 校准版产物
        threshold: Dice 匹配阈值
    返回:
        AlignReport（matched / regions / stats）
    """
    auto, calib = auto_doc.items, calib_doc.items
    n, m = len(auto), len(calib)
    mat = _align_matrix(auto, calib, threshold)
    path = _trace_path(mat, n, m)
    report = AlignReport(auto_path=auto_doc.path, calib_path=calib_doc.path)

    # 路径 → 匹配对 + 不匹配区域（连续跳段聚合成区域）
    i, j = 0, 0
    region_auto: list[int] = []
    region_calib: list[int] = []

    def flush_region() -> None:
        """[局部] 冲刷不匹配区域"""
        nonlocal region_auto, region_calib
        if not region_auto and not region_calib:
            return
        if region_auto and region_calib:
            rtype = "shift"
        elif region_auto:
            rtype = "extra"
        else:
            rtype = "missing"
        report.regions.append(AlignRegion(
            region_type=rtype,
            auto_range=(region_auto[0], region_auto[-1]) if region_auto else (),
            calib_range=(region_calib[0], region_calib[-1]) if region_calib else (),
            auto_texts=[auto[x - 1].text[:120] for x in region_auto],
            calib_texts=[calib[x - 1].text[:120] for x in region_calib]))
        region_auto, region_calib = [], []

    for ai, ci in path:
        if ai and ci:
            flush_region()
            a, c = auto[ai - 1], calib[ci - 1]
            score = mat[ai - 1][ci - 1]
            report.matched.append(MatchedPair(
                auto_idx=a.idx, calib_idx=c.idx, dice=score,
                both_heading=a.is_heading and c.is_heading,
                diffs=_char_diffs(a.text, c.text)))
        elif ai:
            region_auto.append(ai)
        else:
            region_calib.append(ci)
    flush_region()

    matched_count = len(report.matched)
    avg = (sum(x.dice for x in report.matched) / matched_count) if matched_count else 0.0
    report.stats = {
        "auto_para_count": n,
        "calib_para_count": m,
        "matched_count": matched_count,
        "match_ratio": round(matched_count / max(1, min(n, m)), 3),
        "avg_dice": round(avg, 3),
        "low_dice_pairs": sum(1 for x in report.matched if x.dice < 0.8),
        "region_count": len(report.regions),
        "region_by_type": {t: sum(1 for r in report.regions if r.region_type == t)
                           for t in ("shift", "extra", "missing")},
        "threshold": threshold,
        "auto_truncated": auto_doc.truncated,
        "calib_truncated": calib_doc.truncated,
    }
    report.notes.append("两个文件均已完整解析" if not (
        auto_doc.truncated or calib_doc.truncated) else "存在文件被截断解析")
    return report
