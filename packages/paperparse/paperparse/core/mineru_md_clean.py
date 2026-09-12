#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: skill/src/paperparse/core/mineru_md_clean.py
功能: MinerU 高精度解析产物（full.md）→ 干净拼接版 Markdown：
      - 正文/图注文本**原样保留**（含 LaTeX 公式与 <sup>/<sub>，不丢失格式）
      - 删除小图引用行（![](images/<hash>.jpg) 等非 F00x 命名 → 高精度版大图/小图被拆分）
      - 删除子图标注行（单独 (a)/(b) 或 a)、b) 开头的段落——小图附近图注不可信）
      - 保留大图题注（Figure/Fig N. 开头，可认为是正确）
      - 按题注编号在题注前插入本地大图（images/F00N.png，与 markdown_render 的 T-C 规则一致）
      - 摘要无标题时补插 "## Abstract"（与渲染净化一致）
      注: 本模块以 full.md 为**唯一文本来源**（高精度为主）；段落级重排/拼接不在此处做
对外接口: clean_mineru_md / convert_mineru_md_file
版本: v1.1.0 (2026-08-18)
版本历史:
  v1.1.0 回退 v1.2.0 的 merge_mineru_text（句子级混用造成 LaTeX 缺失/重复，用户要求回退）
  v1.1.0 修复图片引用判定（image_map 精确比对）、子图标注误删（正则收紧）
  v1.0.0 初始版本（用户反馈：拼接正文丢失 LaTeX 格式；大图题注正确、小图/子图标注需清理）
"""
from __future__ import annotations

import re
from pathlib import Path

__all__ = ["clean_mineru_md", "convert_mineru_md_file"]

_IMAGE_REF_RE = re.compile(r"^\[?!\[[^\]]*\]\(([^)]+)\)\]?\s*$")  # ![](...) 或 [![](...)]
_CAPTION_RE = re.compile(r"^(Fig(ure)?\.?\s*\d+\s*[.:])", re.IGNORECASE)  # 大图题注 Figure 1.
_CAPTION_NUM_RE = re.compile(r"^(Fig(ure)?\.?\s*)(\d+)", re.IGNORECASE)
# 子图标注行首：(a) / (b) / a) / b) / a. / b. 形式（须带括号/右括号/点号，防止误删普通字母开头正文）
_SUBFIG_RE = re.compile(r"^(\([a-z]\)|[a-z]\)|[a-z]\.)\s*($|\S)", re.IGNORECASE)
_HEADING_RE = re.compile(r"^(#{1,4})\s+(.*?)\s*$")


def _is_subfig_label(line: str) -> bool:
    """[局部] 子图标注行判定：(a) / (b) / a) / b. 等开头的**单独段落**（小图附近图注，不可信）
    —— 图注段内联的 "a) ... b) ..."（行首为 Figure）不受影响；长度上限防误删正文长句"""
    t = line.strip()
    if not t:
        return False
    if re.match(r"^\([a-z]\)\s*$", t):          # 纯标注 "(a)"
        return True
    if re.match(r"^[a-z]\)\s*$", t):            # 纯标注 "a)"
        return True
    if _SUBFIG_RE.match(t) and len(t) < 200:    # "(a) 描述..." / "a) 描述..." / "a. 描述..."
        return True
    return False


def clean_mineru_md(md_text: str, image_map: dict[int, str] | None = None) -> str:
    """[全局] MinerU 高精度 MD → 干净拼接版文本（LaTeX 原样保留）

    参数:
        md_text: MinerU full.md 全文（如 work/mineru_backup/<时间戳-v4batch>/full.md）
        image_map: 图注编号 → 本地大图相对路径（如 {1: "images/F001.png"}）；
                   None=不插入图片
    返回:
        清理后的 Markdown 文本
    """
    lines = md_text.splitlines()
    out: list[str] = []
    inserted: set[int] = set()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        # 1) 图片引用行：仅保留本地大图（路径在 image_map 中，如 images/F001.png）；
        #    其余（MinerU 拆分的小图/内嵌图，哈希命名）→ 删除
        m_img = _IMAGE_REF_RE.match(line.strip())
        if m_img:
            path = m_img.group(1)
            if not (image_map and path in image_map.values()):
                i += 1
                continue
        # 2) 子图标注行 → 删除
        if _is_subfig_label(line):
            i += 1
            continue
        # 3) 大图题注：Figure N. 开头 → 题注前插入对应大图（图在题注前，T-C 规则）
        m_cap = _CAPTION_RE.match(line.strip())
        if m_cap:
            num = int(_CAPTION_NUM_RE.match(line.strip()).group(3))
            if image_map and num in image_map and num not in inserted:
                img_line = "![](%s)" % image_map[num]
                # 防重复：上一非空行已是同一图片引用
                prev = next((x for x in reversed(out) if x.strip()), "")
                if prev.strip() != img_line:
                    out.append("")
                    out.append(img_line)
                inserted.add(num)
        out.append(line)
        i += 1

    text = "\n".join(out)
    # 4) 摘要无标题补插：文档 H1 之后、首个 "##" 章节标题前的段落（作者/摘要）→ 前插 "## Abstract"
    if not re.search(r"^##+\s+.*abstract", text, re.IGNORECASE | re.M):
        lines2 = text.splitlines()
        j = len(lines2)
        for k, ln in enumerate(lines2):
            if re.match(r"^#{2,}\s+", ln.strip()):    # 找首个二级标题（跳过文档 H1）
                j = k
                break
        if 2 < j < len(lines2):       # H1 与首个章节标题之间存在内容（摘要）→ 补插
            lines2.insert(j, "## Abstract")
            lines2.insert(j, "")
        text = "\n".join(lines2)
    # 5) 折叠多余空行（连续 3+ 空行 → 2）
    text = re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
    return text


def convert_mineru_md_file(src_md: str | Path, images_dir: str | Path,
                           out_md: str | Path) -> Path:
    """[全局] 文件级转换：MinerU full.md + 本地大图目录 → 干净拼接版 MD

    大图映射：images_dir 下 F001.png..F00N.png 按编号 ↔ 题注 Figure 1..N。

    参数:
        src_md: MinerU full.md 路径
        images_dir: 本地大图目录（含 F00x.png）
        out_md: 输出 Markdown 路径
    返回:
        输出文件路径
    """
    text = Path(src_md).read_text(encoding="utf-8", errors="replace")
    img_dir = Path(images_dir)
    image_map: dict[int, str] = {}
    if img_dir.exists():
        for p in sorted(img_dir.glob("F*.png")):
            m = re.match(r"F(\d+)\.png$", p.name)
            if m:
                image_map[int(m.group(1))] = "images/%s" % p.name
    cleaned = clean_mineru_md(text, image_map or None)
    out = Path(out_md)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(cleaned, encoding="utf-8")
    return out
