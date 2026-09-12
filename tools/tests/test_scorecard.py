# -*- coding: utf-8 -*-
"""scorecard 工具单测（P-ENHANCE R01）"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.scorecard import (  # noqa: E402
    count_garbled,
    detect_spacing,
    heading_seq,
    math_segments,
    score,
    strip_frontmatter,
)

GOLD_MD = """---
title: "Test Paper on Graphene"
authors: ["Alice", "Bob"]
doi: "10.1000/test.123"
---

# Test Paper on Graphene

## Abstract

Graphene is a material with formula $C_{60}$ and conductivity of $10^5 S/m$.

## Results

The measured value is $x = 5$ nm.

## Conclusion

Done.
"""


def _bad_md() -> str:
    t = GOLD_MD.replace("$C_{60}$", "$C_{6}0}$")          # 公式改错
    t = t.replace("## Conclusion", "## Conclusio")         # 标题改错
    t = t.replace("measured value is", "measured value ofconductivity is")
    t = t.replace("Graphene is a material", "Graphene is a material \ufffd")
    t = t.replace('title: "Test Paper on Graphene"', 'title: "Other Title"')
    t = t.replace("$x = 5$ nm", "$x = 5$ nm and $y=1$")    # 公式增
    return t


def test_strip_frontmatter():
    body, fm = strip_frontmatter(GOLD_MD)
    assert fm["title"] == "Test Paper on Graphene"
    assert fm["doi"] == "10.1000/test.123"
    assert not body.startswith("---")


def test_math_segments():
    segs = math_segments(GOLD_MD)
    assert segs["C_{60}"] == 1
    assert segs["x=5"] == 1


def test_heading_seq():
    heads = heading_seq(GOLD_MD)
    assert heads == ["Test Paper on Graphene", "Abstract", "Results", "Conclusion"]


def test_count_garbled():
    assert count_garbled("ok \ufffd bad") == 1
    assert count_garbled("clean text") == 0


def test_detect_spacing():
    hits = detect_spacing("the conductivity ofconductivity material andthe x")
    assert "ofconductivity" in hits
    assert "material" not in hits


def test_score_diff_detected():
    r = score(GOLD_MD, _bad_md())
    c = r["categories"]
    assert c["garbled"]["u_fffd"] >= 1
    assert c["latex_syntax"]["errors"] >= 1          # $C_{6}0}$ 括号错
    assert c["spacing"]["hits"] >= 1                 # ofconductivity
    assert c["formula"]["mismatch"] >= 1             # 公式差异
    assert c["layout"]["diff_ops"] >= 1              # 标题改错
    assert c["metadata"]["title_match"] is False     # 标题改错


def test_score_gold_identical_zero_diff():
    r = score(GOLD_MD, GOLD_MD)
    c = r["categories"]
    assert c["formula"]["mismatch"] == 0
    assert c["layout"]["diff_ops"] == 0
    assert c["metadata"]["title_match"] is True


def test_score_without_gold_abs_only():
    r = score(None, GOLD_MD)
    c = r["categories"]
    assert c["formula"]["mismatch"] == 0
    assert c["layout"]["diff_ops"] == 0
    assert c["metadata"]["title_match"] is None
    assert "abs_errors" in r["score"]


def test_no_false_positive_on_clean():
    r = score(None, GOLD_MD)
    assert r["categories"]["garbled"]["u_fffd"] == 0
    assert r["categories"]["latex_syntax"]["errors"] == 0
    assert r["categories"]["spacing"]["hits"] == 0


# ---------- R10 结构指标 ----------

def test_structure_metrics():
    from tools.scorecard import detect_structure
    ok_md = ("---\ntitle: t\n---\n\n# T\n\n## Abstract\n\nabs text\n\n"
             "## 1. Introduction\n\nbody\n\n![](images/F001.png)\n")
    st = detect_structure(ok_md)
    assert st["images"] == 1
    assert st["abstract_position_ok"] is True
    assert st["publisher_noise"] == 0
    assert st["suspicious_headings"] == []


def test_structure_detects_problems():
    from tools.scorecard import detect_structure
    bad_md = ("---\ntitle: t\n---\n\n# T\n\n## 1. Introduction\n\n"
              "## A B S T R A C T\n\n"          # ABSTRACT 在 Introduction 后
              "Contents lists available at ScienceDirect\n\n"  # 出版信息残留
              "## increase of immersion time.\n")              # 正文句当标题
    st = detect_structure(bad_md)
    assert st["abstract_position_ok"] is False
    assert st["publisher_noise"] >= 1
    assert any("increase of immersion" in h for h in st["suspicious_headings"])


def test_structure_counts_into_abs():
    bad_md = ("---\ntitle: t\n---\n\n# T\n\n## 1. Introduction\n\n"
              "## A B S T R A C T\n\nContents lists available at ScienceDirect\n")
    r = score(None, bad_md)
    assert r["score"]["abs_errors"] >= 2      # abstract 位置 + 出版信息
