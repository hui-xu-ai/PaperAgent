# -*- coding: utf-8 -*-
"""Q5 拼接/配对修复 + Q1 格式统一 单测

覆盖：
  1. _head_anchor_ok：块首有序锚（合法块通过、无关块拒绝、短块不校验）
  2. verify_pair misaligned：段首对齐守卫（无关段落 → misaligned；同首段 → 非 misaligned）
  3. build_markdown include_references=False：References 区跳过（正文引用保留）
  4. render_clean：干净渲染（无 frontmatter/参考文献，空行规范，图注前插图）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from paperparse.core.para_verify import verify_pair  # noqa: E402
from paperparse.core.md_align import norm_text  # noqa: E402
from paperparse.core.p14_pipeline import (  # noqa: E402
    _head_anchor_ok, build_markdown)
from paperparse.core.markdown_render import render_clean  # noqa: E402


# ---------------------------------------------------------------- 配对锚
class TestHeadAnchor:
    def test_valid_block(self):
        # 块首序列在段内存在 → 通过（双方都经 norm_text 归一）
        seg = norm_text("In addition to the immersion temperature and time the type").split()
        assert _head_anchor_ok("In addition to the immersion temperature", seg)

    def test_unrelated_block_rejected(self):
        # "In terms..." 块首在 "In addition..." 段内不存在 → 拒绝（Q5 实证案例）
        seg = norm_text("In addition to the immersion temperature and time the type of "
                        "il may also affect the performance").split()
        assert not _head_anchor_ok("In terms of the electrochemical performance", seg)

    def test_short_block_ok(self):
        # 短块（<3 词）不校验（标题/图注碎片）
        assert _head_anchor_ok("Fig 1", "some unrelated paragraph".split())

    def test_tolerance(self):
        # 容错：块首 1 词 OCR 差异仍通过
        seg = norm_text("In addition to the immersion temperature and time").split()
        assert _head_anchor_ok("in additon to the immersion", seg)  # additon 错拼


# ---------------------------------------------------------------- 段首守卫
class TestMisaligned:
    def test_unrelated_misaligned(self):
        pv = verify_pair("The quick brown fox jumps over the lazy dog",
                         "Sodium chloride solution was prepared in a beaker")
        assert pv.verdict == "misaligned"

    def test_same_head_not_misaligned(self):
        pv = verify_pair(
            "The quick brown fox jumps over the lazy dog. Then it runs.",
            "The quick brown fox runs fast then jumps over the dog")
        assert pv.verdict != "misaligned"

    def test_same_para_ok(self):
        pv = verify_pair("Ionic polymer sensors with Nafion membrane",
                         "Ionic polymer sensors with Nafion membrane were made")
        assert pv.verdict != "misaligned"


# ---------------------------------------------------------------- 无参考文献
class TestNoReferences:
    def _items(self):
        from types import SimpleNamespace
        return [
            SimpleNamespace(para_id="p1", text="# Title", kind="heading",
                            md_idx=[0]),
            SimpleNamespace(para_id="p2", text="Body paragraph one.", kind="body",
                            md_idx=[1]),
            SimpleNamespace(para_id="p3", text="## References", kind="heading",
                            md_idx=[2]),
            SimpleNamespace(para_id="p4", text="[1] J. Author, A paper, J. 1 (2020) 1.",
                            kind="body", md_idx=[3]),
            SimpleNamespace(para_id="p5", text="[2] B. Writer, Another, J. 2 (2021) 2.",
                            kind="body", md_idx=[4]),
        ]

    def test_references_excluded(self):
        md = build_markdown(self._items(), include_references=False)
        assert "References" not in md
        assert "J. Author" not in md
        assert "Body paragraph one." in md
        assert "| [1]" not in md

    def test_references_included_default(self):
        md = build_markdown(self._items())          # 默认 True 兼容
        assert "References" in md
        assert "| [1]" in md


# ---------------------------------------------------------------- 干净渲染
class TestRenderClean:
    def test_clean_no_frontmatter_no_refs(self, tmp_path):
        import json
        from paperparse.core.document_builder import load_document
        # 构造最小 document.json
        doc = {
            "metadata": {"title": "T", "authors": [], "doi": "10.1/x", "abstract": "",
                         "keywords": [], "tags": ["文献"], "source": "pdf"},
            "paragraphs": [
                {"para_id": "h1", "order": 0, "text_en": "## Abstract", "is_heading": True,
                 "is_caption": False, "section": "Abstract", "text_zh": None,
                 "source_block_ids": ["md0"]},
                {"para_id": "b1", "order": 1, "text_en": "The body text with $\\mathrm{H}_{2}$O.",
                 "is_heading": False, "is_caption": False, "section": "",
                 "text_zh": None, "source_block_ids": ["md1"]},
                {"para_id": "h2", "order": 2, "text_en": "## References", "is_heading": True,
                 "is_caption": False, "section": "References", "text_zh": None,
                 "source_block_ids": ["md2"]},
                {"para_id": "r1", "order": 3, "text_en": "[1] J. Author, A paper, J. 1 (2020) 1.",
                 "is_heading": False, "is_caption": False, "section": "References",
                 "text_zh": None, "source_block_ids": ["md3"]},
            ],
            "figures": [], "tables": [], "references": [],
        }
        p = tmp_path / "document.json"
        p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        d = load_document(str(p))
        md = render_clean(d)
        assert md.startswith("# T"), "干净版必须输出 H1 标题（2026-08-26 修复）"
        assert "References" not in md
        assert "J. Author" not in md
        assert "The body text" in md
        assert "## Abstract" in md
        assert "\n\n\n" not in md                       # 空行规范
