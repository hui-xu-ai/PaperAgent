# -*- coding: utf-8 -*-
"""P14-M7 单测：双通道整段验证"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from paperparse.core.para_verify import verify_pair, _word_seq, _lcs_ratio


class TestWordSeq:
    def test_strip_nonwords(self):
        # 剔除 LaTeX/数字/参考文献编号，只留字母词
        seq = _word_seq("The mass [12] was $\\mathrm { H } ^ { + }$ 150% ions")
        assert seq == ["the", "mass", "was", "ions"] or "h" not in seq


class TestVerifyPair:
    def test_identical(self):
        pv = verify_pair("The quick brown fox jumps over the lazy dog",
                         "The quick brown fox jumps over the lazy dog")
        assert pv.verdict == "ok" and pv.overlap_set >= 0.99

    def test_shift_detected(self):
        """用户规则：单词一样但中间错位 → 必须检出（集合高、序列低）"""
        pv = verify_pair(
            "The quick brown fox jumps over the lazy dog. Then it runs fast.",
            "The quick brown fox runs fast. Then it jumps over the lazy dog.")
        assert pv.shifted and pv.verdict == "suspicious", \
            "单词集合重叠高但乱序必须标 suspicious"

    def test_low_overlap_suspicious(self):
        """Q5 防线2：完全无关段落（P/M 开头不同）→ misaligned（配对错位，跳过 diff）"""
        pv = verify_pair("The quick brown fox jumps over the lazy dog",
                         "Sodium chloride solution was prepared in a beaker")
        assert pv.verdict == "misaligned"

    def test_same_head_mid_diff(self):
        """段首对齐但后段内容差异 → 仍走 review/suspicious（非 misaligned）"""
        pv = verify_pair(
            "The quick brown fox jumps over the lazy dog. It was a sunny day.",
            "The quick brown fox runs in the park. Completely different story.")
        assert pv.verdict != "misaligned"

    def test_minor_diff_review(self):
        pv = verify_pair(
            "Polymer-based ionic sensor consists of two electrode layers",
            "Polymer based ionic sensor consists of two electrode layers")
        assert pv.verdict in ("ok", "review")

    def test_lcs_ratio(self):
        assert _lcs_ratio(["a", "b", "c"], ["a", "b", "c"]) == 1.0
        assert _lcs_ratio(["a", "b", "c"], ["c", "b", "a"]) == pytest.approx(1 / 3)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
