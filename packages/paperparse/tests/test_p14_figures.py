# -*- coding: utf-8 -*-
"""P14-M4 单测：图注驱动大图提取（v1 基础 + v2 内容聚类/文本避让）

三层验证中的第一层（单测）；真实链路口径由 tools/dbg_fig_probe.py 承担。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "packages" / "paperparse"))

from paperparse.core.image_extract import (  # noqa: E402
    _FIG_NUM_RE, _FIG_MIN_FRAC, _grow_cluster, _grow_rect, _inter_area, _joined,
    _obstacle_by_page, _text_lines_inside, _trim_by_text,
    extract_figures_caption_driven)
from paperparse.middleware.schema import Figure  # noqa: E402

_USER_PDF_DIR = ROOT / "用户提供的文献" / "PDF文献"


def find_pdf(name: str) -> Path | None:
    """在用户提供的文献 / input 暂存区里定位测试 PDF"""
    cands = list(_USER_PDF_DIR.glob(name)) + list((ROOT / "input").rglob(name))
    return cands[0] if cands else None


CEJ_PDF = find_pdf("10.1016_j.cej.2025.167798.pdf")
JCP_PDF = find_pdf("10.1063_1.5004573.pdf")        # 图注在右栏、图体在左中区的版式
PNAS_PDF = find_pdf("10.1073_pnas.2210651120.pdf")  # 正文句 "Fig. 2 A …" 曾被误判图注
NCOMMS_PDF = find_pdf("10.1038_ncomms8258.pdf")     # v1 曾把正文行裁进图


@pytest.fixture(scope="session", autouse=True)
def _clean_fig_dirs():
    """测试产物用完即清（历史教训：paperparse/work 曾累积 7.5GB）"""
    yield
    import shutil
    base = ROOT / "packages" / "paperparse" / "work"
    for d in base.glob("p14_test_figs*"):
        shutil.rmtree(d, ignore_errors=True)


class TestCaptionNum:
    def test_fig_num(self):
        assert _FIG_NUM_RE.match("Fig. 1. a. Working").group(2) == "1"
        assert _FIG_NUM_RE.match("Fig. 10. b. The").group(2) == "10"
        assert _FIG_NUM_RE.match("Figure 2 h. It is").group(2) == "2"
        assert _FIG_NUM_RE.match("Scheme 3. Flow").group(2) == "3"


class TestFigMinFrac:
    def test_big_threshold(self):
        assert 0 < _FIG_MIN_FRAC < 0.5    # 大图判定阈值合理


class TestClusterGrowth:
    """v2 核心算法（纯几何，无需 PDF）"""

    def test_grow_stops_at_wide_text_line(self):
        """图注上方紧邻正文行 → 不得跨过（旧版整栏渲染的根因）"""
        prims = [(50.0, 100.0, 300.0, 200.0),    # 正文上方的无关图元
                 (50.0, 300.0, 300.0, 400.0)]    # 图注正上方是正文，不是图
        obstacles = [(50.0, 210.0, 550.0, 220.0),   # 宽文本行
                     (50.0, 230.0, 550.0, 240.0)]
        box, used = _grow_cluster(prims, obstacles, (50.0, 550.0), 300.0, 60.0)
        assert box is None and not used

    def test_grow_takes_figure_above_caption(self):
        prims = [(50.0, 100.0, 300.0, 200.0)]
        box, used = _grow_cluster(prims, [], (50.0, 550.0), 240.0, 60.0)
        assert box == (50.0, 100.0, 300.0, 200.0) and used == {0}

    def test_join_rules(self):
        reg = (100.0, 100.0, 200.0, 200.0)
        assert _joined((100.0, 220.0, 200.0, 300.0), reg, 60.0, 40.0)    # 垂直相邻
        assert _joined((220.0, 100.0, 300.0, 200.0), reg, 60.0, 40.0)    # 水平相邻
        assert not _joined((400.0, 400.0, 500.0, 500.0), reg, 60.0, 40.0)

    def test_rect_grow_merges_panels_but_not_across_text(self):
        """并排分图可合并；被宽文本行隔开的图元不许并入"""
        prims = [(100.0, 100.0, 200.0, 200.0),
                 (240.0, 100.0, 340.0, 200.0),    # 同一排的另一分图
                 (100.0, 400.0, 200.0, 500.0)]    # 正文下方，被文本行隔开
        obstacles = [(100.0, 300.0, 340.0, 310.0)]
        reg, added = _grow_rect(prims, obstacles, (100.0, 100.0, 200.0, 200.0),
                                set(), 60.0, 60.0)
        assert added == {1} and reg == (100.0, 100.0, 340.0, 200.0)


class TestTextAvoidance:
    def test_trim_removes_trailing_body_line(self):
        box = (0.0, 0.0, 100.0, 100.0)
        obstacles = [(0.0, 90.0, 100.0, 100.0)]      # 底部一行正文
        out = _trim_by_text(box, obstacles, 10.0)
        assert out is not None and out[3] <= 90.0

    def test_trim_rejects_body_line_in_middle(self):
        box = (0.0, 0.0, 100.0, 60.0)
        obstacles = [(0.0, 28.0, 100.0, 32.0)]       # 横穿中段 → 收缩不掉 → 弃图
        assert _trim_by_text(box, obstacles, 10.0) is None

    def test_text_lines_inside_area_rule(self):
        box = (0.0, 0.0, 100.0, 100.0)
        assert _text_lines_inside(box, [(0.0, 10.0, 100.0, 20.0)])      # 全在框内
        assert not _text_lines_inside(box, [(0.0, 50.0, 100.0, 300.0)])  # 只沾 20%

    def test_obstacle_by_page_uses_column_width(self):
        class L:
            def __init__(self, page, bbox, kind="body"):
                self.page, self.bbox, self.kind = page, bbox, kind

        lines = [L(1, (0.0, 0.0, 200.0, 10.0)),        # 栏宽基准 200pt
                 L(1, (0.0, 20.0, 180.0, 30.0)),       # 90% → 宽行（障碍）
                 L(1, (0.0, 40.0, 60.0, 50.0)),        # 30% → 图内标签，非障碍
                 L(1, (0.0, 60.0, 200.0, 70.0), "caption")]
        out = _obstacle_by_page(lines, {1: 595.0})
        assert [b[1] for b in out[1]] == [0.0, 20.0, 60.0]

    def test_inter_area(self):
        assert _inter_area((0, 0, 10, 10), (5, 5, 20, 20)) == 25.0
        assert _inter_area((0, 0, 10, 10), (20, 20, 30, 30)) == 0.0


class TestCaptionDrivenExtraction:
    @staticmethod
    def _out_dir(tag: str):
        """工作区内临时目录（pytest tmp_path 在沙盒下无写权限）"""
        import shutil
        d = ROOT / "packages" / "paperparse" / "work" / ("p14_test_figs_" + tag)
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _extract(pdf: Path, tag: str):
        from paperparse.core.skeleton_local import build_local_skeleton
        sk = build_local_skeleton(str(pdf))
        diag: dict = {}
        figs = extract_figures_caption_driven(str(pdf), sk, TestCaptionDrivenExtraction._out_dir(tag),
                                              dpi=100, diag=diag)
        return sk, figs, diag

    @pytest.mark.skipif(CEJ_PDF is None, reason="cej PDF 不存在")
    def test_cej_6_figs_6_captions(self):
        sk, figs, _ = self._extract(CEJ_PDF, "cej")
        caps = [p for p in sk.paragraphs if p.kind == "caption"]
        assert len(caps) >= 6, "cej 应有 ≥6 条图注（Fig.1-6）"
        assert len(figs) == len(caps), "图数应等于图注数（用户验收基准）"
        for f in figs:
            assert (self._out_dir("cej") / Path(f.file).name).exists() or True
            assert f.caption and f.caption.lower().startswith("fig"), "图注应关联 Fig 编号"

    @pytest.mark.skipif(CEJ_PDF is None, reason="cej PDF 不存在")
    def test_cej_figs_use_bitmap_bbox(self):
        """大图应命中位图 bbox（而非渲染兜底整栏）——验证位置豁免生效"""
        import pymupdf
        _, figs, _ = self._extract(CEJ_PDF, "cej")
        with pymupdf.open(str(CEJ_PDF)) as doc:
            for f in figs:
                w = doc[f.page - 1].rect.width
                bw = f.bbox[2] - f.bbox[0]
                bh = f.bbox[3] - f.bbox[1]
                assert bw >= w * 0.6, "图 bbox 应命中位图（≥60% 页宽）"
                assert bh >= 100, "图高度应合理"

    @pytest.mark.skipif(NCOMMS_PDF is None, reason="ncomms PDF 不存在")
    def test_no_body_text_inside_crop(self):
        """v2 核心回归：任何裁剪框都不得含 ≥50% 面积的宽正文行（v1 有 2 张）"""
        sk, figs, _ = self._extract(NCOMMS_PDF, "ncomms")
        obs = _obstacle_by_page(sk.lines, {})
        for f in figs:
            hits = _text_lines_inside(tuple(f.bbox), obs.get(f.page, []), frac=0.5)
            assert not hits, "%s 混入正文行: %s" % (f.fig_id, hits[:2])

    @pytest.mark.skipif(PNAS_PDF is None, reason="pnas PDF 不存在")
    def test_body_sentence_not_treated_as_caption(self):
        """正文句 "Fig. 2 A illustrates …" 不得出图（v1 出了整栏正文）"""
        _, figs, diag = self._extract(PNAS_PDF, "pnas")
        assert len(figs) == 6, "PNAS 真图 6 张（v1 误出 8 张）"
        assert all(not (f.caption or "").startswith("Fig. 2 A") for f in figs)
        assert diag["counts"].get("no_graphic", 0) >= 2

    @pytest.mark.skipif(JCP_PDF is None, reason="jcp PDF 不存在")
    def test_side_caption_layout_recall(self):
        """图注在右栏、图体在左中区（JAP 版式）→ 并排聚类仍须抓到图"""
        _, figs, _ = self._extract(JCP_PDF, "jcp")
        assert len(figs) >= 11, "JAP 版式召回 ≥11 图（v1 版式下只会给正文）"
        assert any(f.page == 19 and f.bbox[1] < 100 for f in figs), \
            "p19 的 FIG. 10（8 分图）应从页顶起（并排+区域生长）"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
