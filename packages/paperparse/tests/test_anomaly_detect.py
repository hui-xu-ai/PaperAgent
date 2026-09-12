#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: tests/test_anomaly_detect.py
功能: 异常片段检测（anomaly_detect）测试：
      - �（U+FFFD）替换字符检测
      - 可疑 LaTeX（反斜杠 Nu_2 等）检测
      - 修复清单渲染（含本地提示）
对外接口: 无（测试）
版本: v1.0.0 (2026-08-18)
版本历史:
  v1.0.0 初始版本
"""
import pytest

from paperparse.core.anomaly_detect import apply_known_fixes, detect_anomalies, render_fix_list
from paperparse.middleware.schema import ArticleDocument, ArticleMetadata, Paragraph


def _doc(*texts):
    return ArticleDocument(
        metadata=ArticleMetadata(extraction_time="x"),
        paragraphs=[Paragraph(para_id="P%03d" % (i + 1), order=i + 1,
                              section="S", text_en=t, confidence=0.9)
                    for i, t in enumerate(texts)])


def test_detect_replacement_char():
    """�（U+FFFD）检测并记录句子"""
    doc = _doc("normal text", "poly(�-caprolactone) (PCL) aligned fibers.")
    items = detect_anomalies(doc)
    assert len(items) == 1
    assert "poly" in items[0]["sentence"]
    assert items[0]["para_id"] == "P002"


def test_detect_suspicious_latex():
    """可疑 LaTeX（反斜杠 Nu_2，应为 N2 氮气）检测"""
    doc = _doc("thermal stability in an inert $\\Nu _ { 2 }$ atmosphere.")
    items = detect_anomalies(doc)
    assert items and "Nu" in items[0]["sentence"]


def test_local_hint():
    """本地提示：从 PyMuPDF 文本中找对应片段（备用识别源）"""
    doc = _doc("value is poly(�-caprolactone) here.")
    items = detect_anomalies(doc, local_texts=["poly( 𝜖 -caprolactone) (PCL)"])
    assert items
    assert "𝜖" in items[0]["local_hint"]


def test_render_fix_list(tmp_work):
    """修复清单渲染（可读文本 + JSON 落盘）"""
    doc = _doc("poly(�-caprolactone) text.")
    items = detect_anomalies(doc)
    out = tmp_work / "fix.txt"
    text = render_fix_list(items, out)
    assert "异常片段修复清单" in text
    assert out.exists() and out.with_suffix(".json").exists()


def test_repair_garbled_replacement(samples_dir):
    """乱码修复（用户需求）：含 U+FFFD 段落按 coords 调本地 PyMuPDF 提取对应片段替换 �"""
    import pymupdf

    from paperparse.core.anomaly_detect import repair_garbled
    from paperparse.middleware.schema import ArticleDocument, ArticleMetadata, Paragraph
    pdf = samples_dir / "10.1002_adma.202407106.pdf"
    with pymupdf.open(str(pdf)) as pdfdoc:
        page_no, txt, rect = None, "", None
        for i in range(pdfdoc.page_count):
            t = pdfdoc[i].get_text("text") or ""
            if "work function" in t:
                page_no, txt, rect = i, t, pdfdoc[i].rect
                break
    assert page_no is not None, "样本 PDF 应含 'work function' 片段"
    idx = txt.index("work function")
    broken = txt[: idx + 4] + "\uFFFD" + txt[idx + 5: idx + 30]
    para = Paragraph(
        para_id="P099", order=1, section="S", text_en=broken, confidence=0.9,
        coords={"pages": [page_no + 1],
                "bbox0": [0.0, 0.0, float(rect.width), float(rect.height)]})
    doc = ArticleDocument(metadata=ArticleMetadata(extraction_time="x"), paragraphs=[para])
    fixed = repair_garbled(doc, str(pdf))
    assert fixed == 1
    assert "\uFFFD" not in doc.paragraphs[0].text_en
    assert "work function" in doc.paragraphs[0].text_en


def test_apply_known_fixes_bar2_both_en_and_zh():
    """用户需求 #2 根因：源文 \\mathsf{A m}^{\\bar{2}} 应为 ^2（OCR 误加 bar）——
    apply_known_fixes 同时修正 text_en 与 text_zh（译文/题注）"""
    bad = "remanence of1.01 $\\mathsf { A } \\mathsf { m } ^ { \\bar { 2 } } \\mathrm { k g } ^ { - 1 }$ ."
    doc = _doc(bad)
    doc.paragraphs[0].text_zh = ("剩磁 1.01 $\\mathsf { A } \\mathsf { m } ^ { \\bar { 2 } } "
                                 "\\mathrm { k g } ^ { - 1 }$。")
    fixed = apply_known_fixes(doc)
    assert fixed == 1
    en = doc.paragraphs[0].text_en
    zh = doc.paragraphs[0].text_zh
    assert "^ { \\bar { 2 } }" not in en and "^ { \\bar { 2 } }" not in zh
    assert "^ { 2 }" in en and "^ { 2 }" in zh
    assert "\\mathsf { A } \\mathsf { m } ^ { 2 }" in en


def test_apply_known_fixes_control_char_bar2_in_zh():
    """回归：译文里 \\bar 的反斜杠被控制字符（\\x08 退格）污染成 '^ { <ctrl>ar { 2 } }'——
    apply_known_fixes 应识别并修复为 ^ { 2 }（仅测单段 zh）"""
    from paperparse.middleware.schema import Paragraph
    en = ("The nanoscale active material achieved a saturated magnetization, "
          "and a small remanence of 1.01 $\\mathsf { A } \\mathsf { m } ^ { 2 } "
          "\\mathrm { k g } ^ { - 1 }$ .")
    # 译文含 \x08（BACKSPACE）替代反斜杠
    zh = ("活性材料实现了 1.01 $\\mathsf { A } \\mathsf { m } ^ { \x08ar { 2 } } "
          "\\mathrm { k g } ^ { - 1 }$ 的低剩磁。")
    doc = ArticleDocument(
        metadata=ArticleMetadata(extraction_time="x"),
        paragraphs=[Paragraph(para_id="P008", order=1, section="S",
                              text_en=en, confidence=0.9)])
    doc.paragraphs[0].text_zh = zh
    assert "\x08ar { 2 }" in zh          # 前置：确实含污染
    fixed = apply_known_fixes(doc)
    assert fixed == 1
    z2 = doc.paragraphs[0].text_zh
    assert "\x08ar { 2 }" not in z2
    assert "\\mathsf { A } \\mathsf { m } ^ { 2 }" in z2
