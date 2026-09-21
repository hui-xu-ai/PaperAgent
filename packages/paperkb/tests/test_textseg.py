# -*- coding: utf-8 -*-
"""textseg 原语单测：边界对齐截断 / 命中窗口 / 标题感知分块（2026-09-21 RAG 审计）。"""
from __future__ import annotations

from paperkb.textseg import (
    boundary_trim,
    embed_prefix,
    split_chunks,
    strip_frontmatter,
    window_around,
)


class TestStripFrontmatter:
    def test_strips_block(self):
        text = "---\ntype: paper-note\ndoi: 10.1/x\ntags: [paper, a]\n---\n# 标题\n正文"
        assert strip_frontmatter(text) == "# 标题\n正文"

    def test_no_frontmatter_untouched(self):
        text = "# 标题\n正文"
        assert strip_frontmatter(text) == text

    def test_unclosed_not_stripped(self):
        text = "---\ntype: x\n正文（无闭合）"
        assert strip_frontmatter(text) == text

    def test_empty(self):
        assert strip_frontmatter("") == ""


class TestBoundaryTrim:
    def test_short_text_unchanged(self):
        assert boundary_trim("abcdef", 10) == "abcdef"

    def test_no_boundary_falls_back_to_hard_cut(self):
        # 与 kb_tools._trim 的历史行为一致（无边界 → 硬切）
        assert boundary_trim("x" * 900, 800) == "x" * 800

    def test_never_exceeds_limit(self):
        text = ("段落一" * 30) + "\n\n" + ("段落二" * 30)
        for limit in (10, 50, 77, 120, 200):
            assert len(boundary_trim(text, limit)) <= limit

    def test_prefers_paragraph_boundary(self):
        para1 = "甲" * 300
        para2 = "乙" * 500
        text = f"{para1}\n\n{para2}"
        out = boundary_trim(text, 400)
        assert out == para1, "应在段落边界收尾，而不是切进第二段"

    def test_avoids_splitting_formula(self):
        # 后半段含 $…$：若在段落边界切会留下奇数个 $，应退到更靠前/更靠后的安全边界
        text = "开头" * 100 + "\n\n" + "公式 $E=mc^2$ 结束。" + "尾巴" * 100
        out = boundary_trim(text, 260)
        assert out.count("$") % 2 == 0, f"截断落在公式内: {out[-40:]!r}"
        assert len(out) <= 260

    def test_zero_limit(self):
        assert boundary_trim("abc", 0) == ""


class TestWindowAround:
    def test_returns_region_not_head(self):
        text = "头" * 400 + "命中关键词" + "尾" * 400
        out = window_around(text, 402)
        assert "命中关键词" in out
        assert "头" * 400 not in out, "不应返回整段文件开头"
        assert out != text[:len(out)]

    def test_limit_respected(self):
        text = "字" * 2000
        assert len(window_around(text, 1000, radius=300, limit=500)) <= 500

    def test_unlocated_falls_back_to_head(self):
        text = "开头一段\n\n第二段" + "字" * 100
        out = window_around(text, -1, limit=20)
        assert out and len(out) <= 20


class TestSplitChunks:
    BODY = (
        "---\ntype: paper-note\ndoi: 10.1/x\n---\n"
        "# 概念关系分析：样例\n\n"
        "## 研究簇概述\n"
        "第一句。第二句。第三句。\n\n"
        "## 概念关系图\n\n"
        "### 电渗驱动机制\n"
        "文献3提出机制。本文将其转化为设计原理。" * 6 + "\n\n"
        "### 多孔电极\n"
        "文献2建立孔模型。本文改用金属纳米膜。" * 6 + "\n\n"
        "## 相关文献\n\n"
        "1. [[10.1_a/_note]]\n2. [[10.1_b/_note]]\n"
    )

    def test_offsets_slice_back(self):
        body = strip_frontmatter(self.BODY)
        for c in split_chunks(body):
            assert c["text"] == body[c["start"]:c["end"]].strip()

    def test_section_content_covered(self):
        body = strip_frontmatter(self.BODY)
        text = "\n".join(c["text"] for c in split_chunks(body))
        for heading in ("## 研究簇概述", "## 概念关系图", "### 电渗驱动机制",
                        "### 多孔电极", "## 相关文献", "1. [[10.1_a/_note]]"):
            assert heading in text, f"分块后丢失: {heading}"

    def test_chunk_section_labelled(self):
        body = strip_frontmatter(self.BODY)
        sections = [c["section"] for c in split_chunks(body)]
        assert sections and all(s for s in sections)
        assert set(sections) <= {"", "研究簇概述", "电渗驱动机制", "多孔电极", "相关文献"}
        assert "研究簇概述" in sections

    def test_short_sections_packed_together(self):
        """短小节应被打包到同一块（避免 _note 卡片被切成八九个碎片）。"""
        body = "# T\n" + "".join(f"## 第{i}节\n一句话内容{i}。\n\n" for i in range(6))
        chunks = split_chunks(body, max_chars=1200)
        assert len(chunks) == 1, [c["text"][:20] for c in chunks]

    def test_no_chunk_exceeds_limit(self):
        body = strip_frontmatter(self.BODY) + ("超长段落。" * 400)
        # 允许"过小尾块并回同小节前块"带来的 ≤4/3 超限（内容优先于整齐度）
        for c in split_chunks(body, max_chars=300):
            assert len(c["text"]) <= 400

    def test_long_paragraph_split_at_sentence(self):
        body = "## 一\n" + "这是完整的一句话。" * 200
        chunks = split_chunks(body, max_chars=300)
        assert len(chunks) > 1
        assert all(c["text"].endswith("。") for c in chunks[:-1]), \
            [c["text"][-12:] for c in chunks[:-1]]

    def test_overlap_between_same_section_chunks(self):
        body = "## 一\n" + "甲。" * 300
        chunks = split_chunks(body, max_chars=200, overlap_chars=60)
        assert len(chunks) > 1
        assert chunks[1]["start"] < chunks[0]["end"], "同小节相邻块应有重叠"

    def test_overlap_is_bounded_and_from_prev_tail(self):
        body = "## 一\n" + "甲。" * 300
        chunks = split_chunks(body, max_chars=200, overlap_chars=60)
        assert len(chunks) > 1
        cur, prev = chunks[1], chunks[0]
        assert cur["start"] < prev["end"], "相邻块应有重叠"
        assert prev["end"] - cur["start"] <= 60, "重叠长度不得超过 overlap_chars"
        overlapped = body[cur["start"]:prev["end"]]
        assert overlapped.strip() and prev["text"].endswith(overlapped.strip())

    def test_deterministic(self):
        body = strip_frontmatter(self.BODY)
        assert split_chunks(body) == split_chunks(body)

    def test_empty_body(self):
        assert split_chunks("") == []
        assert split_chunks("   \n  ") == []

    def test_orphan_heading_dropped(self):
        body = "## 空小节\n\n## 有内容\n" + "正文内容。" * 40
        chunks = split_chunks(body)
        assert all(c["text"] != "## 空小节" for c in chunks)


class TestEmbedPrefix:
    def test_has_all_parts(self):
        assert embed_prefix("标题", "_wiki", "小节") == "[标题 | _wiki | 小节]\n"

    def test_skips_empty(self):
        assert embed_prefix("", "_note", "") == "[_note]\n"
        assert embed_prefix("", "", "") == ""
