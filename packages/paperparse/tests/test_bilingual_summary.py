#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: tests/test_bilingual_summary.py
功能: 摘要段双语渲染回归测试（用户反馈：摘要区顺序混乱「中/英/中/中」重复）
      - 标题段被误标为 body（is_heading=False，text_en 带 "# " 前缀）且其 text_zh 被误填为
        摘要译文时，不得把摘要中文提前/重复渲染（根因：_build_items 未对 body 标题段去重，
        render_clean 已有，_build_items 缺失）。
      - 多段摘要保持「英上中下」顺序、不交错。
      - zh_only 纯中文版摘要段中文只出现一次。
对外接口: 无（测试）
"""
from paperparse.core.markdown_render import render_variant
from paperparse.middleware.schema import (
    ArticleDocument,
    ArticleMetadata,
    Paragraph,
)

# 摘要中文唯一标识（用于统计出现次数）
ABS_ZH = "中文摘要内容。"
ABS_EN = "Abstract English body text."
TITLE = "Paper Title"


def _doc(paragraphs, *, abstract=None):
    return ArticleDocument(
        metadata=ArticleMetadata(
            title=TITLE, authors=["A", "B"], abstract=abstract,
            doi="10.0/x", year=2025, extraction_time="2025-01-01T00:00:00+00:00"),
        paragraphs=paragraphs,
    )


def _abs_region(md):
    """抽取摘要区：从 '## Abstract' 起，或（无摘要标题时）从 frontmatter 后到首个编号标题前的正文区。
    返回 (是否含 Abstract 标题, 文本)。"""
    lines = md.splitlines()
    start = end = None
    has_hdr = False
    for i, ln in enumerate(lines):
        if ln.startswith("## ") and ln[3:].strip().replace(" ", "").lower().startswith("abstract"):
            start = i
            has_hdr = True
            break
    if start is None:
        seen = 0
        for i, ln in enumerate(lines):
            if ln.strip() == "---":
                seen += 1
                if seen == 2:
                    start = i + 1
                    break
        for j in range(start or 0, len(lines)):
            ln = lines[j]
            if ln.startswith("## ") and (ln[3:].strip()[:1].isdigit() or
                                         ln[3:].strip().lower().startswith("references")):
                end = j
                break
        if end is None:
            end = len(lines)
    return has_hdr, lines[start:end]


def test_title_body_para_with_abstract_zh_not_duplicated():
    """回归（用户反馈）：标题段被误标为 body（text_en 带 '# ' 前缀），且其 text_zh 被误填为
    摘要译文 → 不得在摘要区提前/重复渲染摘要中文；摘要应英上中下、只出现一次。"""
    doc = _doc([
        # 标题段：误标为 body，text_zh 被误填为摘要中文（本 bug 根因）
        Paragraph(para_id="P001", order=0, text_en="# " + TITLE, text_zh=ABS_ZH),
        # 摘要正文段（含正确英文 + 中文译文）
        Paragraph(para_id="P002", order=1, text_en=ABS_EN, text_zh=ABS_ZH),
        Paragraph(para_id="P003", order=2, text_en="Intro.", section="1. Introdu", is_heading=True),
    ])
    md = render_variant(doc, "translated")
    # 摘要中文只出现一次（之前因 P001.text_zh 提前渲染 + P002.text_zh 各一次 → 两次）
    assert md.count(ABS_ZH) == 1, "摘要中文不应重复出现: %d 次" % md.count(ABS_ZH)
    # 摘要英文只出现一次，且位于其中文之前（英上中下）
    assert md.count(ABS_EN) == 1, "摘要英文不应重复出现: %d 次" % md.count(ABS_EN)
    assert md.index(ABS_EN) < md.index(ABS_ZH), "摘要应英上中下"
    # 标题段（body）被正确跳过：正文只出现一个 H1 标题，不再作为正文段二次渲染（连带 text_zh）
    h1_lines = [ln for ln in md.splitlines() if ln.startswith("# ")]
    assert h1_lines == ["# " + TITLE], "标题应只渲染为单个 H1: %r" % h1_lines


def test_multi_paragraph_abstract_keeps_en_then_zh_order():
    """多段摘要：每段英上中下，整体段落顺序保持、不交错、不重复。"""
    doc = _doc([
        Paragraph(para_id="P001", order=0, text_en="## ABSTRACT", is_heading=True),
        Paragraph(para_id="P002", order=1, text_en="First part.", text_zh="第一部分。", section="Abstract"),
        Paragraph(para_id="P003", order=2, text_en="Second part.", text_zh="第二部分。", section="Abstract"),
    ])
    md = render_variant(doc, "translated")
    has_hdr, region = _abs_region(md)
    assert has_hdr, "应存在 ## Abstract 标题"
    joined = "\n".join(region)
    # 顺序：First(EN) < 第一部分(ZH) < Second(EN) < 第二部分(ZH)
    assert joined.index("First part.") < joined.index("第一部分。") \
           < joined.index("Second part.") < joined.index("第二部分。")
    # 无重复
    assert joined.count("First part.") == 1
    assert joined.count("第二部分。") == 1


def test_zh_only_variant_abstract_once():
    """zh_only 纯中文版：摘要正文中文只出现一次（标题段误填的摘要中文不得重复）。"""
    doc = _doc([
        Paragraph(para_id="P001", order=0, text_en="# " + TITLE, text_zh=ABS_ZH),
        Paragraph(para_id="P002", order=1, text_en=ABS_EN, text_zh=ABS_ZH),
    ])
    zh_md = render_variant(doc, "zh")
    assert zh_md.count(ABS_ZH) == 1, "zh_only 摘要中文不应重复: %d 次" % zh_md.count(ABS_ZH)


def test_abstract_with_meta_abstract_and_body_para_no_double():
    """metadata.abstract 与摘要正文段共存：摘要只渲染一次（正文段承载内容，元数据不重复插入）。"""
    doc = _doc([
        Paragraph(para_id="P001", order=0, text_en="## A B S T R A C T", is_heading=True),
        Paragraph(para_id="P002", order=1, text_en=ABS_EN, text_zh=ABS_ZH, section="Abstract"),
    ], abstract=ABS_EN)
    md = render_variant(doc, "translated")
    assert md.count(ABS_EN) == 1, "metadata.abstract 不应与正文段重复: %d 次" % md.count(ABS_EN)
    assert md.count(ABS_ZH) == 1
    assert "## Abstract" in md
