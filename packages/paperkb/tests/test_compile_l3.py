# -*- coding: utf-8 -*-
"""L3 概念关系层编译单测。

覆盖：
- 相关文献检索（概念倒排 + 引用 + 聚类）
- 编译结果上下文提取
- L3 prompt 构建
- _relations.md 渲染
- 双向 cross_refs 追加
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from paperkb import api
from paperkb.compile import Compiler, _prompt_l3, _prompt_l3_keywords, _render_relations
from paperkb.config import KbSettings, Roots
from paperkb.doi import doi_to_dirname
from paperkb.llm import FakeLLM


L3_JSON = json.dumps({
    "summary": "\u8fd9\u4e9b\u6587\u732e\u5171\u540c\u6784\u6210\u4e86\u667a\u80fd\u6750\u6599 4D \u6253\u5370\u9886\u57df\u7684\u7814\u7a76\u8109\u7edc\u3002",
    "concept_map": [
        {"concept": "4D printing",
         "papers": ["10.1002/adma.202407106", "10.1002/adma.202400001"],
         "evolution": "\u4ece\u5355\u4e00\u6750\u6599\u5230\u590d\u5408\u6750\u6599\u7684\u6f14\u8fdb"},
    ],
    "methodology_connections": [
        {"from": "10.1002/adma.202407106", "to": "10.1002/adma.202400001",
         "relation": "\u65b9\u6cd5\u6539\u8fdb"},
    ],
    "research_trajectory": "\u4ece\u57fa\u7840\u7814\u7a76\u5411\u5e94\u7528\u8f6c\u5316\u52a0\u901f\u3002",
}, ensure_ascii=False)


@pytest.fixture()
def env(tmp_path: Path):
    roots = Roots(data_dir=tmp_path / "data",
                  library_dir=tmp_path / "library",
                  kb_dir=tmp_path / "kb").ensure()
    api.init_kb(roots, KbSettings(vector_impl="noop"))
    compiler = api._compiler
    return roots, compiler


def _make_compiled_paper(kb_dir: Path, doi: str,
                         concepts: list[dict] | None = None) -> None:
    """\u521b\u5efa\u6a21\u62df\u7f16\u8bd1\u4ea7\u7269\u3002"""
    folder = kb_dir / doi_to_dirname(doi)
    folder.mkdir(parents=True, exist_ok=True)
    note = (
        f"---\ntype: paper-note\ndoi: {doi}\ntags: [paper]\n---\n\n"
        f"# Paper {doi}\n\n"
        f"## \u4e00\u53e5\u8bdd\u8d21\u732e\n> \u8d21\u732e\u63cf\u8ff0\n\n"
        f"## \u516d\u7ef4\u603b\u7ed3\n### \u7814\u7a76\u80cc\u666f\n\u80cc\u666f\u5185\u5bb9\n"
        f"### \u7814\u7a76\u65b9\u6cd5\n\u65b9\u6cd5\u5185\u5bb9\n### \u7814\u7a76\u7ed3\u679c\n\u7ed3\u679c\u5185\u5bb9\n"
        f"### \u7ed3\u8bba\n\u7ed3\u8bba\u5185\u5bb9\n### \u521b\u65b0\u70b9\n\u521b\u65b0\u5185\u5bb9\n### \u5c40\u9650\n\u5c40\u9650\u5185\u5bb9\n\n"
        f"## \u6982\u5ff5\u6807\u7b7e\n#tag1 #tag2\n"
    )
    (folder / "_note.md").write_text(note, encoding="utf-8")

    wiki = (
        f"---\ntype: paper-wiki\ndoi: {doi}\n---\n\n"
        f"# \u6df1\u5ea6\u7f16\u8bd1\uff1a{doi}\n\n"
        f"## \u65b9\u6cd5\u8bba\u6279\u5224\n\u6279\u5224\u5185\u5bb9\n\n"
        f"## \u53ef\u590d\u73b0\u6027\u5206\u6790\n\u5206\u6790\u5185\u5bb9\n\n"
        f"## \u6f5c\u5728\u5e94\u7528\n\u5e94\u7528\u5185\u5bb9\n"
    )
    (folder / "_wiki.md").write_text(wiki, encoding="utf-8")

    if concepts:
        store = api._store
        for c in concepts:
            store.upsert_concept(doi, c["name"], c.get("definition", ""))


class TestL3RelatedPapers:
    def test_find_related_by_concepts(self, env):
        roots, compiler = env
        _make_compiled_paper(roots.kb_dir, "10.1002/a",
                             concepts=[{"name": "GNN", "definition": "\u56fe\u795e\u7ecf\u7f51\u7edc"}])
        _make_compiled_paper(roots.kb_dir, "10.1002/b",
                             concepts=[{"name": "GNN", "definition": "\u56fe\u795e\u7ecf\u7f51\u7edc\u53d8\u4f53"}])
        _make_compiled_paper(roots.kb_dir, "10.1002/c",
                             concepts=[{"name": "Transformer", "definition": "\u6ce8\u610f\u529b\u6a21\u578b"}])

        related = compiler._find_related_papers("10.1002/a", top_k=5)
        dois = [r["doi"] for r in related]
        assert "10.1002/b" in dois
        assert "10.1002/c" not in dois

    def test_find_related_excludes_self(self, env):
        roots, compiler = env
        _make_compiled_paper(roots.kb_dir, "10.1002/a",
                             concepts=[{"name": "GNN", "definition": ""}])

        related = compiler._find_related_papers("10.1002/a", top_k=5)
        dois = [r["doi"] for r in related]
        assert "10.1002/a" not in dois


class TestL3CompiledContext:
    def test_extract_note_summary(self, env):
        roots, compiler = env
        _make_compiled_paper(roots.kb_dir, "10.1002/a")

        ctx = compiler._compiled_context("10.1002/a")
        assert "\u8d21\u732e\u63cf\u8ff0" in ctx
        assert "\u80cc\u666f\u5185\u5bb9" in ctx

    def test_empty_for_missing(self, env):
        roots, compiler = env
        ctx = compiler._compiled_context("10.1002/nonexistent")
        assert ctx == ""


class TestL3Prompt:
    def test_prompt_l3_keywords_structure(self):
        meta = {"doi": "10.1002/a", "title": "Test Paper"}
        self_ctx = "本文贡献描述\n背景内容"
        prompt = _prompt_l3_keywords(meta, self_ctx)
        assert "关键词" in prompt
        assert "10.1002/a" in prompt
        assert "Test Paper" in prompt
        assert self_ctx in prompt

    def test_prompt_l3_structure(self):
        meta = {"doi": "10.1002/a", "title": "Test Paper"}
        self_ctx = "\u672c\u6587\u8d21\u732e\u63cf\u8ff0\n\u80cc\u666f\u5185\u5bb9"
        related_ctxs = [
            {"doi": "10.1002/b", "context": "\u76f8\u5173\u8bba\u6587B\u5185\u5bb9",
             "score": 0.8, "connection": "concept:GNN"},
        ]
        prompt = _prompt_l3(meta, self_ctx, related_ctxs)
        assert "\u6982\u5ff5\u5173\u7cfb" in prompt
        assert "10.1002/b" in prompt
        assert "\u76f8\u5173\u8bba\u6587B\u5185\u5bb9" in prompt


class TestL3Render:
    def test_render_relations(self, env):
        roots, compiler = env
        meta = MagicMock()
        meta.doi = "10.1002/a"
        meta.title = "Test Paper"

        data = json.loads(L3_JSON)
        related = [{"doi": "10.1002/b", "connection": "concept:4D printing",
                     "context": "", "score": 0.8}]

        result = _render_relations(meta, data, related)
        assert "paper-relations" in result
        assert "\u6982\u5ff5\u5173\u7cfb\u56fe" in result
        assert "4D printing" in result
        assert "\u65b9\u6cd5\u8bba\u8fde\u63a5" in result
        assert "\u7814\u7a76\u8d8b\u52bf" in result


class TestL3CrossRefs:
    def test_bidirectional_cross_refs(self, env):
        roots, compiler = env
        _make_compiled_paper(roots.kb_dir, "10.1002/a")
        _make_compiled_paper(roots.kb_dir, "10.1002/b")

        related = [{"doi": "10.1002/b", "connection": "concept:GNN",
                     "context": "", "score": 0.8}]
        compiler._bidirectional_cross_refs("10.1002/a", related)

        dir_b = doi_to_dirname("10.1002/b")
        wiki_b = (roots.kb_dir / dir_b / "_wiki.md").read_text(encoding="utf-8")
        dir_a = doi_to_dirname("10.1002/a")
        assert f"{dir_a}/_note" in wiki_b

    def test_cross_refs_idempotent(self, env):
        roots, compiler = env
        _make_compiled_paper(roots.kb_dir, "10.1002/a")
        _make_compiled_paper(roots.kb_dir, "10.1002/b")

        related = [{"doi": "10.1002/b", "connection": "", "context": "", "score": 0.5}]
        compiler._bidirectional_cross_refs("10.1002/a", related)
        dir_b = doi_to_dirname("10.1002/b")
        wiki_b_1 = (roots.kb_dir / dir_b / "_wiki.md").read_text(encoding="utf-8")

        compiler._bidirectional_cross_refs("10.1002/a", related)
        wiki_b_2 = (roots.kb_dir / dir_b / "_wiki.md").read_text(encoding="utf-8")
        assert wiki_b_1 == wiki_b_2


class TestL3Compile:
    def test_compile_l3_empty_keywords_falls_back(self, env):
        """LLM 返回空关键词 → **不硬失败**：回退到「已聚合概念 + bib 关键词」继续做 L3。

        2026-09-23 改口径（原测试期望抛 CompileError「关键词提取失败」）：L3 的价值是跨文献
        关系，不该因一次关键词提取失败就整篇放弃。现在只有"向量 + 混合检索都空"才报错
        （见 `test_compile_l3_no_search_results`），且**告警日志**会写明走了回退。
        """
        roots, compiler = env
        _make_compiled_paper(
            roots.kb_dir, "10.1002/a",
            concepts=[{"name": "Smart Materials", "definition": "智能材料"}])
        _make_compiled_paper(roots.kb_dir, "10.1002/b")     # 有相关文献，L3 应当跑完

        fake_llm = FakeLLM(responses=["", L3_JSON])         # 第 1 问回空 → 回退；第 2 问出关系
        from paperkb import llm as llm_mod
        llm_mod.configure_llm(fake_llm)

        seen: dict = {}

        def _spy(keywords, doi, top_k=15):
            seen["kw"] = list(keywords)
            return [{"doi": "10.1002/b", "score": 0.9, "passage_type": "title"}]

        with patch.object(compiler, "_search_related_by_keywords", side_effect=_spy):
            res = compiler._compile_l3("10.1002/a", force=False)

        assert "Smart Materials" in seen["kw"], f"应回退到已聚合概念名，实际 {seen['kw']}"
        assert res["status"] == "done" and res["related"] == 1
        assert (roots.kb_dir / doi_to_dirname("10.1002/a") / "_relations.md").exists()

    def test_compile_l3_no_search_results(self, env):
        """关键词有了，但向量检索 + 混合检索**都空**时抛 CompileError（带归因的文案）。"""
        roots, compiler = env
        _make_compiled_paper(roots.kb_dir, "10.1002/a")

        kw_response = json.dumps(["smart materials", "4D printing"])
        fake_llm = FakeLLM(responses=[kw_response])
        from paperkb import llm as llm_mod
        llm_mod.configure_llm(fake_llm)

        with patch.object(compiler, "_search_related_by_keywords",
                          return_value=[]):
            from paperkb.compile import CompileError
            with pytest.raises(CompileError, match="无相关文献"):
                compiler._compile_l3("10.1002/a", force=False)

    def test_compile_l3_full_flow(self, env):
        """\u5b8c\u6574 L3 \u7f16\u8bd1\u6d41\u7a0b\uff08mock LLM \u4e24\u6b65 + mock \u5411\u91cf\u68c0\u7d22\uff09\u3002"""
        roots, compiler = env
        for suffix in ["a", "b", "c", "d"]:
            doi = f"10.1002/{suffix}"
            _make_compiled_paper(
                roots.kb_dir, doi,
                concepts=[{"name": "Smart Materials", "definition": "\u667a\u80fd\u6750\u6599"}])

        kw_response = json.dumps(["smart materials", "4D printing", "GNN"])
        fake_llm = FakeLLM(responses=[kw_response, L3_JSON])
        from paperkb import llm as llm_mod
        llm_mod.configure_llm(fake_llm)

        mock_results = [
            {"doi": "10.1002/b", "score": 0.9, "passage_type": "title"},
            {"doi": "10.1002/c", "score": 0.8, "passage_type": "note"},
            {"doi": "10.1002/d", "score": 0.7, "passage_type": "wiki"},
        ]
        with patch.object(compiler, "_search_related_by_keywords",
                          return_value=mock_results):
            result = compiler._compile_l3("10.1002/a", force=False)

        assert result["status"] == "done"
        assert result["level"] == "L3"
        assert result["related"] == 3
        assert "keywords" in result

        dir_a = doi_to_dirname("10.1002/a")
        relations_path = roots.kb_dir / dir_a / "_relations.md"
        assert relations_path.exists()
        content = relations_path.read_text(encoding="utf-8")
        assert "paper-relations" in content

    def test_parse_l3_keywords_json(self, env):
        _, compiler = env
        raw = json.dumps(["GNN", "graph neural network", "4D printing"])
        kws = compiler._parse_l3_keywords(raw)
        assert len(kws) == 3
        assert "GNN" in kws

    def test_parse_l3_keywords_csv(self, env):
        _, compiler = env
        kws = compiler._parse_l3_keywords("GNN, 4D printing\nshape memory")
        assert len(kws) == 3
        assert "shape memory" in kws
