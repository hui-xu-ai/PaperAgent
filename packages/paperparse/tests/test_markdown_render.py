#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: tests/test_markdown_render.py
功能: T13 模板渲染测试（frontmatter/tags/双语 details/图片/参考文献 + golden）
对外接口: 无（测试）
版本: v1.0.0 (2025-xx-xx)
版本历史:
  v1.0.0 初始版本
"""
from pathlib import Path

from paperparse.core.markdown_render import list_templates, render
from paperparse.middleware.schema import (
    ArticleDocument,
    ArticleMetadata,
    Figure,
    Paragraph,
    Reference,
)

GOLDEN = Path(__file__).resolve().parent / "golden" / "obsidian_bilingual.md"


def _doc() -> ArticleDocument:
    return ArticleDocument(
        metadata=ArticleMetadata(
            title="Test Paper", authors=["A. One", "B. Two"],
            keywords=["artificial muscle", "soft actuator"],
            doi="10.1002/adma.202407106", year=2024,
            abstract="Abstract text.",
            extraction_time="2025-01-01T00:00:00+00:00"),
        paragraphs=[
            Paragraph(para_id="P001", order=1, text_en="2. Results", is_heading=True, confidence=1.0),
            Paragraph(para_id="P002", order=2, section="2. Results",
                      text_en="English paragraph text here.", confidence=0.9),
            Paragraph(para_id="P003", order=3, section="2. Results",
                      text_en="English original.", text_zh="中文译文。", confidence=0.9),
            Paragraph(para_id="P004", order=4, section="2. Results",
                      text_en="Fig. 1. Caption text.", is_caption=True, confidence=1.0),
        ],
        figures=[Figure(fig_id="F001", file="images/F001.png",
                        caption="Fig. 1. Caption text.", page=1)],
        references=[Reference(ref_id="R1", number=1, raw_text="[1] Some ref.")],
    )


def test_frontmatter_and_tags():
    md = render(_doc())
    assert "---" in md
    assert 'tags: ["文献", "artificial_muscle", "soft_actuator"]' in md
    assert 'title: "Test Paper"' in md
    assert 'doi: "10.1002/adma.202407106"' in md


def test_bilingual_details():
    """双语：英文在上、中文直接在下（无 <details> 折叠，用户需求）；英文与中文间一个空白行"""
    md = render(_doc())
    assert "中文译文。" in md
    assert "English original." in md
    # 无折叠标签
    assert "<details>" not in md
    assert "英文原文" not in md
    # 英文在上、中文在下，且之间即一个空白行
    ei = md.index("English original.")
    zi = md.index("中文译文。")
    assert ei < zi
    between = md[ei + len("English original."): zi]
    assert between.strip(" \n") == ""        # 中间无其他内容
    assert between.count("\n") == 2          # 恰好一个空白行（en 与 zh 之间：段末换行+空行）


def test_figure_and_references():
    md = render(_doc())
    assert "![](images/F001.png)" in md
    assert "## References" in md
    # 参考文献：列表序号 + 无 "[n]" 前缀（避免 Obsidian 复选框解析，用户问题 6）
    assert "1. [1] Some ref." not in md
    assert "1. Some ref." in md


def test_figure_before_caption():
    """T-C：图必须插在对应题注之前（Obsidian 习惯：先图后题注）"""
    md = render(_doc())
    img = md.find("![](images/F001.png)")
    cap = md.find("Fig. 1. Caption text.")
    assert img != -1 and cap != -1 and img < cap


def test_plain_template_no_details():
    md = render(_doc(), template="plain")
    assert "English paragraph text here." in md
    assert "<details>" not in md


def test_golden():
    """golden 测试：模板结构变更必须显式更新 tests/golden/obsidian_bilingual.md"""
    md = render(_doc())
    if not GOLDEN.exists():
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(md, encoding="utf-8")
        return  # 首次生成基线
    assert md == GOLDEN.read_text(encoding="utf-8")


def test_list_templates():
    names = list_templates()
    assert "obsidian_bilingual" in names
    assert "plain" in names
    assert "recognized" in names


def test_summary_fields_rendered_as_separate_blocks():
    """v1.4.0：AI 总结六字段各自独立成段（行间空引用行），不再整段糊在一起"""
    from paperparse.middleware.schema import ArticleDocument, ArticleMetadata, Paragraph
    doc = ArticleDocument(
        metadata=ArticleMetadata(title="T", abstract="Abs", extraction_time="x"),
        paragraphs=[Paragraph(para_id="P001", order=0, section="1. Intro",
                              text_en="Intro.", confidence=0.9)],
        ai_summary={"研究背景": "背景内容", "研究方法": "方法内容",
                    "研究结果": "结果内容", "结论": "结论内容",
                    "创新点": "创新内容", "局限": "局限内容"})
    md = render(doc)
    # 每个字段独立 `> **键**: 值` 行，且字段之间有空的引用行 `>`（分段）
    assert "> **研究背景**: 背景内容" in md
    assert "> **研究方法**: 方法内容" in md
    idx_bg = md.index("> **研究背景**: 背景内容")
    idx_m = md.index("> **研究方法**: 方法内容")
    between = md[idx_bg:idx_m]
    assert "\n>\n" in between or between.count("\n") >= 2   # 字段间有分隔


def test_abstract_section_inserted():
    """有摘要且无 Abstract 标题段落 → 自动插入 ## Abstract（用户问题 4）"""
    md = render(_doc())
    assert "## Abstract" in md
    assert "Abstract text." in md


def test_abstract_not_duplicated_when_body_para_present():
    """回归（用户反馈）：摘要正文段（section=='Abstract'，含译文）只渲染一次、
    位于 ## Abstract 之后，不因连字差异与 metadata.abstract 重复"""
    doc = ArticleDocument(
        metadata=ArticleMetadata(
            title="T", abstract="Eﬃcient ion transport and enriched responsive modals.",
            extraction_time="2025-01-01T00:00:00+00:00"),
        paragraphs=[
            Paragraph(para_id="P001", order=0, text_en="T", is_heading=True, confidence=1.0),
            # 摘要正文段：文本与 metadata.abstract 存在连字差异（Eficient vs Eﬃcient）
            Paragraph(para_id="P002", order=1, section="Abstract",
                      text_en="Eficient ion transport and enriched responsive modals.",
                      text_zh="中文摘要译文。", confidence=0.9),
            Paragraph(para_id="P003", order=2, section="1. Introduction",
                      text_en="Intro body.", confidence=0.9),
        ])
    md = render(doc)
    # 摘要段落（以连字差异开头的"Eficient"为唯一标识）整段只出现一次
    assert md.count("Eficient ion transport") == 1
    assert "中文摘要译文。" in md              # 摘要正文段译文保留
    # ## Abstract 标题必须在摘要段落之前
    assert md.index("## Abstract") < md.index("Eficient ion transport")
    # metadata.abstract（连字 "Eﬃcient"）不得被重复插入为第二段
    assert md.count("Eﬃcient ion transport") == 0
    assert "## Abstract" in md


def test_heading_dedup():
    """正文不重复标题/作者（用户问题 3）"""
    doc = _doc()
    doc.paragraphs.insert(0, Paragraph(
        para_id="P000", order=0, text_en="Test Paper", is_heading=True, confidence=1.0))
    md = render(doc)
    # 标题段落被跳过：## Test Paper 不出现（H1 之外无重复）
    assert md.count("Test Paper") == 2          # H1 + frontmatter title
    assert "## Test Paper" not in md


def test_recognized_variant_excludes_sections():
    """recognized 变体：不含参考文献/致谢/COI 等章节（用户问题 13）"""
    doc = _doc()
    doc.paragraphs.append(Paragraph(
        para_id="P010", order=10, text_en="Acknowledgements", is_heading=True, confidence=1.0))
    doc.paragraphs.append(Paragraph(
        para_id="P011", order=11, section="Acknowledgements",
        text_en="We thank funding.", confidence=1.0))
    md = render(doc, template="recognized", exclude_sections={"acknowledgements", "references"})
    assert "We thank funding" not in md
    assert "## Acknowledgements" not in md
    assert "English paragraph text here." in md


def test_article_type_in_frontmatter_and_tags():
    doc = _doc()
    doc.metadata.article_type = "Research Article"
    md = render(doc)
    assert 'type: "Research Article"' in md
    assert '"Research_Article"' in md            # tags 含文章类型
