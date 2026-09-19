#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: paperparse/core/md_align.py
功能: P14-M5 mineru md ↔ 本地骨架对齐：
      - 解析 md 段落（标题/正文/图注/图片标记）；
      - 本地骨架行 ↔ md 正文段做**顺序敏感锚点对齐**（多文字重复程度标段落
        起止；锚点单调不回退，防栏序错误整段错位）；
      - 输出对齐映射（md 段 → 本地行区间）+ 差异清单（md 段边界 vs 本地
        骨架段落边界不一致处 = 拼接疑点，供 M6 修复）。
对外接口: align_md_to_skeleton / MdPara / AlignResult
版本: v1.0.0 (2026-08-24)

设计依据（用户规则）：
- md 段落是"文本基底"（v4 full.md == 网页版 0 差异）；本地骨架是"权威边界"；
- 字符匹配可能不完全（LaTeX/OCR 瑕疵/连字符）→ 按**多个文字的重复程度**
  （归一化词袋 Dice + 连续词重叠）标注段落起止，不逐字匹配。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# caption 严格判据单一真相源（block_classify R10）：数字后须标点/大写，排除正文子图引用
from paperparse.core.block_classify import _is_caption_line
from typing import Optional

from paperparse.core.para_align import strip_latex

__all__ = ["align_md_to_skeleton", "MdPara", "AlignResult", "parse_md_paragraphs"]

# 行/段归一化（对齐用）：剥 LaTeX + HTML 标签 + 折叠空白 + 小写
_TAG_RE = re.compile(r"<[^>]+>")
_SUP_CITE_RE = re.compile(r"[⁰¹²³⁴⁵⁶⁷⁸⁹]+")
_CITE_RE = re.compile(r"\[\s*\d+(?:[\s,–—-]\s*\d+)*\s*\]")
_MATHY_RE = re.compile(r"\$[^$]*\$")
_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
NUM_RE = re.compile(r"\b\d+\b")
# Unicode ligature 规范化（PDF 提取 "artiﬁcial" ﬁ=U+FB01 vs md "artificial" fi
# → 对齐 token 不一致 → 锚失败 → 链式错位；adma 实测 24/137 对齐）
_LIGATURES = str.maketrans({
    "ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl", "ﬆ": "st",
})


def norm_text(text: str) -> str:
    """[全局] 对齐归一化：剥 LaTeX/标签/图片/引用编号/数字 → 小写折叠空白"""
    t = strip_latex(text or "")
    t = _TAG_RE.sub(" ", t)
    t = _IMG_RE.sub(" ", t)
    t = _CITE_RE.sub(" ", t)
    t = _SUP_CITE_RE.sub(" ", t)
    t = _MATHY_RE.sub(" ", t)
    t = t.translate(_LIGATURES)
    t = NUM_RE.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip().lower()


def _tokens(norm: str) -> set[str]:
    """[局部] 词袋（英文词 + 中文单字/双字）"""
    toks = set(re.findall(r"[a-z][a-z'\-]*", norm))
    han = re.findall(r"[\u4e00-\u9fff]", norm)
    toks.update(han)
    toks.update(han[i] + han[i + 1] for i in range(len(han) - 1))
    return toks


def _dice(a_norm: str, b_norm: str) -> float:
    """[局部] Dice 系数（0~1）"""
    ta, tb = _tokens(a_norm), _tokens(b_norm)
    if not ta and not tb:
        return 1.0 if a_norm == b_norm else 0.0
    if not ta or not tb:
        return 0.0
    return 2.0 * len(ta & tb) / (len(ta) + len(tb))


@dataclass
class MdPara:
    """[全局] md 段落（解析产物）"""
    idx: int
    text: str                # 原始文本（含 LaTeX/标签，M6 保留）
    kind: str = "body"       # title/heading/body/caption/image/other
    norm: str = ""
    section: str = ""

    def to_dict(self) -> dict:
        return {"idx": self.idx, "kind": self.kind, "text": self.text[:120]}


@dataclass
class AlignResult:
    """[全局] md ↔ 本地骨架对齐结果"""
    md_paras: list = field(default_factory=list)          # list[MdPara]
    # md 段 idx → 本地行区间 (start_line_id, end_line_id)；None=未对齐
    line_map: dict = field(default_factory=dict)
    diffs: list = field(default_factory=list)             # 拼接疑点清单
    stats: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"md_paras": [p.to_dict() for p in self.md_paras],
                "line_map": {str(k): v for k, v in self.line_map.items()},
                "diffs": self.diffs, "stats": self.stats}


def parse_md_paragraphs(md_text: str) -> list[MdPara]:
    """[全局] md 文本 → 段落列表（空行分隔；标题/图片/图注分类）"""
    paras: list[MdPara] = []
    section = ""
    idx = 0
    for raw in re.split(r"\n\s*\n", md_text):
        block = "\n".join(l.strip() for l in raw.splitlines() if l.strip()).strip()
        if not block:
            continue
        lines = block.splitlines()
        first = lines[0]
        kind = "body"
        if first.startswith("# "):
            kind = "title"
        elif first.startswith("## "):
            kind = "heading"
            section = first[3:].strip()
        elif first.startswith("!["):
            kind = "image"
            # 图片标记后紧跟图注（同一块内 "![](...)\nFig. 1. caption"）→ 拆出 caption
            # 严格判据：数字后须标点/大写，排除正文子图引用 "Fig. 1a shows"（R10 同源）
            if len(lines) > 1 and _is_caption_line(lines[1]):
                paras.append(MdPara(idx=idx, text=block, kind=kind,
                                    norm=norm_text(block), section=section))
                idx += 1
                cap_block = "\n".join(lines[1:]).strip()
                paras.append(MdPara(idx=idx, text=cap_block, kind="caption",
                                    norm=norm_text(cap_block), section=section))
                idx += 1
                continue
        elif _is_caption_line(first):
            kind = "caption"
        paras.append(MdPara(idx=idx, text=block, kind=kind,
                           norm=norm_text(block), section=section))
        idx += 1
    return paras


def align_md_to_skeleton(md_text: str, skeleton) -> AlignResult:
    """[全局] 主入口：mineru md 段落 ↔ 本地骨架行对齐

    算法（顺序敏感锚点贪心）：
      1. md 正文段序列（跳过 title/heading/image）与本地 body 行序列；
      2. 对每个 md 段：找最佳连续行区间——锚行（单行 Dice 最高）向上下扩展，
         扩展后拼接文本与 md 段 Dice 最优；
      3. 锚点单调：下一个 md 段的搜索起点 ≥ 当前段结束行（不回退）；
      4. md 段 ↔ 本地骨架段落边界不一致 → diffs（供 M6）。
    """
    md_paras = parse_md_paragraphs(md_text)
    lines = list(getattr(skeleton, "lines", []) or [])
    body_lines = [ln for ln in lines
                  if getattr(ln, "kind", "") in ("body", "other", "list")
                  and (getattr(ln, "text", "") or "").strip()]
    line_norms = [norm_text(getattr(ln, "text", "")) for ln in body_lines]

    md_body = [p for p in md_paras if p.kind == "body"]
    line_map: dict[int, tuple] = {}
    diffs: list = []
    search_start = 0

    # 反向双锚辅助（用户规则：从 md 段首/段尾片段出发，去 PDF 行中模糊匹配
    # 定位 + 特征验证；而不是从 PDF 盲找边界）
    _CITE_TAIL_RE = re.compile(r"\s*\[[\d\s,\-–—]+\]\s*$")

    def _prefix_overlap(line_norm: str, toks: list) -> int:
        """行词序列与段首词的前缀连续匹配数（模糊：逐词对齐失配即停；
        容忍单词识别错误——连续 ≥3 词即可锚定）"""
        lt = line_norm.split()
        n = 0
        for a, b in zip(lt, toks):
            if a == b:
                n += 1
            else:
                break
        return n

    def _suffix_overlap(line_norm: str, toks: list) -> int:
        """行词序列中与段尾词的连续匹配数（任意起始）"""
        lt = line_norm.split()
        best = 0
        for start in range(len(lt)):
            n = 0
            for a, b in zip(lt[start:], toks):
                if a == b:
                    n += 1
                else:
                    break
            best = max(best, n)
        return best

    def _is_head_feature(idx: int) -> bool:
        """段首特征：栏内首行（跨栏/跨页）或 相对同栏上一行 x0 右移
        （相对缩进 ≈1-2 字符宽；无缩进期刊严格对齐 → 右移即段首信号）"""
        if idx <= 0:
            return True
        ln, prev = body_lines[idx], body_lines[idx - 1]
        if prev.page != ln.page or prev.column != ln.column:
            return True
        indent_pt = max(3.0, 0.5 * ln.font_size) if ln.font_size else 6.0
        return ln.bbox[0] - prev.bbox[0] >= indent_pt

    def _line_ends_sentence(text: str) -> bool:
        t = _CITE_TAIL_RE.sub("", (text or "").strip()).rstrip()
        return bool(t) and t[-1] in ".!?"

    for _pi, p in enumerate(md_body):
        if not p.norm:
            continue
        toks = p.norm.split()
        if len(toks) < 4:
            continue
        head_toks = toks[:8]
        tail_toks = toks[-8:]
        # 下一个 md 段段首片段（段尾校正不得吞它的行）
        _next_head = None
        if _pi + 1 < len(md_body) and md_body[_pi + 1].norm:
            _nt = md_body[_pi + 1].norm.split()
            if len(_nt) >= 4:
                _next_head = _nt[:8]
        # 1) 段首锚：行与 md 段首片段前缀匹配 ≥3 词（从 search_start 起，
        #    顺序敏感单调推进）
        head_i = -1
        for i in range(search_start, len(body_lines)):
            if _prefix_overlap(line_norms[i], head_toks) >= 3:
                head_i = i
                break
        if head_i < 0:
            continue
        # 2) 段首特征校正：锚行若不是段首特征（mineru 拆段处段首片段可能
        #    落在 PDF 段中间）→ 向前找最近的段首特征行（≤4 行，仍与段首
        #    片段匹配 ≥2 词）
        if not _is_head_feature(head_i):
            for j in range(head_i - 1, max(search_start, head_i - 4) - 1, -1):
                if _is_head_feature(j) \
                        and _prefix_overlap(line_norms[j], head_toks) >= 2:
                    head_i = j
                    break
        # 3) 段尾定位：**连续匹配 ≥3 优先**（精确段尾锚 + 校正到句尾；
        #    adma 93 对齐路径）；**未命中 → 累积扩展兜底**（允许连续 2 行
        #    无增益跳过——断词行 "facili-"/"− as the" 卡住缺陷；cej 长段
        #    碎行无单行连续匹配时走此路径）
        tail_i = head_i
        _tail_anchored = False
        for i in range(head_i, min(head_i + 80, len(body_lines))):
            if _suffix_overlap(line_norms[i], tail_toks) >= 3:
                tail_i = i
                _tail_anchored = True
                break
        if _tail_anchored:
            # 段尾校正：限 3 行；句尾/新段首/下一 md 段段首行 → 停
            # （mineru 断段处 md 段尾非句尾——校正过头会吞下一段行，
            # 实证 md#10 尾 "(Fig. 1a)" 吞掉 md#11 "during…" 行）
            j = tail_i
            while j < min(tail_i + 3, len(body_lines) - 1):
                if _line_ends_sentence(body_lines[j].text):
                    break
                if _is_head_feature(j + 1):
                    break
                if _next_head and _prefix_overlap(
                        line_norms[j + 1], _next_head) >= 3:
                    break
                j += 1
            tail_i = j
        else:
            cur_norm = line_norms[head_i]
            cur_d = _dice(p.norm, cur_norm)
            skip = 0
            for i in range(head_i + 1, min(head_i + 80, len(body_lines))):
                cand = cur_norm + " " + line_norms[i]
                d = _dice(p.norm, cand)
                if d >= cur_d - 0.005:
                    cur_norm, cur_d = cand, d
                    tail_i = i
                    skip = 0
                else:
                    skip += 1
                    if skip >= 2:
                        break
        # 4) 区间验证：主路径（连续段尾锚命中）Dice ≥ 0.45；兜底路径
        #    （累积扩展）**更严 ≥ 0.5**——兜底扩展易吞后续段污染
        #    search_start，宁缺毋滥（不记录 → 后续段正常锚定）
        seg = " ".join(line_norms[head_i:tail_i + 1])
        need = 0.45 if _tail_anchored else 0.5
        if _dice(p.norm, seg) < need:
            continue
        line_map[p.idx] = (body_lines[head_i].line_id,
                           body_lines[tail_i].line_id)
        search_start = tail_i + 1

    # 3) 差异：md 段 ↔ 本地骨架段落边界不一致
    skeleton_paras = [pp for pp in getattr(skeleton, "paragraphs", []) or []
                      if getattr(pp, "kind", "") == "body"]
    for p in md_body:
        rng = line_map.get(p.idx)
        if not rng:
            continue
        start_id, end_id = rng
        # 该 md 段覆盖的本地行区间 → 对应本地骨架段落数
        covered = [sp for sp in skeleton_paras
                   if sp.start_line and sp.end_line
                   and start_id <= sp.end_line.line_id <= end_id
                   or (sp.start_line.line_id <= end_id
                       and sp.end_line.line_id >= start_id)]
        if len(covered) > 1:
            diffs.append({"type": "md_splits_local",
                          "md_idx": p.idx, "text": p.text[:120],
                          "local_paras": [c.para_id for c in covered]})
        elif len(covered) == 1 and covered[0].words > 20 and p.norm \
                and _dice(p.norm, norm_text(covered[0].text)) < 0.5:
            diffs.append({"type": "md_local_mismatch", "md_idx": p.idx,
                          "text": p.text[:120],
                          "local_para": covered[0].para_id})

    stats = {"md_paras": len(md_paras), "md_body": len(md_body),
             "aligned": len(line_map), "unaligned": len(md_body) - len(line_map),
             "diffs": len(diffs)}
    return AlignResult(md_paras=md_paras, line_map=line_map,
                       diffs=diffs, stats=stats)
