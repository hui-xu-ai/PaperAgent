# -*- coding: utf-8 -*-
"""共享全文前缀（paper_context）单测：编译直读原文、一次产出 _note、前缀字节一致缓存、全流程闭环。

覆盖用户需求：
- a) 编译输入为原文全文（text_en，跳 References）；未译也能编译
- b) 一次 LLM 调用产出含一言+六维+引用的 _note.md（无独立 summary 调用，mock 计数）
- c) 翻译与编译共享前缀字节一致（断言两 prompt 前缀相同 → 命中提示词缓存）
- d) 全流程（翻译+编译）仍闭环
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from paperkb import api
from paperkb.config import Roots
from paperkb.llm import FakeLLM
from paperkb.models import PaperMeta

L1_JSON = json.dumps({
    "one_liner": "提出 PLA 增强 PFSR 的 3D 打印 IPMC 驱动器",
    "background": {"text": "IPMC 是软体机器人关键致动器", "paras": ["P001", "P002"]},
    "method": {"text": "FFF 3D 打印 + PLA 增强", "paras": ["P005"]},
    "result": {"text": "弯曲角 169 度", "paras": ["P010"]},
    "conclusion": {"text": "可编程变形", "paras": ["P012"]},
    "innovation": {"text": "PLA 增强打印", "paras": ["P005"]},
    "limitation": {"text": "需进一步验证", "paras": []},
    "concepts": [{"name": "IPMC actuator", "definition": "离子聚合物金属复合材料致动器"}],
    "tags": ["soft-robotics", "3D-printing"],
}, ensure_ascii=False)


@pytest.fixture()
def env(tmp_path: Path):
    roots = Roots(data_dir=tmp_path / "data",
                  library_dir=tmp_path / "library",
                  kb_dir=tmp_path / "kb").ensure()
    api.init_kb(roots)
    api.configure_llm(FakeLLM())
    yield roots
    api._store = None       # noqa: SLF001
    api._journals = None    # noqa: SLF001
    api._compiler = None    # noqa: SLF001
    api._settings = None    # noqa: SLF001
    from paperkb import llm
    llm._client = None      # noqa: SLF001


def _mk_doc() -> "PaperDoc":
    from paperkb.doc import PaperDoc, Para

    doc = PaperDoc(doi="10.1/a", title="Shared ctx paper")
    doc.paragraphs = [
        Para(para_id="P000", section="Introduction", text_en="Introduction", is_heading=True),
        Para(para_id="P001", section="Introduction", text_en="The energy is $E=mc^2$."),
        Para(para_id="P002", section="Introduction", text_en="Second paragraph."),
        Para(para_id="P900", section="References", text_en="REF1 Smith et al. 2020."),
    ]
    doc.sections = [{"section": "Introduction", "count": 2}]
    return doc


def _setup_paper(roots: Roots, doi: str, title: str = "Shared ctx paper",
                 with_zh: bool = False) -> Path:
    """入库 meta + kb/<DOI>/document.json（含 References 节；默认 text_zh 空）。"""
    meta = PaperMeta(doi=doi, title=title, journal="SCIENCE CHINA-MATERIALS",
                     year="2026", abstract="IPMC actuators for soft robotics.",
                     keywords=["IPMC"], times_cited=3, source_file="test.bib",
                     issn="2095-8226")
    api._need_store().upsert_meta(meta)  # noqa: SLF001
    from paperkb.doi import doi_to_dirname

    d = roots.kb_dir / doi_to_dirname(doi)
    d.mkdir(parents=True, exist_ok=True)
    doc = {
        "metadata": {"doi": doi, "title": title},
        "sections": [{"section": "Introduction", "count": 2},
                     {"section": "Methods", "count": 1}],
        "paragraphs": [
            {"para_id": "P001", "section": "Introduction", "text_en": "IPMC are smart.",
             "text_zh": "IPMC 是智能材料。" if with_zh else "", "is_heading": False},
            {"para_id": "P002", "section": "Introduction", "text_en": "Soft robotics needs actuators.",
             "text_zh": "软体机器人需要致动器。" if with_zh else "", "is_heading": False},
            {"para_id": "P005", "section": "Methods", "text_en": "FFF printing with PLA.",
             "text_zh": "" if not with_zh else "FFF 打印 PLA。", "is_heading": False},
            {"para_id": "P900", "section": "References", "text_en": "REF1 Smith et al. 2020.",
             "text_zh": "", "is_heading": False},
        ],
    }
    dpath = d / "document.json"
    dpath.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return dpath


# ---------------------------------------------------------------- a/c：paper_context 共享前缀
def test_paper_context_block():
    """paper_context 共享块：标题+章节+正文 text_en，[[MATHn]] 全局占位，跳过 References。"""
    from paperkb.context import paper_context_with_math

    doc = _mk_doc()
    block, math_list = paper_context_with_math(doc)
    assert doc.title in block
    assert "## Introduction" in block
    assert "[P001]" in block
    assert "[P900]" not in block           # References 被跳过
    assert "REF1 Smith" not in block
    assert "[[MATH0]]" in block            # 公式全局占位
    assert math_list == ["$E=mc^2$"]       # 全局公式表（供 reassemble 回填）


def test_translate_compile_share_prefix():
    """c) 翻译/编译两 prompt 共享前缀字节一致（命中提示词缓存）。"""
    from paperkb.compile import _prompt_l1
    from paperkb.context import shared_ctx
    from paperkb.translate.pipeline import _translate_task

    doc = _mk_doc()
    shared = shared_ctx(doc)               # header + 同一全文块
    meta = {"title": doc.title, "abstract": "A.", "keywords": ["ipmc"],
            "year": "2026", "journal": "J"}
    compile_prompt = _prompt_l1(meta, doc, "JIF 5.0")
    # 与 pipeline 逐字一致的构造方式（shared 前缀在前，任务后缀在后）
    translate_prompt = shared + "\n\n" + _translate_task(["P001", "P002"])

    assert compile_prompt.startswith(shared)
    assert translate_prompt.startswith(shared)
    # 两结果的前缀（到共享块末尾）字节一致 → 服务端模型缓存对第 2+ 次命中缓存价
    assert compile_prompt[:len(shared)] == translate_prompt[:len(shared)]
    assert compile_prompt[:len(shared)] == shared


# ---------------------------------------------------------------- b：一次调用产出 _note
def test_l1_single_call_produces_note(env, tmp_path):
    """b) L1 编译单次 LLM 调用产出 _note.md（一言+六维+段落引用+概念），无独立 summary 调用。"""
    roots = env
    _setup_paper(roots, "10.1000/once.1")
    from paperkb import llm as llm_mod
    from paperkb.doi import doi_to_dirname

    fake: FakeLLM = llm_mod.get_llm()
    fake._responses.append(L1_JSON)  # noqa: SLF001
    r = api.compile_now("10.1000/once.1", "L1")
    assert r["status"] == "done"
    # 单次调用：仅 1 个 compile 上下文调用（无独立 summary 步骤）
    compile_calls = [c for c in fake._calls if c[0] == "compile"]
    assert len(compile_calls) == 1, "L1 编译应为单次 LLM 调用"
    note = roots.kb_dir / doi_to_dirname("10.1000/once.1") / "_note.md"
    text = note.read_text(encoding="utf-8")
    assert "PLA 增强 PFSR" in text            # 一句话贡献（一行摘要）
    assert "IPMC 是软体机器人关键致动器" in text  # 六维 background
    assert "[P001]" in text                   # 段落引用
    assert "#soft-robotics" in text           # 概念标签


# ---------------------------------------------------------------- d：全流程闭环
def test_full_pipeline_translate_then_compile(env, tmp_path):
    """d) 全流程（翻译+编译）闭环：先译出 text_zh，再编译产出 _note.md（编译读原文）。"""
    roots = env
    doi = "10.1000/full.1"
    dpath = _setup_paper(roots, doi)           # text_zh 空，未译
    from paperkb import llm as llm_mod
    from paperkb.doi import doi_to_dirname

    fake: FakeLLM = llm_mod.get_llm()
    # 1) 翻译：返回译文（para_id 键），References 段不译
    fake._responses.append(json.dumps({"translations": [
        {"para_id": "P001", "zh": "IPMC 是智能材料。"},
        {"para_id": "P002", "zh": "软体机器人需要致动器。"}]},
        ensure_ascii=False))
    tr = api.translate_paper(dpath)
    assert tr["translated"] == 2
    data = json.loads(dpath.read_text(encoding="utf-8"))
    assert data["paragraphs"][0]["text_zh"] == "IPMC 是智能材料。"
    assert data["paragraphs"][2]["text_zh"] == ""        # References 不译
    # 2) 编译 L1（翻译后仍能编译；编译直读原文 text_en）
    fake._responses.append(L1_JSON)  # noqa: SLF001
    r = api.compile_now(doi, "L1")
    assert r["status"] == "done"
    note = roots.kb_dir / doi_to_dirname(doi) / "_note.md"
    text = note.read_text(encoding="utf-8")
    assert "PLA 增强 PFSR" in text and "[P001]" in text
