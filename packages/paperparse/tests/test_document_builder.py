#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: tests/test_document_builder.py
功能: T12 文档构建单元测试（DOI 目录名 / 参考文献解析 / 单一事实源往返）
对外接口: 无（测试）
版本: v1.0.0 (2025-xx-xx)
版本历史:
  v1.0.0 初始版本
"""
import json

from paperparse.core.document_builder import (
    build_document,
    dedupe_paragraphs,
    doi_dir_name,
    extract_references,
    load_document,
    mark_abstract_section,
    save_document,
)
from paperparse.core.stitch_code import stitch
from paperparse.middleware.schema import (
    ArticleMetadata,
    LayoutInfo,
    Reference,
    TextBlock,
)

from tests.test_layout import _two_column_blocks  # 复用合成块


def test_doi_dir_name():
    assert doi_dir_name("10.1002/adma.202407106") == "10.1002_adma.202407106"
    assert doi_dir_name(None).startswith("paper_")
    assert len(doi_dir_name(None)) == 14  # "paper_" + 8 位 hash


def test_extract_references():
    blocks = [
        TextBlock(block_id="R1", page=14, bbox=(0, 100, 100, 120), text="[1] A. Author, J. Mag. 2020, 5, 1."),
        TextBlock(block_id="R2", page=14, bbox=(0, 130, 100, 150), text="continuation of ref one."),
        TextBlock(block_id="R3", page=14, bbox=(0, 160, 100, 180), text="[2] B. Writer, Adv. Mater. 2021, 33, 2."),
        TextBlock(block_id="R4", page=15, bbox=(0, 100, 100, 120), text="www.boilerplate.com"),
        TextBlock(block_id="R5", page=15, bbox=(0, 130, 100, 150), text="[3] C. Editor, Nature 2022, 10, 3."),
    ]
    for i, b in enumerate(blocks):
        b.order = i
    layout = LayoutInfo(page_count=15, reference_zone_pages=[14, 15])
    refs = extract_references(blocks, layout)
    assert [r.number for r in refs] == [1, 2, 3]
    assert "continuation of ref one" in refs[0].raw_text
    assert refs[2].raw_text == "[3] C. Editor, Nature 2022, 10, 3."


def test_build_and_roundtrip(tmp_work):
    from paperparse.middleware.schema import Paragraph, StitchResult
    meta = ArticleMetadata(title="T", authors=["A"], keywords=["k1"],
                           doi="10.1002/adma.202407106")
    sr = StitchResult(paragraphs=[
        Paragraph(para_id="P001", order=1, text_en="Hello.", confidence=0.9)])
    refs = [Reference(ref_id="R1", number=1, raw_text="[1] X.")]
    doc = build_document(meta, sr, references=refs, run_id="run-1",
                         warnings=["w1"])
    p = save_document(doc, tmp_work / "document.json")
    doc2 = load_document(p)
    assert doc2.metadata.doi == "10.1002/adma.202407106"
    assert doc2.references[0].raw_text == "[1] X."
    assert doc2.audit.run_id == "run-1"
    # JSON 可序列化
    json.dumps(doc2.model_dump(mode="json"))


def _long_para(pid: str, section: str, text: str) -> "Paragraph":
    """[局部] 构造 >=200 字符长正文段（参与重复检查）"""
    from paperparse.middleware.schema import Paragraph
    return Paragraph(para_id=pid, order=0, text_en=text, section=section,
                     confidence=0.9)


def test_dedupe_exact_keep_first():
    from paperparse.middleware.schema import Paragraph
    p1 = _long_para("P001", "", "A " * 120)          # 长段（240+ 字符）
    p2 = Paragraph(para_id="P002", order=1, text_en="A " * 120, section="",
                   confidence=0.9)
    paras = [p1, p2]
    removed = dedupe_paragraphs(paras)
    assert removed == 1
    assert [x.para_id for x in paras] == ["P001"]    # 保留首个出现


def test_dedupe_near_duplicate_collapsed():
    """近重复（少量字符差异）大段应被识别为重复并删除"""
    from paperparse.middleware.schema import Paragraph
    base = ("Efficient ion transport and enriched responsive modals via "
            "modulating electrochemical properties of conductivity and capacitance "
            "are essential for soft electro ionic actuators in modern soft robotics "
            "applications across biomedical and industrial fields today. ")
    base = base * 3                                    # 足够长（>200 字符）
    p1 = _long_para("P001", "", base)
    p2 = _long_para("P002", "", base.replace("electrochemical", "electro chemical")
                    .replace("actuators", "actuators "))   # 轻微差异 → 近重复
    paras = [p1, p2]
    removed = dedupe_paragraphs(paras)
    assert removed == 1
    assert [x.para_id for x in paras] == ["P001"]


def test_dedupe_abstract_anchor_prefers_correct_side():
    """摘要片段同时出现在 Abstract 标题前（错位）与 ## Abstract 后（正确）时，
    保留正确位置的副本，删除错位前置副本（用户问题 1 场景）"""
    from paperparse.middleware.schema import Paragraph
    text = ("Efficient ion transport and enriched responsive modals via modulating "
            "electrochemical properties of conductivity and capacitance are essential "
            "for soft electro-ionic actuators across modern biomedical soft robotics "
            "demonstrations. ")
    text = text * 3
    misplaced = _long_para("P001", "", text)                # 前置错位副本（section 空）
    correct = Paragraph(para_id="P002", order=1, text_en=text, section="Abstract",
                        confidence=0.9)
    paras = [misplaced, correct]
    dedupe_paragraphs(paras)                                # P002 已带 section="Abstract"
    assert [x.para_id for x in paras] == ["P002"]           # 保留正确位置副本


def test_mark_abstract_ocr_noise_defects():
    """回归（用户复现）：本地版摘要含 OCR 缺陷（ofconductivity 粘连、eficient 丢字符、
    ±0.5 vs ± 0.5 数字空格）时，与 metadata.abstract 的相似度被数字/粘连噪声拉低，
    原 Dice<0.9 导致 mark_abstract_section 失效、渲染层重复输出摘要；
    数字 token 过滤后应恢复命中，且去重能删除后续重复副本。"""
    from paperparse.middleware.schema import ArticleDocument, ArticleMetadata, Paragraph
    abstract = ("Efficient ion transport and enriched responsive modals via modulating "
                "electrochemical properties of conductivity and capacitance are essential "
                "for soft electro-ionic actuators. The developed actuator exhibits "
                "peak-to-peak displacement of 13.08 mm at an ultra-low ± 0.5 V, with "
                "doubled direct current deflection under 200 mT at 1 V.")
    ocr = ("Eficient ion transport and enriched responsive modals via modulating "
           "electrochemical properties ofconductivity and capacitance are essential "
           "for soft electro-ionic actuators. The developed actuator exhibits "
           "peak-to-peak displacement of 13.08 mm at an ultra-low ±0.5 V, with "
           "doubled direct current deflection under 200 mT at 1 V.")
    doc = ArticleDocument(
        metadata=ArticleMetadata(title="T", abstract=abstract, extraction_time="x"),
        paragraphs=[
            Paragraph(para_id="P001", order=0, section="", text_en="1. Introduction",
                      confidence=0.9),
            Paragraph(para_id="P002", order=1, section="", text_en=ocr, confidence=0.9),
        ])
    n = mark_abstract_section(doc)
    assert n == 1
    assert doc.paragraphs[1].section == "Abstract"
    # 去重：错位前置副本（干净版）应被删除，保留 Abstract 锚定副本
    paras = [doc.paragraphs[1],
             Paragraph(para_id="P003", order=2, section="", text_en=abstract,
                       confidence=0.9)]
    removed = dedupe_paragraphs(paras)
    assert removed == 1
    assert [p.para_id for p in paras] == ["P002"]


def test_build_document_runs_dedupe_and_abstract_mark(tmp_work):
    """build_document 自动执行 摘要标记 + 去重（含 Abstract 锚定），并写审计警告"""
    from paperparse.middleware.schema import Paragraph, StitchResult
    text = ("Differentiation and proliferation of stem cells under mechanical strain "
            "and electrical stimuli sheds light on facilitating biomedical soft "
            "robotics with ultrahigh actuation performance in real environments. ")
    text = text * 3
    meta = ArticleMetadata(title="T", abstract=text.strip(), doi="10.x/y")
    misplaced = Paragraph(para_id="P001", order=0, text_en=text, section="",
                          confidence=0.9)
    correct = Paragraph(para_id="P002", order=1, text_en=text, section="",
                        confidence=0.9)
    sr = StitchResult(paragraphs=[misplaced, correct])
    doc = build_document(meta, sr)
    ids = [p.para_id for p in doc.paragraphs]
    assert ids == ["P002"]                                  # 保留 Abstract 锚定副本
    assert doc.paragraphs[0].section == "Abstract"
    assert any("重复清理" in w for w in doc.audit.warnings)
    # 删除清单反馈：审计警告含 removed/kept 细节（用户：反馈删除了哪些内容）
    assert any("删除清单" in w and "P001" in w for w in doc.audit.warnings)


def test_mark_abstract_fuzzy_after_latex_fusion():
    """v2.2 回归：LaTeX 融合后摘要段（mineru 版含 $/\\mathrm 残留）也能被标记为 Abstract
    （原精确相等匹配失效 → 摘要未标记 → 渲染去重失效 → 摘要重复）"""
    from paperparse.middleware.schema import ArticleDocument, ArticleMetadata, Paragraph
    local = ("Efficient ion transport and enriched responsive modals are essential "
             "for soft electro-ionic actuators, with peak displacement of 13.08 mm.")
    fused = ("Eﬃcient ion transport and enriched responsive modals are essential for soft "
             "electro-ionic actuators, with peak displacement of ${ \\mathrm { 1 3 . 0 8 } }$ "
             "$\\mathrm { m m }$.")
    doc = ArticleDocument(
        metadata=ArticleMetadata(title="T", abstract=local, extraction_time="x"),
        paragraphs=[
            Paragraph(para_id="P001", order=0, section="", text_en="Intro.", confidence=0.9),
            Paragraph(para_id="P002", order=1, section="", text_en=fused, confidence=0.9),
        ])
    n = mark_abstract_section(doc)
    assert n == 1
    assert doc.paragraphs[1].section == "Abstract"


