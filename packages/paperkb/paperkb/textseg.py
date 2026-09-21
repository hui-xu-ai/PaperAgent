# -*- coding: utf-8 -*-
"""文本切分与边界对齐（RAG 向量索引 / 检索注入共用原语）。

背景（2026-09-21 审计）：向量索引与检索注入此前一律 `text[:N]` 硬切——实测把句子、
markdown 列表行拦腰截断，并把「方法论连接 / 研究趋势 / 相关文献」这类尾部整段丢掉
（9 篇编译产物里 4 篇超限，丢 359~1014 字符）；CJK LIKE 兜底更是把 `substr(content,1,500)`
（文件开头 = frontmatter 模板）当"检索证据"返回给模型。⇒ 抽出本模块，四个原语：

- `strip_frontmatter`：去 YAML frontmatter（`type/doi/tags` 是模板噪声，不该占嵌入预算，
  更不该被当成检索证据）；
- `boundary_trim`：**绝不超上限**地截断，优先在 段落 > 行 > 句 > 逗号/空格 边界收尾，
  且不在 `$…$` 公式中间断开；
- `window_around`：命中位置 ±radius 的窗口（CJK 兜底用，替代"文件开头 500 字"）；
- `split_chunks`：markdown 标题层级分块（块内按 段落→句子→硬切 三级细分，同节相邻块
  带重叠，过小尾块并入前块），返回**原文偏移** `start/end`——命中块可逐字节从原文切回，
  保证"命中段 = 注入段"。

不变量：`boundary_trim` / `window_around` 返回值长度恒 ≤ limit；`split_chunks` 的
`text == body[start:end].strip()`；同一输入恒得同一输出（无随机、无时间依赖）。
"""
from __future__ import annotations

import re

# YAML frontmatter（须成对 --- ；无闭合则不视为 frontmatter）
_FRONTMATTER_RE = re.compile(r"^---\r?\n.*?\r?\n---\r?\n?", re.S)

# markdown 标题（允许前导空格，须为行首）
_HEADING_RE = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(.*?)[ \t]*$")

# 纯标题块（整块除标题外无正文）→ 无可检索内容，丢弃
_HEADING_ONLY_RE = re.compile(r"\A#{1,6}[ \t]+\S[^\n]*\Z")

# 边界候选（语义优先级从高到低：段落 > 行 > 句末 > 逗号 > 空格）
_BOUNDARY_ORDER = ("\n\n", "\n", "。", "！", "？", "；", ". ", "! ", "? ", "; ",
                   "，", ", ", " ")

# 句末标点（**不含 "."**：避免小数/缩写被切断）
_SENT_END_RE = re.compile(r"[。！？；!?;]")

# 段落分隔（含空行里的空白）
_PARA_SEP_RE = re.compile(r"\n[ \t]*\n")


def strip_frontmatter(text: str) -> str:
    """去 YAML frontmatter；无 frontmatter 时原样返回。"""
    if not text:
        return ""
    m = _FRONTMATTER_RE.match(text)
    return text[m.end():] if m else text


def _dollars_balanced(s: str) -> bool:
    """前缀内 `$` 是否成对（防截断落在 `$…$` 公式中间）。"""
    return s.count("$") % 2 == 0


def boundary_trim(text: str, limit: int, min_ratio: float = 0.6) -> str:
    """截断到 ≤ limit 字符，尽量在语义边界收尾。

    - 依次尝试 段落 / 行 / 句末 / 逗号 / 空格 边界，取**该类型里最靠右**且不低于
      `limit*min_ratio` 的位置；找不到任何边界 → 退回硬切 `text[:limit]`；
    - 每个候选都要求 `$` 成对，否则换下一个类型（公式优先完整）。

    返回值长度恒 ≤ limit（边界位置取自 `text[:limit]` 之内）。
    """
    if limit <= 0:
        return ""
    text = text or ""
    if len(text) <= limit:
        return text
    window = text[:limit]
    floor = int(limit * min_ratio)
    for sep in _BOUNDARY_ORDER:
        pos = window.rfind(sep)
        if pos < 0 or pos + len(sep) < floor:
            continue
        cut = text[:pos + len(sep)].rstrip()
        if cut and _dollars_balanced(cut):
            return cut
    return window


def window_around(text: str, pos: int, radius: int = 300, limit: int = 700) -> str:
    """命中位置 `pos` 附近窗口（去首尾半截行；长度 ≤ limit）。

    `pos < 0`（未定位到命中）→ 退化为文本开头窗口。
    """
    text = text or ""
    if not text:
        return ""
    radius = max(50, int(radius))
    if pos < 0:
        return boundary_trim(text, limit)
    start = max(0, pos - radius)
    end = min(len(text), start + limit, pos + radius)
    seg = text[start:end]
    if start > 0:
        nl = seg.find("\n")
        if 0 <= nl <= len(seg) // 2:
            seg = seg[nl + 1:]
    if end < len(text):
        nl = seg.rfind("\n")
        if nl >= len(seg) // 2:
            seg = seg[:nl]
    return boundary_trim(seg.strip(), limit)


def split_chunks(body: str, *, max_chars: int = 1200, overlap_chars: int = 160,
                 min_chars: int = 60) -> list[dict]:
    """markdown 标题感知分块。

    两级：先把每个标题小节细分为 ≤max_chars 的"单元"（段落→句子→硬切），再**跨小节
    贪心打包**到 ≤max_chars 的块——否则 `_note.md` 这种"每节一两行"的卡片会被切成
    八九个碎块（每块一个标题 + 一句话），向量里全是碎片。

    Returns:
        [{"section": 首单元所属小节名, "text": 原文切片, "start": int, "end": int}]
        偏移以 `body` 为基准；`text == body[start:end].strip()`。
    """
    body = body or ""
    if not body.strip():
        return []
    units: list[tuple[str, int, int]] = []
    for section, bstart, bend, level in _heading_blocks(body):
        if level == 1 and _HEADING_ONLY_RE.match(body[bstart:bend].strip()):
            continue   # H1 标题块无正文：标题已在 embed_prefix 里，不必单列
        units.extend((section, s, e)
                     for s, e in _unit_ranges(body, bstart, bend, max_chars)
                     if body[s:e].strip())
    chunks: list[dict] = []
    cur: list | None = None
    for section, s, e in units:
        if cur is None:
            cur = [section, s, e]
        elif e - cur[1] <= max_chars:
            cur[2] = e
        else:
            chunks.append(cur)
            cur = [section, s, e]
    if cur is not None:
        chunks.append(cur)
    out = [{"section": c[0], "start": c[1], "end": c[2],
            "text": body[c[1]:c[2]].strip()} for c in chunks]
    out = _merge_small(body, out, max_chars, min_chars)
    out = _apply_overlap(body, out, overlap_chars)
    return [c for c in out if c["text"]]


def embed_prefix(title: str = "", label: str = "", section: str = "") -> str:
    """嵌入用前缀：`[标题 | 层级 | 小节]`（提升区分度，不进入原文偏移）。"""
    parts = [p.strip() for p in (title, label, section) if (p or "").strip()]
    return f"[{' | '.join(parts)}]\n" if parts else ""


# ---------------------------------------------------------------- 内部：分块
def _heading_blocks(body: str) -> list[tuple[str, int, int, int]]:
    """按标题切块区间：[(小节名, 起, 止, 层级)]（标题行归属其后的内容）。"""
    heads: list[tuple[int, str, int]] = []
    pos = 0
    for line in body.splitlines(keepends=True):
        m = _HEADING_RE.match(line.rstrip("\r\n"))
        if m:
            heads.append((pos, m.group(2).strip(), len(m.group(1))))
        pos += len(line)
    total = len(body)
    if not heads:
        return [("", 0, total, 0)]
    blocks: list[tuple[str, int, int, int]] = []
    if heads[0][0] > 0:
        blocks.append(("", 0, heads[0][0], 0))
    for i, (ls, title, level) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else total
        blocks.append((title, ls, end, level))
    return blocks


def _paragraph_ranges(body: str, start: int, end: int) -> list[tuple[int, int]]:
    """[start,end) 内的段落区间（段落间空行不计入）。"""
    out: list[tuple[int, int]] = []
    pos = start
    for m in _PARA_SEP_RE.finditer(body[start:end]):
        cut = start + m.start()
        if cut > pos:
            out.append((pos, cut))
        pos = start + m.end()
    if end > pos:
        out.append((pos, end))
    return out


def _sentence_ranges(body: str, start: int, end: int) -> list[tuple[int, int]]:
    """[start,end) 内按句末标点切分（不含 "."：避免小数/缩写被切）。"""
    out: list[tuple[int, int]] = []
    pos = start
    for m in _SENT_END_RE.finditer(body[start:end]):
        cut = start + m.end()
        if cut > pos:
            out.append((pos, cut))
            pos = cut
    if end > pos:
        out.append((pos, end))
    return out


def _unit_ranges(body: str, start: int, end: int,
                 max_chars: int) -> list[tuple[int, int]]:
    """块内三级细分：段落 → 句子 → 逗号/空格边界硬切，每段 ≤ max_chars。"""
    units: list[tuple[int, int]] = []
    for pstart, pend in _paragraph_ranges(body, start, end):
        if pend - pstart <= max_chars:
            units.append((pstart, pend))
            continue
        for sstart, send in _sentence_ranges(body, pstart, pend):
            if send - sstart <= max_chars:
                units.append((sstart, send))
                continue
            cur = sstart
            while send - cur > max_chars:
                piece = boundary_trim(body[cur:send], max_chars)
                step = len(piece) if piece else 0
                if step <= 0:
                    step = max_chars
                units.append((cur, cur + step))
                cur += step
            if send > cur:
                units.append((cur, send))
    return units


def _merge_small(body: str, chunks: list[dict], max_chars: int,
                 min_chars: int) -> list[dict]:
    """收尾：丢弃纯标题残块；过小块**仅并回同小节的**前块（不跨小节混语义）。

    跨小节的小块（如短「## 相关文献」列表）**保留**——内容优先于整齐度，
    合并会让它挂到别的小节名下（检索时"小节名"就成了谎报）。
    """
    out: list[dict] = []
    cap = int(max_chars * 4 / 3)
    for c in chunks:
        text = c["text"]
        if not text:
            continue
        if _HEADING_ONLY_RE.match(text):     # 只有标题、没有正文 → 无可检索内容
            continue
        if (len(text) < min_chars and out
                and out[-1]["section"] == c["section"]
                and c["end"] - out[-1]["start"] <= cap):
            prev = out[-1]
            prev["end"] = c["end"]
            prev["text"] = body[prev["start"]:c["end"]].strip()
            continue
        out.append(c)
    return out


def _last_unit_start(body: str, start: int, end: int,
                     overlap_chars: int) -> int | None:
    """上块内"最后一个 段落/句子"的起点（其长度 ≤ overlap_chars 才算数）。"""
    for rng in (_paragraph_ranges(body, start, end),
                _sentence_ranges(body, start, end)):
        if not rng:
            continue
        last_s, last_e = rng[-1]
        if last_s > start and last_e - last_s <= overlap_chars:
            return last_s
    return None


def _apply_overlap(body: str, chunks: list[dict],
                   overlap_chars: int) -> list[dict]:
    """同小节相邻块带重叠：把上块最后一个 段落/句子 前置到本块。"""
    if overlap_chars <= 0:
        return chunks
    for i in range(1, len(chunks)):
        prev, cur = chunks[i - 1], chunks[i]
        if prev["section"] != cur["section"]:
            continue
        tail = _last_unit_start(body, prev["start"], prev["end"], overlap_chars)
        if tail is not None and tail < cur["start"]:
            cur["start"] = tail
            cur["text"] = body[tail:cur["end"]].strip()
    return chunks
