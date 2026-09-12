# -*- coding: utf-8 -*-
"""P14 阶段1 单测：skeleton_local M1/M2/M3 核心规则"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from paperparse.core.skeleton_local import (
    build_local_skeleton, _ends_sentence, _strip_refs, _words,
    extract_lines, _classify_line, _reclassify_caption_continuations,
)


class TestSentenceCompleteness:
    """引用编号剥离后再判终止符（用户规则：不能只查最后字符）"""

    def test_ref_after_period(self):
        assert _ends_sentence("…quality. [1]")
        assert _ends_sentence("…Information). [61]")
        assert _ends_sentence("…(Figure 1b).")

    def test_ref_tight_no_space(self):
        assert _ends_sentence("…sensing performance. [22]")
        assert _ends_sentence("…framework. [13]")
        assert _ends_sentence("…electrode layers. [12]")

    def test_incomplete_sentence(self):
        assert not _ends_sentence("…consists of an ionic polymer matrix sandwiched by two electrode layers")
        assert not _ends_sentence("…the water will evaporate (Fig. 1a)")
        assert not _ends_sentence("…corresponding to the stretching")


class TestWords:
    def test_words_count(self):
        assert _words("Ionic polymer sensor Ionic liquid Heating") == 6
        # 公式行：数字/π/Δ 不计数，字母变量 E/mf/l 计 3
        assert _words("E = 3 . 87 π 2 mf 2 l 3") == 3
        # 引用编号 [12] 不计数，正文 4 词
        assert _words("[ 12 ] The ionic polymer matrix") == 4


class TestClassify:
    def test_caption_vs_subfig_ref(self):
        # 图注：编号后标点/大写
        assert _classify_line(_ln("Fig. 1. a. Working principle."), 10.0) == "caption"
        # 子图引用（正文）：编号后小写 → 非 caption
        assert _classify_line(_ln("Fig. 2 h. It is worth noting that"), 10.0) != "caption"
        assert _classify_line(_ln("shown in Fig. 1a"), 10.0) != "caption"

    def test_meta_lines(self):
        assert _classify_line(_ln("* Corresponding author."), 10.0) == "meta"
        assert _classify_line(_ln("E-mail address: yjwang@hhu.edu.cn (Y. Wang)."), 10.0) == "meta"
        assert _classify_line(_ln("https://doi.org/10.1016/j.cej.2025.167798"), 10.0) == "meta"

    def test_heading(self):
        assert _classify_line(_ln("1. Introduction"), 10.0) == "heading"
        assert _classify_line(_ln("A B S T R A C T"), 10.0) == "heading"
        assert _classify_line(_ln("Declaration of competing interest"), 10.0) == "heading"

    def test_list_item(self):
        assert _classify_line(_ln("(1) Pretreatment. First, Nafion membrane"), 10.0) == "list"
        assert _classify_line(_ln("(2)"), 10.0) != "list"   # 公式编号不判列表

    def test_big_font_body_not_heading(self):
        # 正文中字号异常行（10.84 vs median 7.97）小写开头 → 非标题
        ln = _ln("rated by the hydrophobic C – F backbone. [ 3", page=5, y=648.2)
        ln.font_size = 10.84
        assert _classify_line(ln, 7.97) == "body"

    def test_zip_country_meta(self):
        # 机构地址行 "Xiamen 361005, China"（邮编+国家在行中部）→ meta
        # （match 从行首失败 → search；adma 首页 B 段后机构行误拼入正文）
        from paperparse.core.skeleton_local import LocalLine
        ln = LocalLine(line_id="T", page=1, bbox=(50.8, 661.0, 300.0, 673.0),
                       text="Xiamen 361005, China", font_size=9.0)
        assert _classify_line(ln, 9.0) == "meta"


class TestCaptionContinuation:
    """LP019 修复：图注换行残段（无缩进 body）须并入 caption，不混入正文"""

    def test_caption_continuation_reclassified(self):
        from paperparse.core.skeleton_local import LocalLine
        # Fig.2 图注块：caption 首行 + 两行无缩进续行 + 缩进正文段首（y 相邻）
        lines = [
            LocalLine(line_id="T1", page=4, bbox=(37.6, 700.0, 300.0, 712.0),
                      text="Fig. 2. a. The mass change ratio of IPS at different "
                           "immersion temperatures. b. The stiffness of IPS at "
                           "different immersion temperatures. c. The surface "
                           "resistance of", kind="caption", column=0,
                      font_size=7.17),
            LocalLine(line_id="T2", page=4, bbox=(37.6, 712.0, 300.0, 724.0),
                      text="IPS at different immersion temperatures. d. The surface "
                           "electrode morphology of IPS immersed", kind="body",
                      column=0, font_size=7.17),
            LocalLine(line_id="T3", page=4, bbox=(37.6, 724.0, 300.0, 736.0),
                      text="at different temperatures.", kind="body", column=0,
                      font_size=7.17),
            LocalLine(line_id="T4", page=4, bbox=(49.5, 740.0, 300.0, 752.0),
                      text="Then, the effect of IL immersion temperature on the "
                           "electrochemical", kind="body", column=0,
                      font_size=7.97),
        ]
        _reclassify_caption_continuations(lines)
        kinds = {ln.line_id: ln.kind for ln in lines}
        assert kinds["T2"] == "caption", kinds     # 图注续行 → caption
        assert kinds["T3"] == "caption", kinds     # 图注续行 → caption
        assert kinds["T4"] == "body", kinds        # 缩进正文段首保持 body

    def test_caption_continuation_stops_at_far_line(self):
        from paperparse.core.skeleton_local import LocalLine
        lines = [
            LocalLine(line_id="T1", page=4, bbox=(37.6, 700.0, 300.0, 712.0),
                      text="Fig. 1. a. Working principle.", kind="caption",
                      column=0, font_size=7.17),
            LocalLine(line_id="T2", page=4, bbox=(37.6, 800.0, 300.0, 812.0),
                      text="A new paragraph far below the caption.", kind="body",
                      column=0, font_size=7.17),
        ]
        _reclassify_caption_continuations(lines)
        assert lines[1].kind == "body"   # y 间隔 > 15 → 不并

    def test_caption_continuation_stops_at_font_diff(self):
        from paperparse.core.skeleton_local import LocalLine
        lines = [
            LocalLine(line_id="T1", page=4, bbox=(37.6, 700.0, 300.0, 712.0),
                      text="Fig. 3. a. The mass change ratio of IPS with different "
                           "immersion time.", kind="caption", column=0,
                      font_size=7.17),
            LocalLine(line_id="T2", page=4, bbox=(37.6, 710.0, 300.0, 722.0),
                      text="increase of immersion time.", kind="body", column=0,
                      font_size=7.97),
        ]
        _reclassify_caption_continuations(lines)
        assert lines[1].kind == "body"   # 正文字号 vs 图注字号差 0.8pt → 不并


def _ln(text, page=1, x0=37.6, y=100.0):
    from paperparse.core.skeleton_local import LocalLine
    return LocalLine(line_id="T", page=page, bbox=(x0, y, x0 + 200, y + 12),
                     text=text, font_size=9.0)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
