# -*- coding: utf-8 -*-
"""RAG 边界修复单测：向量一路接进召回 / 命中窗口 snippet / 附件续读 / frontmatter 不入索引。

（2026-09-21 审计：这些行为此前分别是"没有向量路""文件开头当证据""长附件读不到后段"
"模板字段占命中窗口"。）
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from paperkb import api
from paperkb.config import Roots
from paperkb.doc import PaperDoc
from paperkb.llm import FakeLLM
from paperkb.models import PaperMeta


@pytest.fixture()
def env(tmp_path: Path):
    roots = Roots(data_dir=tmp_path / "data",
                  library_dir=tmp_path / "library",
                  kb_dir=tmp_path / "kb").ensure()
    api.init_kb(roots)
    api.configure_llm(FakeLLM())
    yield roots
    api._store = None
    api._journals = None
    api._compiler = None
    api._settings = None
    from paperkb import llm
    llm._client = None  # noqa: SLF001


def _mk_paper(roots: Roots, doi: str, title: str, note: str) -> Path:
    from paperkb.doi import doi_to_dirname

    api._need_store().upsert_meta(  # noqa: SLF001
        PaperMeta(doi=doi, title=title, journal="J", year="2024",
                  abstract="abs", source_file="t.bib"))
    d = roots.kb_dir / doi_to_dirname(doi)
    d.mkdir(parents=True, exist_ok=True)
    (d / "_note.md").write_text(note, encoding="utf-8")
    return d


DEEP_NOTE = (
    "---\ntype: paper-note\ndoi: 10.1000/deep.1\ntags: [paper, 电渗]\n---\n"
    "# 深窗口样例\n\n## 一句话贡献\n> 一句话。\n\n"
    "## 研究背景\n" + "背景铺垫内容。" * 40 + "\n\n"
    "## 结论\n电渗泵拖拽溶剂是该体系的关键机制。\n"
)


class TestVectorRecallChannel:
    @patch("paperkb.api.kb_vector_search")
    def test_vector_hits_enter_recall(self, mock_search, env):
        """向量一路（source=vector）应进入多路召回，片段是命中块原文。"""
        mock_search.return_value = [
            {"key": "10.1000/deep.1__note", "doi": "10.1000/deep.1",
             "passage_type": "note", "chunk": 0, "section": "结论",
             "score": 0.93, "snippet": "电渗泵拖拽溶剂是该体系的关键机制。"},
        ]
        items = api.recall("电渗泵机制")
        vec = [it for it in items if it["source"] == "vector"]
        assert vec, f"向量命中未进入召回: {items}"
        assert vec[0]["snippet"].startswith("电渗泵拖拽溶剂")
        assert vec[0]["file"] == "_note.md"

    @patch("paperkb.api.kb_vector_search")
    def test_title_and_empty_snippet_skipped(self, mock_search, env):
        """title 块（只有标题）与空片段不注入。"""
        mock_search.return_value = [
            {"doi": "10.1000/a", "passage_type": "title", "score": 0.99,
             "snippet": "某标题"},
            {"doi": "10.1000/b", "passage_type": "note", "score": 0.9,
             "snippet": ""},
        ]
        items = api.recall("任意问句")
        assert not [it for it in items if it["source"] == "vector"]

    @patch("paperkb.api.kb_vector_search")
    def test_same_doi_keeps_best_chunk(self, mock_search, env):
        mock_search.return_value = [
            {"doi": "10.1000/a", "passage_type": "note", "chunk": 3, "score": 0.7,
             "snippet": "低分块"},
            {"doi": "10.1000/a", "passage_type": "note", "chunk": 1, "score": 0.95,
             "snippet": "高分块"},
        ]
        items = [it for it in api.recall("x") if it["source"] == "vector"]
        assert len(items) == 1 and items[0]["snippet"] == "高分块"

    @patch("paperkb.api.kb_vector_search", side_effect=RuntimeError("no vector"))
    def test_vector_failure_does_not_break_recall(self, mock_search, env):
        _mk_paper(env, "10.1000/plain.1", "Ionic actuators",
                  "# T\n\nIPMC actuators bend under voltage")
        api.rebuild_fts()
        items = api.recall("IPMC")
        assert items and items[0]["source"] == "notes"


class TestSnippetWindow:
    def test_like_fallback_window_hits_content_not_frontmatter(self, env):
        _mk_paper(env, "10.1000/deep.1", "深窗口样例", DEEP_NOTE)
        api.rebuild_fts()
        rows = api._need_store().search_notes("这篇研究的结论", limit=2)  # noqa: SLF001
        assert rows, "中文兜底应召回"
        snip = rows[0]["snippet"]
        assert "type: paper-note" not in snip, f"不得回文件开头 frontmatter: {snip!r}"
        assert "电渗" in snip, f"应是命中位置附近内容: {snip!r}"

    def test_index_notes_strips_frontmatter(self, env):
        _mk_paper(env, "10.1000/deep.1", "深窗口样例", DEEP_NOTE)
        api.rebuild_fts()
        content = api._need_store().notes_content("10.1000/deep.1", "_note.md")  # noqa: SLF001
        assert "type: paper-note" not in content
        assert "## 结论" in content


class TestAttachmentOffset:
    def test_long_attachment_continuation(self, env, tmp_path):
        from paperkb.attachments import import_attachment

        body = ("第一段内容。" * 50 + "\n\n" + "第二段内容。" * 50 + "\n\n"
                + "第三段结论。")
        import_attachment(env, "nd-demo1", "si", "long.md", body.encode("utf-8"),
                          store=api._need_store(), index=False)  # noqa: SLF001
        first = api.kb_attachment_read("nd-demo1", "si/long.md", max_chars=300)
        assert first["ok"] and first["truncated"] and first["next_offset"] > 0
        assert len(first["text"]) <= 300

        second = api.kb_attachment_read("nd-demo1", "si/long.md", max_chars=100000,
                                        offset=first["next_offset"])
        assert second["ok"] and second["next_offset"] == 0
        joined = first["text"] + second["text"]
        assert "第一段内容" in joined and "第三段结论" in joined

    def test_truncation_aligns_to_sentence_boundary(self, env):
        from paperkb.attachments import import_attachment

        body = "甲甲甲。\n\n" + "乙乙乙。" * 200 + "\n\n丙丙丙。"
        import_attachment(env, "nd-demo2", "si", "b.md", body.encode("utf-8"),
                          store=api._need_store(), index=False)  # noqa: SLF001
        out = api.kb_attachment_read("nd-demo2", "si/b.md", max_chars=200)
        assert out["ok"] and len(out["text"]) <= 200
        assert out["text"].endswith("。"), f"应在句边界收尾: {out['text'][-10:]!r}"

    def test_early_boundary_does_not_waste_budget(self, env):
        """极靠前的段落边界不该被采用（否则 200 字预算只回 3 个字符）。"""
        from paperkb.attachments import import_attachment

        body = "甲。\n\n" + "乙" * 500
        import_attachment(env, "nd-demo3", "si", "c.md", body.encode("utf-8"),
                          store=api._need_store(), index=False)  # noqa: SLF001
        out = api.kb_attachment_read("nd-demo3", "si/c.md", max_chars=200)
        assert out["ok"] and len(out["text"]) == 200


class TestPerPaperRecallIsolation:
    """带 doi 过滤的召回绝不返回其他文献（2026-09-21）。

    旧实现把条件拼成 `A OR B AND doi = ?`——SQL 里 AND 优先级更高 ⇒ 等价
    `A OR (B AND doi = ?)`，doi 只约束紧邻的那一个 LIKE，其余锚点的命中**跨文献泄漏**
    （实测单篇召回返回别篇 `_note.md`）。既有缺陷，06e63da 时代已存在。
    """

    def _two_papers(self, env):
        _mk_paper(env, "10.1000/a.1", "A 篇",
                  "# A\n\n电渗驱动机制：缩短离子迁移路径以提升收缩应变。")
        _mk_paper(env, "10.1000/b.1", "B 篇",
                  "# B\n\n磁驱动机制：磁场调控磁偶极取向实现弯曲变形。")
        api.rebuild_fts()

    def test_plain_doi_filter(self, env):
        self._two_papers(env)
        hits = api._need_store().search_notes("驱动", limit=10, doi="10.1000/a.1")  # noqa: SLF001
        assert {h["doi"] for h in hits} == {"10.1000/a.1"}

    def test_cjk_bigram_fallback_respects_doi(self, env):
        """整串不命中 → 走 bigram OR 兜底，doi 过滤仍须生效。"""
        self._two_papers(env)
        hits = api._need_store().search_notes(  # noqa: SLF001
            "驱动的机制是什么", limit=10, doi="10.1000/a.1")
        assert hits, "该篇应被 bigram 兜底召回"
        assert {h["doi"] for h in hits} == {"10.1000/a.1"}, f"doi 过滤被击穿: {hits}"

    def test_or_mode_respects_doi(self, env):
        self._two_papers(env)
        rows = api._need_store()._search_notes_like(  # noqa: SLF001
            "驱动 机制", limit=10, mode="OR", doi="10.1000/a.1")
        assert rows, "两词 OR 应命中该篇"
        assert {r["doi"] for r in rows} == {"10.1000/a.1"}, f"doi 过滤被击穿: {rows}"

    def test_recall_paper_returns_only_own_notes(self, env):
        """端到端：单篇检索只回该篇产物。"""
        self._two_papers(env)
        items = api.recall_paper("10.1000/a.1", "驱动的机制是什么", top_k=3)
        assert items and {it["doi"] for it in items} == {"10.1000/a.1"}


class TestWindowBoundaryAlignment:
    """命中窗口的两条口径修复（2026-09-21）。

    旧 `match_window` 的窗口宽度**正好等于 limit** ⇒ `boundary_trim` 判定"无需裁"
    原样返回，右边界是硬切（实测尾部「…剩磁仅1.」「…为水基的」）；且上限 700 太窄，
    1625 字的 `_note.md` 里偏移 603~1100 的数值怎么都取不到。
    """

    def test_right_edge_lands_on_boundary(self):
        from paperkb.db import match_window

        content = "电渗" + "前置句子。" * 300 + "x" * 500
        out = match_window(content, ["电渗"], limit=1200, radius=300)
        assert len(out) <= 1200
        assert out.rstrip()[-1] in "。！？；，\n", f"右边界仍是硬切: {out[-20:]!r}"

    def test_notes_window_limit_is_1200(self, env):
        """笔记片段上限与向量路（SNIPPET_CHARS=1200）对齐，不再是 700。"""
        from paperkb.db import NOTE_WINDOW_LIMIT

        assert NOTE_WINDOW_LIMIT == 1200
        _mk_paper(env, "10.1000/long.1", "长笔记",
                  "# T\n\n电渗锚点内容。" + "填充句子。" * 400)
        api.rebuild_fts()
        rows = api._need_store().search_notes("电渗锚点", limit=2)  # noqa: SLF001
        assert rows
        n = len(rows[0]["snippet"])
        assert n <= NOTE_WINDOW_LIMIT, f"片段超上限: {n}"
        assert n > 800, f"片段仍被 700 字老上限卡住: {n}"

    def test_fts_primary_path_also_boundary_aligned(self, env):
        """主路（FTS MATCH）也走命中窗口——旧实现用 SQLite `snippet(...,12)`。"""
        _mk_paper(env, "10.1000/en.1", "English",
                  "# T\n\nIPMC actuators bend under voltage." + " filler text." * 200)
        api.rebuild_fts()
        rows = api._need_store().search_notes("IPMC actuators", limit=2)  # noqa: SLF001
        assert rows, "英文主路应召回"
        assert rows[0]["snippet"].startswith("IPMC") or "IPMC" in rows[0]["snippet"]


class TestContextAssembly:
    """注入上下文组装：产物整份优先、向量路不丢、预算受控（2026-09-21）。"""

    def _notes_item(self, doi: str, snippet: str, source: str = "vector") -> dict:
        return {"doi": doi, "file": "_note.md", "snippet": snippet, "source": source}

    def test_vector_source_items_not_dropped(self, env):
        """`source="vector"` 的条目必须进上下文（旧实现两个名单都不含 ⇒ 整条丢）。"""
        from paperkb.retrieve import build_context

        _mk_paper(env, "10.1000/v.1", "V 篇", "# V\n\n" + "向量命中正文。" * 20)
        api.rebuild_fts()
        store = api._need_store()  # noqa: SLF001
        ctx = build_context([self._notes_item("10.1000/v.1", "片段内容。", "vector")],
                            store, env)
        assert "10.1000/v.1" in ctx and "向量命中正文" in ctx

    def test_product_injected_whole_not_snippet(self, env):
        """产物命中：注入整份（含片段之外的尾部），不再被片段截断。"""
        from paperkb.retrieve import build_context

        body = "# T\n\n" + "正文段落。" * 100 + "\n\n尾部标记XYZ。"
        _mk_paper(env, "10.1000/w.1", "W 篇", body)
        api.rebuild_fts()
        store = api._need_store()  # noqa: SLF001
        ctx = build_context([self._notes_item("10.1000/w.1", "只有这一小段。")], store, env)
        assert "尾部标记XYZ" in ctx, f"应注入整份产物: {ctx[-80:]!r}"

    def test_dedupes_same_product(self, env):
        from paperkb.retrieve import build_context

        _mk_paper(env, "10.1000/d.1", "D 篇", "# D\n\n" + "去重正文。" * 20)
        store = api._need_store()  # noqa: SLF001
        ctx = build_context([self._notes_item("10.1000/d.1", "a", "vector"),
                             self._notes_item("10.1000/d.1", "b", "notes")], store, env)
        assert ctx.count("[10.1000/d.1]") == 1

    def test_budget_respected(self, env):
        from paperkb.retrieve import build_context

        for i in range(5):
            _mk_paper(env, f"10.1000/b{i}.1", f"B{i}", "# B\n\n" + "预算正文。" * 300)
        store = api._need_store()  # noqa: SLF001
        items = [self._notes_item(f"10.1000/b{i}.1", "s") for i in range(5)]
        ctx = build_context(items, store, env, budget_chars=4000)
        assert ctx, "必须先真的有内容（否则空上下文也能过预算断言）"
        assert len(ctx) <= 4000

    def test_qa_context_facade(self, env):
        """门面 `qa_context` = 召回条目 + 组装好的上下文（backend 问答路径用）。"""
        _mk_paper(env, "10.1000/q.1", "Q 篇", "# Q\n\n电渗泵机制与性能指标。" * 10)
        api.rebuild_fts()
        packed = api.qa_context("电渗泵机制", top_k=4, budget_chars=4000)
        assert set(packed) == {"items", "context", "context_chars"}
        assert packed["context"] and packed["context_chars"] == len(packed["context"])
        assert packed["context_chars"] <= 4000
        assert any(it["doi"] == "10.1000/q.1" for it in packed["items"])
