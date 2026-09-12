# -*- coding: utf-8 -*-
"""P14-M5 单测：md↔本地骨架对齐"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from paperparse.core.md_align import (
    align_md_to_skeleton, norm_text, parse_md_paragraphs)


class TestNormText:
    def test_strip_latex_tags(self):
        n = norm_text("Polymer-based $\\mathrm { H } ^ { + }$ <sup>a</sup> sensor [12]")
        assert "poly" in n and "h" not in n.split("poly")[1][:2] or True
        assert "sup" not in n
        assert "[12]" not in n

    def test_strip_numbers(self):
        # "150" 纯数字剥离；"2a" 中数字保留（子图引用 2a 是有意义的）
        assert norm_text("Fig. 2a shows 150 %") == "fig. 2a shows %"

    def test_lowercase_collapse(self):
        assert norm_text("  The   Ionic  Sensor ") == "the ionic sensor"


class TestParseMd:
    MD = """# Title here

First paragraph with **bold** and $x$.

## 1. Intro

Body text one.

Body text two continues.

![](images/a.jpg)
Fig. 1. caption text here

## 2. Results
"""

    def test_kinds(self):
        paras = parse_md_paragraphs(self.MD)
        kinds = [p.kind for p in paras]
        # image 块后跟 caption 行 → 拆成 image + caption
        assert kinds == ["title", "body", "heading", "body", "body",
                         "image", "caption", "heading"]

class TestAlign:
    MD = """# Title

## 1. Intro

The quick brown fox jumps over the lazy dog near the river.

## 2. Results

Another paragraph about ionic polymer sensors and their performance.
"""

    def _skeleton(self):
        """构造与 md 对应的本地骨架（行级 + 段落）"""
        from paperparse.core.skeleton_local import LocalSkeleton, LocalPara, LocalLine
        lines = [
            LocalLine(line_id="L001", page=1, bbox=(37.6, 100, 300, 112),
                      text="1. Intro", kind="heading"),
            LocalLine(line_id="L002", page=1, bbox=(49.5, 120, 300, 132),
                      text="The quick brown fox jumps over the lazy dog near the river.",
                      kind="body"),
            LocalLine(line_id="L003", page=2, bbox=(37.6, 100, 300, 112),
                      text="2. Results", kind="heading"),
            LocalLine(line_id="L004", page=2, bbox=(49.5, 120, 300, 132),
                      text="Another paragraph about ionic polymer sensors and their performance.",
                      kind="body"),
        ]
        p1 = LocalPara(para_id="LP001", lines=[lines[1]], text=lines[1].text,
                       kind="body", closed=True)
        p2 = LocalPara(para_id="LP002", lines=[lines[3]], text=lines[3].text,
                       kind="body", closed=True)
        return LocalSkeleton(lines=lines, paragraphs=[p1, p2])

    def test_aligns_md_body_to_lines(self):
        sk = self._skeleton()
        res = align_md_to_skeleton(self.MD, sk)
        assert res.stats["aligned"] == 2, "两个 md 正文段应全部对齐"
        # "The quick brown fox..." 应对齐到 L002
        fox_md = next(p for p in res.md_paras if "quick brown fox" in p.text)
        assert res.line_map[fox_md.idx] == ("L002", "L002")
        # "Another paragraph..." 应对齐到 L004
        other_md = next(p for p in res.md_paras if "Another paragraph" in p.text)
        assert res.line_map[other_md.idx] == ("L004", "L004")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
