#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: tests/test_para_align.py
功能: T-A 段落级对齐模块测试：合成 fixture（错位/缺段/多段/标题分割/字符差异）+
      真实双文件统计断言（只断言统计值，不全文读入样本，遵守 D9 纪律）
对外接口: fixtures: tmp_work（conftest 提供）
版本: v1.0.0 (2026-08-18)
版本历史:
  v1.0.0 初始版本（T-A）
"""
import json

import pytest

from paperparse.core import para_align as pa
from paperparse.core import para_align_report as par


# ---------- 合成 fixture ----------

AUTO_MD = """# Title of the Paper

## 1. Introduction

This is the first paragraph of the introduction. It talks about motivation.

## 2. Methods

We used a special method for testing. The method is novel and fast.

Extra paragraph that should not be here.

## 3. Results

The results are good. We show them in Figure 1.

## 2.1. Subsection Heading of

Methods Detail

## 4. Conclusion

In conclusion we did a great job.
"""

CALIB_MD = """# Title of the Paper

## 1. Introduction

This is the first paragraph of the introduction. It talks about motivation.

## 2. Methods

We used a special method for testing. The method is novel and fast.

## 2.1. Subsection Heading of Methods Detail

## 3. Results

The results are good. We show them in Figure 1.

## 4. Conclusion

In conclusion we did a great job.
"""


@pytest.fixture
def synth_files(tmp_work):
    a = tmp_work / "auto.md"
    c = tmp_work / "calib.md"
    a.write_text(AUTO_MD, encoding="utf-8")
    c.write_text(CALIB_MD, encoding="utf-8")
    return a, c


# ---------- 解析 ----------

def test_parse_md_paras_basic(synth_files):
    a, c = synth_files
    auto = pa.parse_md_paras(a)
    calib = pa.parse_md_paras(c)
    assert auto.title == "Title of the Paper"
    # auto：多出 1 段正文 + 标题被拆成 2 段 → 段数比校准多 2
    assert len(auto.items) == len(calib.items) + 2
    assert not auto.truncated
    # 标题分割：auto 中 "Methods Detail" 是独立段落（标题续行未并入）
    texts = [x.text for x in auto.items]
    assert "Methods Detail" in texts
    assert "2.1. Subsection Heading of" in texts


def test_parse_md_paras_skips_frontmatter(tmp_work):
    """frontmatter / callout / 图片行不进入段落"""
    md = tmp_work / "fm.md"
    md.write_text(
        "---\ntitle: X\ntags: [a]\n---\n\n> [!info] callout\n\n"
        "![](images/F001.png)\n\n## S\n\nbody text\n", encoding="utf-8")
    doc = pa.parse_md_paras(md)
    # frontmatter 不解析为标题（title 由 # 行获取），callout/图片行被跳过
    assert doc.title == ""
    assert [x.text for x in doc.items] == ["S", "body text"]


def test_parse_md_paras_missing(tmp_work):
    with pytest.raises(FileNotFoundError):
        pa.parse_md_paras(tmp_work / "nope.md")


# ---------- 对齐 ----------

def test_align_docs_synth(synth_files):
    a, c = synth_files
    rep = pa.align_docs(pa.parse_md_paras(a), pa.parse_md_paras(c))
    # 标题分割 → auto 多 2 段（extra 区域）
    assert rep.stats["region_by_type"]["extra"] >= 1
    # 主体段落全部匹配
    assert rep.stats["matched_count"] >= 6
    assert rep.stats["avg_dice"] > 0.9


def test_align_docs_char_diff(tmp_work):
    """字符级差异：英文与中文句子定位"""
    a = tmp_work / "a.md"
    c = tmp_work / "c.md"
    a.write_text("## S\n\nHello world. This is the original sentence.\n", encoding="utf-8")
    c.write_text("## S\n\nHello world. This is the modified sentence!\n", encoding="utf-8")
    rep = pa.align_docs(pa.parse_md_paras(a), pa.parse_md_paras(c))
    assert rep.stats["matched_count"] == 2       # ## S 标题 + 正文段
    m = rep.matched[1]
    assert m.diffs, "应有字符级差异"
    assert any(d.op in ("replace", "insert", "delete") for d in m.diffs)
    # 句子定位：差异应定位到 "this is the original sentence" 所在句子
    joined = " ".join(d.auto_sent for d in m.diffs)
    assert "original sentence" in joined

    # 中文句子边界
    a.write_text("## S\n\n第一句。第二句有差异。\n", encoding="utf-8")
    c.write_text("## S\n\n第一句。第二句被改写了。\n", encoding="utf-8")
    c.write_text("## S\n\n第一句。第二句被改写了。\n", encoding="utf-8")
    rep2 = pa.align_docs(pa.parse_md_paras(a), pa.parse_md_paras(c))
    sent = " ".join(d.auto_sent for d in rep2.matched[1].diffs)
    assert "第二句" in sent


def test_align_docs_missing_and_extra(tmp_work):
    """缺段（missing）与多段（extra）判定"""
    a = tmp_work / "a.md"
    c = tmp_work / "c.md"
    a.write_text("## S\n\npara one\n\npara two\n", encoding="utf-8")
    c.write_text("## S\n\npara one\n\nmiddle missing\n\npara two\n", encoding="utf-8")
    rep = pa.align_docs(pa.parse_md_paras(a), pa.parse_md_paras(c))
    assert rep.stats["region_by_type"]["missing"] == 1
    assert rep.regions[0].calib_texts[0].startswith("middle")


def test_strip_latex():
    """LaTeX 公式剥离（自动版高精度解析含 $..$ 公式；校准版无 → 匹配前剥离不误报）"""
    t = "The material $Co_2P$ shows high conductivity with $\\alpha = 0.5$."
    s = pa.strip_latex(t)
    assert "$" not in s and "\\alpha" not in s
    assert "Co_2P" not in s
    # $$..$$ 与 \begin..\end 块
    t2 = "Equation: $$E = mc^2$$ and \\begin{equation}x+y\\end{equation} done."
    s2 = pa.strip_latex(t2)
    assert "mc^2" not in s2 and "x+y" not in s2
    assert "Equation" in s2 and "done" in s2


def test_align_docs_latex_tolerance(tmp_work):
    """含 LaTeX 公式的自动版 vs 无公式校准版：应正常匹配（公式不产生差异噪音）"""
    a = tmp_work / "a.md"
    c = tmp_work / "c.md"
    a.write_text("## S\n\nThe device shows $\\Delta d$ = 13.08 mm at $\\pm 0.5$ V.\n",
                 encoding="utf-8")
    c.write_text("## S\n\nThe device shows Δ d = 13.08 mm at ± 0.5 V.\n", encoding="utf-8")
    rep = pa.align_docs(pa.parse_md_paras(a), pa.parse_md_paras(c))
    assert rep.stats["matched_count"] == 2          # 标题 + 正文均匹配
    m = rep.matched[1]
    assert m.dice > 0.8
    assert "\\Delta" not in " ".join(d.auto_sent for d in m.diffs)


# ---------- 缺陷清单 ----------

def test_defect_list_heading_split(synth_files):
    a, c = synth_files
    auto, calib = pa.parse_md_paras(a), pa.parse_md_paras(c)
    rep = pa.align_docs(auto, calib)
    defs = par.defect_list(rep, auto, calib)
    types = {d["type"] for d in defs}
    assert "heading_split" in types, "合成样本应检出标题分割"
    assert all(d["id"] and d["suspect"] for d in defs)


# ---------- 报告落盘 ----------

def test_render_reports(synth_files, tmp_work):
    a, c = synth_files
    rep = pa.align_docs(pa.parse_md_paras(a), pa.parse_md_paras(c))
    jp = par.render_json_report(rep, tmp_work / "rep.json")
    tp = par.render_text_report(rep, tmp_work / "rep.txt")
    data = json.loads(jp.read_text(encoding="utf-8"))
    assert data["stats"]["matched_count"] == rep.stats["matched_count"]
    assert "不匹配区域" in tp.read_text(encoding="utf-8")
    assert "matched" in data and "regions" in data


# ---------- 真实样本（统计断言，不读全文） ----------

REAL_AUTO = "work/上一轮识别结果/paper.md"
REAL_CALIB = "paper_手动校准版.md"


@pytest.mark.skipif(not __import__("pathlib").Path(REAL_AUTO).exists(),
                    reason="真实样本缺失")
def test_real_alignment_stats():
    """上一轮 paper.md vs 手动校准版：主体应高度匹配，但检出已知缺陷"""
    auto = pa.parse_md_paras(REAL_AUTO)
    calib = pa.parse_md_paras(REAL_CALIB)
    assert len(auto.items) > 100 and len(calib.items) > 100
    rep = pa.align_docs(auto, calib)
    st = rep.stats
    assert st["matched_count"] >= 100           # 主体匹配
    assert st["avg_dice"] > 0.9
    assert st["region_count"] >= 10             # 存在不匹配区域（已知缺陷）
    defs = par.defect_list(rep, auto, calib)
    dtypes = {d["type"] for d in defs}
    # 已知缺陷：标题分割、正文误判标题（extra 中 heading）、图注错位（shift）
    assert "heading_split" in dtypes
    assert "extra" in dtypes and "shift" in dtypes
    # 缺陷清单应定位到具体段落（auto_idx 非空）
    assert any(d["auto_idx"] for d in defs if d["type"] in ("heading_split", "shift"))
