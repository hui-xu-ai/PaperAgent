# -*- coding: utf-8 -*-
"""回归（用户反馈 2026-09-12）：L2 编译在"无 `sections` 键"的 document.json 上不得产出占位。

真实现象：该篇被判定 L2 后 `_details.md` 只有 763B 的「待补充：章节原文与段落 ID…」，
但 job 被标记 done、UI 无报错；且因为文件已存在，后续 `skipped_existing` **永久挡住重编**。
根因：`_prompt_l2` 的章节片段只来自 `doc.sections`，而 P14 解析产物**没有 `sections` 键**
（实测 L2 那次 LLM 输入仅 372 token，L1 是 23975）。
"""
from __future__ import annotations

import json

import pytest

from paperkb import api
from paperkb.compile import Compiler, _l2_output_ok, _l2_sections, _prompt_l2
from paperkb.config import Roots
from paperkb.doc import read_document
from paperkb.llm import FakeLLM

DOC_NO_SECTIONS = {
    "metadata": {"doi": "10.1000/l2.1", "title": "L2 测试篇"},
    "paragraphs": [
        {"para_id": "P001", "section": "Introduction", "text_en": "Intro a."},
        {"para_id": "P002", "section": "Introduction", "text_en": "Intro b."},
        {"para_id": "P003", "section": "Methods", "text_en": "Method a."},
        {"para_id": "P004", "section": "Methods", "text_en": "Method b."},
        {"para_id": "P005", "section": "Results", "text_en": "Result a."},
        {"para_id": "P006", "section": "Results", "text_en": "Result b."},
    ],
}


@pytest.fixture()
def env(tmp_path):
    roots = Roots(data_dir=tmp_path / "data", library_dir=tmp_path / "library",
                  kb_dir=tmp_path / "kb").ensure()
    api.init_kb(roots)
    api.configure_llm(FakeLLM())
    yield roots
    for attr in ("_store", "_journals", "_compiler", "_settings"):
        setattr(api, attr, None)
    from paperkb import llm
    llm._client = None  # noqa: SLF001


def _write_doc(roots, doi="10.1000/l2.1"):
    d = roots.kb_dir / "10.1000_l2.1"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "document.json"
    p.write_text(json.dumps(DOC_NO_SECTIONS, ensure_ascii=False), encoding="utf-8")
    (d / "_note.md").write_text("# L1\n\n一句话：测试。\n", encoding="utf-8")
    return p


def test_l2_sections_aggregated_from_paragraphs_when_missing(env):
    """没有 `sections` 键时，章节清单必须能从段落聚合出来（否则 L2 拿不到正文）。"""
    doc = read_document(_write_doc(env))
    assert not doc.sections, "前置条件：该 document.json 确实没有 sections"
    secs = _l2_sections(doc)
    names = [s["section"] for s in secs]
    assert names == ["Introduction", "Methods", "Results"], names
    assert all(s["count"] == 2 for s in secs)


def test_l2_prompt_contains_body_when_no_sections(env):
    """关键断言：L2 提示词里必须**有正文片段与段落 ID**，不能是"(无可用章节片段)"。"""
    doc = read_document(_write_doc(env))
    prompt = _prompt_l2({"title": "T"}, doc, "L1 摘要")
    assert "(无可用章节片段)" not in prompt, "这正是那次占位产出的直接原因"
    assert "[P001]" in prompt and "[P005]" in prompt
    assert "Intro a." in prompt


def test_l2_output_gate_rejects_placeholder():
    assert not _l2_output_ok("### 待补充：章节原文与段落 ID\n当前输入的章节片段为空")
    assert not _l2_output_ok("太短")
    assert not _l2_output_ok("# 详细笔记\n- 有内容但没有段落引用")   # 引用 < 3
    ok = "# 详细笔记\n### Introduction\n- 要点一（[P001]）\n- 要点二（[P002]）\n- 要点三（[P003]）\n"
    assert _l2_output_ok(ok)


def test_compile_l2_rejects_placeholder_output(env):
    """LLM 回退成占位文本时必须**失败**（而不是静默写盘 + done）。"""
    from paperkb.compile import CompileError

    doc_path = _write_doc(env)
    store = api._need_store()  # noqa: SLF001
    comp = Compiler(store, api._journals, store.roots)  # noqa: SLF001
    fake = FakeLLM(["### 待补充：章节原文与段落 ID\n当前输入的章节片段（原文 text_en）为空…"])
    api.configure_llm(fake)
    with pytest.raises(CompileError):
        comp.compile("10.1000/l2.1", "L2", force=True)
    assert not (doc_path.parent / "_details.md").exists(), "无效产物不得落盘"


def test_compile_l2_rewrites_invalid_existing_file(env):
    """已存在但无效的 `_details.md` 不得触发 `skipped_existing`（否则永久挡住重编）。"""
    doc_path = _write_doc(env)
    (doc_path.parent / "_details.md").write_text(
        "### 待补充：章节原文与段落 ID\n空", encoding="utf-8")
    store = api._need_store()  # noqa: SLF001
    comp = Compiler(store, api._journals, store.roots)  # noqa: SLF001
    api.configure_llm(FakeLLM([
        "# 详细笔记\n### Introduction\n- 要点（[P001]）\n- 要点（[P002]）\n- 要点（[P003]）\n"]))
    r = comp.compile("10.1000/l2.1", "L2", force=False)
    assert r["status"] == "done", r
    md = (doc_path.parent / "_details.md").read_text(encoding="utf-8")
    assert "待补充" not in md and "[P001]" in md
