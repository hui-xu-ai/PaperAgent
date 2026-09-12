# -*- coding: utf-8 -*-
"""skeleton_fix 单测（P-ENHANCE R02 / S6 骨架标题校正）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "paperparse"))

from paperparse.core.skeleton_fix import apply_skeleton_headings  # noqa: E402
from paperparse.core.layout_skeleton import LayoutSkeleton, SkeletonItem  # noqa: E402
from paperparse.middleware.schema import ArticleDocument, Paragraph  # noqa: E402


def _para(pid, text, is_heading=False, is_caption=False, section=""):
    return Paragraph(para_id=pid, order=0, section=section, text_en=text,
                     is_heading=is_heading, is_caption=is_caption)


def _doc(paras):
    return ArticleDocument(metadata={"title": "t"}, paragraphs=paras)


def _skel(heads, with_items=False):
    tree = [{"heading": h, "level": 2, "items": [i]} for i, h in enumerate(heads)]
    return LayoutSkeleton(items=[], stats={}, section_tree=tree)


def test_demote_local_noise_heading():
    # 本地误判 heading（"S5 and S6). This..." 乱拼段）与骨架无匹配 → 降级 body
    doc = _doc([
        _para("P001", "INTRODUCTION", is_heading=True),
        _para("P002", "(pH 11) for infiltration of PEI into the hydrogel.", is_heading=True),
        _para("P003", "Some body text here."),
    ])
    n = apply_skeleton_headings(doc, _skel(["INTRODUCTION", "RESULTS"]))
    assert n >= 1
    assert doc.paragraphs[1].is_heading is False      # 乱拼段降级
    assert doc.paragraphs[0].is_heading is True


def test_caption_not_marked_heading():
    # 图注段含骨架 heading 子串 → 不得标 heading（caption 保护）
    doc = _doc([
        _para("P001", "INTRODUCTION", is_heading=True),
        _para("P002", "Fig. 2. Preparation of WNE on hydrogel. Photo...", is_caption=True),
    ])
    apply_skeleton_headings(doc, _skel(["INTRODUCTION", "Preparation of WNE on hydrogel"]))
    assert doc.paragraphs[1].is_heading is False


def test_split_glued_heading_body():
    # 标题+正文粘连（"Performance of WNE actuators Upon confirming..."）→ 拆分
    doc = _doc([
        _para("P001", "INTRODUCTION", is_heading=True),
        _para("P002", "Performance of WNE actuators Upon confirming the noteworthy properties",
              is_heading=True),
    ])
    n = apply_skeleton_headings(doc, _skel(["INTRODUCTION", "Performance of WNE actuators"]))
    assert n >= 2
    texts = [p.text_en for p in doc.paragraphs]
    assert "Performance of WNE actuators" in texts           # heading 段
    assert any("Upon confirming" in t for t in texts)        # 正文段
    h = [p for p in doc.paragraphs if p.is_heading]
    assert len(h) == 2


def test_insert_missing_in_order():
    # 缺失 heading（RESULTS 在 INTRODUCTION 与 Preparation 之间）→ 按序插入
    doc = _doc([
        _para("P001", "INTRODUCTION", is_heading=True),
        _para("P002", "Preparation of WNEs on hydrogels", is_heading=True),
        _para("P003", "Body of preparation section."),
    ])
    n = apply_skeleton_headings(
        doc, _skel(["INTRODUCTION", "RESULTS", "Preparation of WNEs on hydrogels"]))
    assert n >= 1
    heads = [p.text_en for p in doc.paragraphs if p.is_heading]
    assert heads.index("RESULTS") == heads.index("INTRODUCTION") + 1


def test_exact_match_kept():
    doc = _doc([_para("P001", "1. Introduction", is_heading=True)])
    n = apply_skeleton_headings(doc, _skel(["1. Introduction"]))
    assert n == 0
    assert doc.paragraphs[0].is_heading is True
