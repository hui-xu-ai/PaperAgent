# -*- coding: utf-8 -*-
"""★2026-09-17（NC 真机暴露）：`_looks_like_num_heading` —— 把"数字开头"与"真章节标题"分开。

背景：`_NUM_HEAD_RE = ^\\d+(\\.\\d+)*\\.?\\s+[A-Z]` 在 Nature 系双栏排版里命中满地——
NC 篇 160 个本地段里 68 个被判成 heading（adma 只有 9），骨架/区域崩坏、
md↔骨架对齐 13%（adma/snb 70%）、第三信号票数 7/21。修后 NC heading 68→8、对齐 13%→45%。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from paperparse.core.skeleton_local import _looks_like_num_heading   # noqa: E402


class TestNumHeadingPredicate:
    def test_real_section_headings(self):
        for t in ("1. Introduction",
                  "2. Results and Discussion",
                  "2.1. Electron/Ion Transport Mechanism and Structure Design",
                  "3. Conclusion",
                  "4.1.2. Sub-subsection title",
                  "3. V-shaped actuator design"):     # 单位词在标题里也不能误杀
            assert _looks_like_num_heading(t) is True, t

    def test_reference_entries_are_not_headings(self):
        """NC 实测：参考文献条目 `53. Liu, J. H., …` 曾被判成 56 个 heading"""
        for t in ("53. Liu, J. H., Wang, Z. C., Liu, L. W. & Chen, W. Reduced graphene oxide",
                  "55. Temmer, R. et al. In search of better electroactive polymer actuator",
                  "1. Wu, G. et al. Nature Commun. 6, 7258 (2015)."):
            assert _looks_like_num_heading(t) is False, t

    def test_measurement_lines_are_not_headings(self):
        """NC/adma 实测：数值+单位起头的正文行"""
        for t in ("3 V (up to 0.93 ± 0.03%, three times higher than graphene",
                  "140.8 F g 1 .",
                  "0.1 Hz. (b) Time-dependent displacement of g-CN 800 C and RGO actuators",
                  "2.0 Hz (Figure 6b ; Movie S2 , Supporting Information). The plat-",
                  "5 µm thick film was prepared"):
            assert _looks_like_num_heading(t) is False, t

    def test_length_and_punctuation_guards(self):
        assert _looks_like_num_heading("1. " + "A" * 120) is False       # 过长
        assert _looks_like_num_heading("1. Introduction,") is False      # 句读结尾
        assert _looks_like_num_heading("introduction") is False          # 无编号（走别的判据）
        assert _looks_like_num_heading("") is False
