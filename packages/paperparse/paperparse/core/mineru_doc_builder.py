#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: skill/src/paperparse/core/mineru_doc_builder.py
功能: 高精度文本融合（用户批准架构）：**以 MinerU 高精度 full.md 为文本主体**，
      本地 stitch 提供段落结构（顺序/标题/图注/段间合并决策），PyMuPDF 提供辅助
      （大图 F00x.png 提取由 S4 完成，此处仅装配）。
      - 段级映射（**段落守恒**：每个 full.md 段只归属一个本地段，禁止重复使用）
      - 本地合并段（如 Introduction 首段跨块合并）→ 覆盖多个 full.md 段并拼接 LaTeX
      - 未归属 full 段的本地段保留本地文本（不混拼，避免"有的有 LaTeX 有的没有"）
对外接口: build_mineru_doc
版本: v1.0.0 (2026-08-18)
版本历史:
  v1.0.0 初始版本（替代 v1.2.0 已回退的句子级 merge——段落级整体替换）
"""
from __future__ import annotations

import re
from pathlib import Path

from paperparse.core.para_align import parse_md_paras
from paperparse.middleware.schema import Paragraph

__all__ = ["build_mineru_doc"]

MIN_COVERAGE = 0.5          # 归属 full 段长度和 / 本地段长度 下限（防局部小匹配整段替换）
MIN_FULL_LEN = 40           # full 段参与归属的最小 compact 长度（防 "Information)." 误归属）
NGRAM_COVER = 0.85          # full 段 4-gram 在本地段中的覆盖率下限（容 OCR 拼写差异，如 Eficient/Efficient）


def _ngrams(s: str, n: int = 4) -> set[str]:
    """[局部] 字符 n-gram 集合（归属判据：对单字符差异/空格差异容错）"""
    return {s[i:i + n] for i in range(max(0, len(s) - n + 1))}


_LIGATURES = str.maketrans({"\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl"})


def _compact(text: str) -> str:
    """[局部] 匹配用紧凑归一化：连字展开 → 去 HTML → 公式内容去空白保留 → 去 LaTeX 命令/标记 → 小写去空白"""
    t = text.translate(_LIGATURES)
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"\${1,2}([^$]+?)\${1,2}", lambda m: re.sub(r"\s+", "", m.group(1)), t)
    t = re.sub(r"\\[a-zA-Z]+", "", t)
    t = re.sub(r"[{}^_]", "", t)
    t = t.lower()
    return re.sub(r"\s+", "", t)


def _full_paras(full_md_text: str) -> list[tuple[bool, str]]:
    """[局部] 解析 full.md 段落（is_heading, text）；临时文件方式走流式解析"""
    import os
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".md", encoding="utf-8",
                                     delete=False) as f:
        f.write(full_md_text)
        tmp = f.name
    try:
        doc = parse_md_paras(tmp)
    finally:
        os.unlink(tmp)
    return [(x.is_heading, x.text) for x in doc.items]


def build_mineru_doc(paragraphs: list[Paragraph], full_md_text: str,
                     min_coverage: float = MIN_COVERAGE) -> int:
    """[全局] 本地结构 + MinerU full.md LaTeX 文本的段落级融合（段落守恒）

    参数:
        paragraphs: 本地 stitch 段落（Paragraph 列表；text_en 原地替换为 LaTeX 版）
        full_md_text: MinerU 高精度 full.md 全文（唯一文本主体）
        min_coverage: 归属的 full 段长度和 / 本地段长度 下限
    返回:
        被替换文本的段落数
    报错:
        无（未匹配段保留本地文本）
    """
    full_items = _full_paras(full_md_text)
    full_comp = [(i, _compact(t)) for i, (_h, t) in enumerate(full_items)]
    full_grams = [_ngrams(c) for _i, c in full_comp]

    # 1) 归属：每个 full 段只归属第一个覆盖它的本地段（段落守恒；n-gram 覆盖率判据）
    local_comp = [_compact(p.text_en) for p in paragraphs]
    local_grams = [_ngrams(c) for c in local_comp]
    assigned: dict[int, list[int]] = {}
    for fi, (i, fc) in enumerate(full_comp):
        if len(fc) < MIN_FULL_LEN:
            continue
        gf = full_grams[fi]
        if not gf:
            continue
        for pi, gp in enumerate(local_grams):
            if not gp:
                continue
            if len(gf & gp) / len(gf) >= NGRAM_COVER:
                assigned.setdefault(pi, []).append(i)
                break

    # 2) 本地段文本重建（归属的 full 段按**在本地段中的出现位置**排序——full.md 中
    # MinerU 可能错排段序（如 "Electro-ionic…" 被放到摘要区），拼接须保持本地阅读顺序）
    replaced = 0
    for pi, fis in assigned.items():
        pc = local_comp[pi]
        # 按 full 段 compact 在本地段 compact 中的出现位置排序（未出现的放最后）
        ordered = sorted(fis, key=lambda i: (
            pc.find(full_comp[i][1]) if full_comp[i][1] in pc else 10 ** 9))
        texts = [full_items[i][1] for i in ordered]
        merged = " ".join(t.strip() for t in texts).strip()
        covered = sum(len(full_comp[i][1]) for i in fis)
        if (merged and covered >= len(pc) * min_coverage
                and merged != paragraphs[pi].text_en):
            paragraphs[pi].text_en = merged
            replaced += 1
    return replaced
