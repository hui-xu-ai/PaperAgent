#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: tests/test_image_extract.py
功能: T10 图片提取（大图保留/去重/小图过滤/图注关联）单元测试
对外接口: 无（测试）
版本: v1.0.0 (2025-xx-xx)
版本历史:
  v1.0.0 初始版本
"""
import pymupdf

from paperparse.core.image_extract import extract_figures
from paperparse.core.pymupdf_fallback import extract_blocks


def _make_fig_pdf(path):
    doc = pymupdf.open()
    page = doc.new_page()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 100, 100))
    pix.clear_with(150)
    # 同一张大图插入两次（应去重为 1 张）
    page.insert_image(pymupdf.Rect(60, 100, 260, 300), pixmap=pix)
    page.insert_image(pymupdf.Rect(60, 350, 260, 550), pixmap=pix)
    # 小图（装饰，应过滤）
    small = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 20, 20))
    small.clear_with(90)
    page.insert_image(pymupdf.Rect(60, 600, 80, 620), pixmap=small)
    # 图注（在图上方的测试布局里放于图下方）
    page.insert_text((72, 580), "Fig. 1. Test caption for the figure.", fontsize=10)
    doc.save(str(path))
    doc.close()


def test_extract_figures_dedup_and_caption(tmp_work):
    pdf = tmp_work / "figs.pdf"
    _make_fig_pdf(pdf)
    from paperparse.core.block_classify import classify_lines
    blocks = extract_blocks(str(pdf)).blocks
    classify_lines(blocks)                      # 管线顺序：S1 提取 → S2 分类 → S4 图注关联
    figs = extract_figures(str(pdf), blocks, tmp_work / "images", dpi=72)
    assert len(figs) == 1
    fig = figs[0]
    assert fig.fig_id == "F001"
    assert fig.file == "images/F001.png"
    assert (tmp_work / "images" / "F001.png").exists()
    assert fig.caption == "Fig. 1. Test caption for the figure."
    assert fig.page == 1
    assert len(fig.sha256) == 64


class TestCaptionDrivenAnchorSplit:
    """P3 缺陷（2026-09-17）：并排图注被骨架并成一个 caption 段时丢图。

    NC p5：左栏 Figure 4 ＋ 右栏 Figure 5 并排 → 骨架 caption 段两行
    （"Figure 5 | …" / "Figure 4 | …"），段 bbox 43.6→548.8 横跨分栏缝。
    旧版只认段首行 → Figure 4 无锚点 → 只出 5 张图。
    """

    def test_caption_label_matching(self):
        from paperparse.core.image_extract import _match_caption_label as m
        assert m("Figure 5 | Nitrogen").group(2) == "5"
        assert m("(c) Fig. 12. x").group(2) == "12"            # 括号面板标签
        assert m("[b] Figure 7 | y").group(2) == "7"
        assert m("In Fig. 2 we show") is None                  # 正文句不入锚点
        assert m("b Figure 4 | x") is None                     # 裸字母不剥离
        assert m("Table 2 | Values") is None                   # 表/方案走旧路径
        assert m("Scheme 3. Flow") is None

    def test_side_by_side_captions_split_into_two_figures(self, tmp_work):
        from paperparse.core.image_extract import extract_figures_caption_driven
        from paperparse.core.skeleton_local import LocalLine, LocalPara, LocalSkeleton

        pdf = tmp_work / "side_by_side.pdf"
        doc = pymupdf.open()
        page = doc.new_page(width=595, height=782)          # 双栏
        for fill, rect in ((150, (44, 100, 285, 300)),       # 左栏图
                           (90, (305, 100, 546, 300))):      # 右栏图（不同像素）
            px = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 120, 120))
            px.clear_with(fill)
            page.insert_image(pymupdf.Rect(*rect), pixmap=px)
        page.insert_text((44, 320), "Figure 4 | Left panel caption.", fontsize=8)
        page.insert_text((305, 318), "Figure 5 | Right panel caption.", fontsize=8)
        doc.save(str(pdf))
        doc.close()

        def ln(i, bbox, text, column):
            return LocalLine(line_id="L%03d" % i, page=1, bbox=bbox, text=text,
                             kind="caption", column=column, order=i)

        # 骨架把这些并排图注并成**一个** caption 段（行文本同实测）
        merged = LocalPara(para_id="P1", kind="caption", text="",
                           lines=[ln(1, (303.6, 318.0, 548.8, 326.0),
                                    "Figure 5 | Right panel caption.", 1),
                                  ln(2, (43.6, 320.0, 285.3, 328.0),
                                    "Figure 4 | Left panel caption.", 0)])
        skel = LocalSkeleton(lines=list(merged.lines), paragraphs=[merged])

        diag = {}
        figs = extract_figures_caption_driven(str(pdf), skel, tmp_work / "imgs",
                                              dpi=72, diag=diag)
        assert sorted(r["num"] for r in diag["anchors"]) == [4, 5], diag["anchors"]
        assert len(figs) == 2, [(f.fig_id, f.caption, f.bbox) for f in figs]
        by_num = {int(f.caption.split()[1].rstrip("|")): f.bbox for f in figs}
        # 两栏各自的裁剪框不得越过分栏缝中点（图注不跨栏 ⇒ 两张小图）
        assert by_num[4][2] <= 297.0 and by_num[5][0] >= 297.0
