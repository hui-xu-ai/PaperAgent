# -*- coding: utf-8 -*-
"""tools/html_pipeline 冒烟测试（HT5）：HTML→MD + elements.json + PDF 补图。"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HTML = ROOT / "用户提供的文献" / "HTML网页文献" / "10.1016_j.cej.2025.167798.html"
PDF = ROOT / "用户提供的文献" / "PDF文献" / "10.1016_j.cej.2025.167798.pdf"


def test_html_pipeline_e2e(tmp_path):
    if not HTML.exists():
        import pytest
        pytest.skip("无样本 HTML")
    r = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "html_pipeline.py"),
         str(HTML), "--pdf", str(PDF), "--out", str(tmp_path)],
        capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    doi = "10.1016_j.cej.2025.167798"
    out = tmp_path / doi
    assert (out / f"{doi}.md").exists()
    doc = json.loads((out / "elements.json").read_text(encoding="utf-8"))
    assert doc["metadata"]["doi"]  # SD 通道应填 DOI
    assert doc["paragraphs"]
    if PDF.exists():
        imgs = list((out / "images").glob("*.png"))
        assert imgs, "应从 PDF 提取图"
        md = (out / f"{doi}.md").read_text(encoding="utf-8")
        assert "images/fig" in md, "MD 应回填 PDF 图引用"


def test_frontmatter_keywords_in_separate_field(tmp_path):
    """Q3：关键词多词 → 放 keywords 独立字段；tags 只保留合法单 token（Obsidian 无空格）。"""
    if not HTML.exists():
        import pytest
        pytest.skip("无样本 HTML")
    r = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "html_pipeline.py"),
         str(HTML), "--out", str(tmp_path)],
        capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    doi = "10.1016_j.cej.2025.167798"
    md = (tmp_path / doi / f"{doi}.md").read_text(encoding="utf-8")
    fm = json.loads(md.split("---")[1])
    # 含空格的多词关键词进 keywords 字段
    assert "keywords" in fm and any(" " in k for k in fm["keywords"])
    # tags 不含含空格 token（全部合法）
    for t in fm.get("tags", []):
        assert " " not in t, f"tag 含空格不合法: {t}"
    # 年份/期刊字段已加
    assert fm.get("year") and fm.get("journal")
