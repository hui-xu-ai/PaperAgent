#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: tests/test_mineru_md_clean.py
功能: MinerU 高精度 MD 清理模块测试：
      - LaTeX/<sup> 格式原样保留（拼接不丢格式）
      - 小图引用（哈希命名）删除、本地大图引用保留
      - 子图标注行（(a)/a) 等单独段落）删除
      - 大图题注（Figure N.）保留且图插在题注前
      - 摘要无标题补插 "## Abstract"
对外接口: 无（测试）
版本: v1.0.0 (2026-08-18)
版本历史:
  v1.0.0 初始版本
"""
import re

import pytest

from paperparse.core.mineru_md_clean import clean_mineru_md

FIXTURE = """# Test Paper Title

Zhenjin Xu, Keqi Deng

Eficient ion transport $\\mathsf { s p } ^ { 2 }$ with conductivity (≈20 000 S cm<sup>−1</sup>).

Multi-modal actuation strategies have been adopted.

## 1. Introduction

Artificial muscles with $I _ { \\mathrm { D 1 } }$ values.

![](images/7740bd91ff75b53ec5dde53c697f5d745e202a4511e556e0f5307046bc9a5156.jpg)

(a)

(b) Dual-responsive actuation

![](images/F001.png)

Figure 1. Schematic illustrations of ${ \\mathsf { C o } }$ structure. a) Coral-like structure. b) Transport.

## 2. Methods

Body text with $\\alpha$ formula.
"""


def test_clean_keeps_latex_and_sup():
    """LaTeX 公式与 <sup> 原样保留（用户问题 1：拼接不丢格式）"""
    out = clean_mineru_md(FIXTURE)
    assert "\\mathsf { s p } ^ { 2 }" in out
    assert "<sup>−1</sup>" in out
    assert "I _ { \\mathrm { D 1 } }" in out
    assert "\\alpha" in out


def test_clean_removes_small_images():
    """小图引用（哈希命名）删除；本地大图引用保留（用户问题 2）"""
    out = clean_mineru_md(FIXTURE, image_map={1: "images/F001.png"})
    assert "7740bd91" not in out
    assert "![](images/F001.png)" in out
    # 无 image_map 时大图引用也应保留（F00x 本地命名不受影响）？——当前规则：仅 image_map 内的保留
    assert "F001.png" in out


def test_clean_removes_subfig_labels():
    """子图标注行（(a) / (b) 描述 / a) 开头段落）删除；图注内联 a) b) 保留（行首为 Figure）"""
    out = clean_mineru_md(FIXTURE)
    assert not re.search(r"^\(a\)\s*$", out, re.M)
    assert "Dual-responsive actuation" not in out          # (b) 描述行删除
    assert "a) Coral-like structure. b) Transport." in out  # 图注内联子图说明保留


def test_clean_inserts_abstract_heading():
    """摘要无标题 → 文档 H1 后、首个 ## 前补插 "## Abstract"（渲染净化一致性）"""
    out = clean_mineru_md(FIXTURE)
    lines = out.splitlines()
    h1 = next(i for i, ln in enumerate(lines) if ln.startswith("# "))
    assert "## Abstract" in out
    ai = lines.index("## Abstract")
    assert ai > h1                                   # Abstract 在文档 H1 之后
    assert "Eficient ion transport" in out           # 摘要段保留
    assert "## 1. Introduction" in out               # 章节标题仍在


def test_clean_caption_before_figure():
    """图插在题注之前（T-C 规则）；题注本身保留"""
    out = clean_mineru_md(FIXTURE, image_map={1: "images/F001.png"})
    img = out.find("![](images/F001.png)")
    cap = out.find("Figure 1. Schematic")
    assert img != -1 and cap != -1 and img < cap


def test_clean_removes_wrapped_image_refs():
    """[![](...)] 包裹形式的小图引用也要删除"""
    t = FIXTURE + "\n[![](images/67b178ea5882e20b1bcb349d277380e7858a91511933320e09e801c237c970f1.jpg)]\n"
    out = clean_mineru_md(t)
    assert "67b178ea" not in out


# ---------- 真实样本（统计断言，不读全文） ----------

REAL_FULL = "work/mineru_backup/20260818-191542_v4batch/full.md"
REAL_IMG = "work/拼接修复版_v9/10.1002_adma.202407106/images"


@pytest.mark.skipif(not __import__("pathlib").Path(REAL_FULL).exists(),
                    reason="真实 MinerU 输出缺失")
def test_real_mineru_full_clean():
    """真实 MinerU 高精度 full.md：41 小图引用清空、7 大图题注保留、LaTeX 保留"""
    from paperparse.core.mineru_md_clean import convert_mineru_md_file
    import tempfile
    out = convert_mineru_md_file(REAL_FULL, REAL_IMG, "work/pytest-tmp2/real_clean.md")
    t = out.read_text(encoding="utf-8")
    refs = re.findall(r"!\[\]\([^)]*\)", t)
    assert all("F00" in r for r in refs), "不应残留哈希小图引用"
    assert len(refs) == 7
    caps = re.findall(r"^Figure \d\.", t, re.M)
    assert len(caps) == 7
    assert t.count("$") > 300          # LaTeX 大量保留
    assert "## Abstract" in t          # 摘要标题补插


