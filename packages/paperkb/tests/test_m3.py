# -*- coding: utf-8 -*-
"""M3 单测：编译系统（L1/L2/队列/概念聚合）+ 卡片（FakeLLM，不调真实 API）。"""
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
    api._store = None
    api._journals = None
    api._compiler = None
    api._settings = None
    from paperkb import llm
    llm._client = None  # noqa: SLF001


def _setup_paper(roots: Roots, doi: str = "10.1000/ipmc.1") -> None:
    """入库 meta + kb/<DOI>/document.json。"""
    meta = PaperMeta(doi=doi, title="3D printing of IPMC", journal="SCIENCE CHINA-MATERIALS",
                     year="2026", abstract="IPMC actuators for soft robotics.",
                     keywords=["IPMC", "3D printing"], times_cited=3,
                     source_file="test.bib", issn="2095-8226")
    api._need_store().upsert_meta(meta)  # noqa: SLF001
    from paperkb.doi import doi_to_dirname
    d = roots.kb_dir / doi_to_dirname(doi)
    d.mkdir(parents=True, exist_ok=True)
    doc = {
        "metadata": {"doi": doi, "title": meta.title},
        "sections": [{"section": "Introduction", "count": 2},
                     {"section": "Methods", "count": 2}],
        "paragraphs": [
            {"para_id": "P001", "section": "Introduction", "text_en": "IPMC are smart materials.", "is_heading": False},
            {"para_id": "P002", "section": "Introduction", "text_en": "Soft robotics needs actuators.", "is_heading": False},
            {"para_id": "P005", "section": "Methods", "text_en": "FFF printing with PLA.", "is_heading": False},
            {"para_id": "P010", "section": "Methods", "text_en": "Bending angle 169 deg.", "is_heading": False},
        ],
    }
    (d / "document.json").write_text(json.dumps(doc), encoding="utf-8")


# ---------------------------------------------------------------- L1

def test_compile_l1(env, tmp_path):
    roots = env
    _setup_paper(roots)
    from paperkb import llm as llm_mod
    llm_mod.get_llm()._responses.append(L1_JSON)  # noqa: SLF001
    r = api.compile_now("10.1000/ipmc.1", "L1")
    assert r["status"] == "done"
    from paperkb.doi import doi_to_dirname
    note = roots.kb_dir / doi_to_dirname("10.1000/ipmc.1") / "_note.md"
    text = note.read_text(encoding="utf-8")
    assert "PLA 增强 PFSR" in text            # one_liner
    assert "[P001]" in text                    # 段落引用
    assert "#soft-robotics" in text            # 概念标签
    # P2：元数据不再写入 _note.md（由翻译 frontmatter 承载）
    # ctx 与 job 状态
    assert api._need_store().get_ctx("10.1000/ipmc.1", "L1")
    assert api._need_store().get_job("10.1000/ipmc.1", "L1")["status"] == "done"
    # 幂等：产物存在 → 跳过
    r2 = api.compile_now("10.1000/ipmc.1", "L1")
    assert r2["status"] == "skipped_existing"
    # force 覆盖（补响应，FakeLLM 单次消费）
    llm_mod.get_llm()._responses.append(L1_JSON)  # noqa: SLF001
    r3 = api.compile_now("10.1000/ipmc.1", "L1", force=True)
    assert r3["status"] == "done"


def test_compile_l2_injects_l1_ctx(env, tmp_path):
    """新 L2（原 L3）产出 _wiki.md，prompt 须注入 L1 摘要以避免重复。"""
    roots = env
    _setup_paper(roots)
    from paperkb import llm as llm_mod
    fake: FakeLLM = llm_mod.get_llm()
    fake._responses.append(L1_JSON)  # noqa: SLF001
    api.compile_now("10.1000/ipmc.1", "L1")
    # 新 L2 产出 _wiki.md（JSON 格式）
    l2_json = json.dumps({
        "wiki": "## 方法论批判\n分析。\n## 可复现性分析\n数据。\n## 潜在应用\n应用（[P001]）（[P002]）（[P005]）",
        "concepts": [{"name": "IPMC actuator", "definition": "离子聚合物金属复合材料致动器"}],
    }, ensure_ascii=False)
    fake._responses.append(l2_json)
    r = api.compile_now("10.1000/ipmc.1", "L2")
    assert r["status"] == "done"
    ctx, prompt = fake._calls[-1]
    assert "L1 摘要" in prompt or "已覆盖" in prompt


def test_compile_l2_concepts(env, tmp_path):
    """L2 编译产出 concepts，3 篇同概念应聚合生成概念页。"""
    roots = env
    _setup_paper(roots, "10.1000/ipmc.1")
    _setup_paper(roots, "10.1000/ipmc.2")
    _setup_paper(roots, "10.1000/ipmc.3")
    from paperkb import llm as llm_mod
    fake: FakeLLM = llm_mod.get_llm()
    # 先做 L1
    for doi in ("10.1000/ipmc.1", "10.1000/ipmc.2", "10.1000/ipmc.3"):
        fake._responses.append(L1_JSON)
        api.compile_now(doi, "L1")
    # 再做 L2（原 L3）
    l2_json = json.dumps({
        "wiki": "## 方法论批判\n分析。",
        "concepts": [{"name": "IPMC actuator", "definition": "离子聚合物金属复合材料致动器"}],
    }, ensure_ascii=False)
    for doi in ("10.1000/ipmc.1", "10.1000/ipmc.2", "10.1000/ipmc.3"):
        fake._responses.append(l2_json)
        api.compile_now(doi, "L2")
    # 概念页：3 篇同概念 → 生成
    page = roots.kb_dir / "_concepts" / "ipmc-actuator.md"
    assert page.exists(), "3 文献同概念应聚合生成概念页"
    assert "10.1000/ipmc.1" in page.read_text(encoding="utf-8")


def test_queue_and_process(env, tmp_path):
    roots = env
    _setup_paper(roots)
    q = api.compile_queue("10.1000/ipmc.1")   # 无 level → 按价值分
    assert q["status"] in ("queued", "skipped_done")
    r = api.compile_process(limit=5)
    assert r and r[0]["level"] in ("L1", "L2")
    jobs = api.compile_status()
    assert jobs and jobs[0]["paper_doi"] == "10.1000/ipmc.1"


def test_queue_records_real_value_score(env, tmp_path):
    """★批5 回归钉（用户实测：编译队列「价值分」显示 0.00）：

    显式 level 入队时，队列行的 `value_score` 必须是**真实价值分**（旧实现写死 0.0，
    与 `/api/kb-meta/scores` 里该篇的分数不一致，UI 显示 0.00 误导）。
    """
    roots = env
    _setup_paper(roots)
    q = api.compile_queue("10.1000/ipmc.1", "L1")     # 显式 level（旧实现 → 0.0）
    assert q["status"] in ("queued", "skipped_done")
    jobs = [j for j in api.compile_status() if j["paper_doi"] == "10.1000/ipmc.1"]
    assert jobs, "应有一条队列记录"
    real = api.value_score_for("10.1000/ipmc.1")
    assert jobs[0]["value_score"] == float((real or {}).get("score") or 0.0)
    assert jobs[0]["value_score"] > 0, "有元数据/期刊信息时价值分不应为 0"


def test_queue_explicit_score_is_respected(env, tmp_path):
    """调用方显式给分（批量入队路径）时不得被覆盖。"""
    roots = env
    _setup_paper(roots, "10.1000/ipmc.9")
    from paperkb.api import _need_compiler
    _need_compiler().queue("10.1000/ipmc.9", "L2", value_score=4.25)
    jobs = [j for j in api.compile_status() if j["paper_doi"] == "10.1000/ipmc.9"]
    assert jobs and jobs[0]["value_score"] == 4.25


def test_queue_skipped_no_doc_bib_only(env, tmp_path):
    """A3：无 document.json（bib-only 篇）→ 不入队，返回 skipped_no_doc。

    - 不抛异常、不写 queued、不产生 failed 垃圾
    - 已有 done 跳过逻辑保留（有 doc 的 done 篇仍返回 skipped_done）
    """
    roots = env
    meta = PaperMeta(doi="10.1000/bibonly.1", title="Bib only paper",
                     journal="TEST J", year="2025", abstract="no kb doc",
                     source_file="test.bib")
    api._need_store().upsert_meta(meta)  # noqa: SLF001
    # 显式等级入队 → skipped_no_doc
    q = api.compile_queue("10.1000/bibonly.1", "L1")
    assert q == {"status": "skipped_no_doc", "doi": "10.1000/bibonly.1",
                 "level": "L1"}
    # 自动等级入队（level=None 走价值分）→ 同样跳过
    q2 = api.compile_queue("10.1000/bibonly.1")
    assert q2["status"] == "skipped_no_doc"
    # 队列中无该篇、无 failed 记录
    jobs = api.compile_status()
    assert all(j["paper_doi"] != "10.1000/bibonly.1" for j in jobs)
    # 幂等：重复入队同样跳过
    q3 = api.compile_queue("10.1000/bibonly.1", "L1")
    assert q3["status"] == "skipped_no_doc"
    # done 跳过逻辑保留：有 doc 且已 done 的篇不因 A3 改变语义
    _setup_paper(roots, "10.1000/ipmc.1")
    from paperkb import llm as llm_mod
    llm_mod.get_llm()._responses.append(L1_JSON)  # noqa: SLF001
    api.compile_now("10.1000/ipmc.1", "L1")
    assert api.compile_queue("10.1000/ipmc.1", "L1")["status"] == "skipped_done"


def test_parse_json_tolerant():
    from paperkb.compile import _parse_json
    assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _parse_json('前文 {"a": 1} 后文') == {"a": 1}
    assert _parse_json("not json") == {}


def _setup_paper_zh(roots: Roots, doi: str, title: str = "3D printing of IPMC",
                    with_meta: bool = True) -> None:
    """入库 kb/<DOI>/document.json（含 text_zh + References 节）。with_meta=False 模拟无 bib。"""
    if with_meta:
        meta = PaperMeta(doi=doi, title=title, journal="SCIENCE CHINA-MATERIALS",
                         year="2026", abstract="IPMC actuators for soft robotics.",
                         keywords=["IPMC"], times_cited=3, source_file="test.bib")
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
             "text_zh": "IPMC 是智能材料。", "is_heading": False},
            {"para_id": "P002", "section": "Introduction", "text_en": "Soft robotics needs actuators.",
             "text_zh": "软体机器人需要致动器。", "is_heading": False},
            {"para_id": "P005", "section": "Methods", "text_en": "FFF printing with PLA.",
             "text_zh": "FFF 打印 PLA。", "is_heading": False},
            # References：不应进入编译输入（text_en 仅英文，未译）
            {"para_id": "P900", "section": "References", "text_en": "REF1 Smith et al. 2020.",
             "text_zh": "", "is_heading": False},
            {"para_id": "P901", "section": "References", "text_en": "REF2 Johnson et al. 2021.",
             "text_zh": "", "is_heading": False},
        ],
    }
    (d / "document.json").write_text(json.dumps(doc), encoding="utf-8")


def test_compile_l1_reads_original_text_skips_references(env, tmp_path):
    """编译 L1 **直读原文全文 text_en**（不依赖 text_zh/未译也能编译），并跳过 References 节。"""
    roots = env
    _setup_paper_zh(roots, "10.1000/zh.1")
    from paperkb import llm as llm_mod
    fake: FakeLLM = llm_mod.get_llm()
    fake._responses.append(L1_JSON)  # noqa: SLF001
    r = api.compile_now("10.1000/zh.1", "L1")
    assert r["status"] == "done"
    _, prompt = fake._calls[-1]
    assert "IPMC are smart." in prompt          # 原文 text_en 进正文（编译直读原文）
    assert "IPMC 是智能材料。" not in prompt     # 不读 text_zh（编译不依赖翻译结果）
    assert "REF1 Smith" not in prompt           # References 已跳过
    assert "REF2 Johnson" not in prompt
    assert "[P001]" in prompt                   # 正文段落 ID 保留


def test_compile_l1_bib_optional(env, tmp_path):
    """A5：未导 bib（无 papers_meta）不阻断编译——元数据从 document.json 兜底。"""
    roots = env
    _setup_paper_zh(roots, "10.1000/nobib.1", title="No bib paper", with_meta=False)
    from paperkb import llm as llm_mod
    fake: FakeLLM = llm_mod.get_llm()
    fake._responses.append(L1_JSON)  # noqa: SLF001
    r = api.compile_now("10.1000/nobib.1", "L1")
    assert r["status"] == "done"
    from paperkb.doi import doi_to_dirname
    note = roots.kb_dir / doi_to_dirname("10.1000/nobib.1") / "_note.md"
    assert note.exists()
    text = note.read_text(encoding="utf-8")
    assert "No bib paper" in text          # title 从 document.json 兜底
    assert "PLA 增强 PFSR" in text          # one_liner 正常输出


def test_queue_bib_optional(env, tmp_path):
    """A5：无 bib 但有 document.json → 入队（不再硬拦「先 bib 导入」）。"""
    roots = env
    _setup_paper_zh(roots, "10.1000/qbib.1", with_meta=False)
    q = api.compile_queue("10.1000/qbib.1", "L1")
    assert q["status"] == "queued"
    # 无 doc（bib-only）仍跳过，不抛错
    q2 = api.compile_queue("10.1000/bibonly.1")
    assert q2["status"] == "skipped_no_doc"


# ---------------------------------------------------------------- 卡片

def test_cards(env, tmp_path):
    roots = env
    _setup_paper(roots)
    w = api.card_write("10.1000/ipmc.1", "card-qa", "问：驱动原理？答：离子迁移",
                       title="驱动原理", para_ids=["P001"], tags=["actuator"])
    assert w["slug"].startswith("qa-")
    w2 = api.card_write("10.1000/ipmc.1", "card-note", "个人笔记：注意 PLA 含量",
                        title="PLA 含量")
    lst = api.card_list("10.1000/ipmc.1")
    assert len(lst) == 2
    qa = api.card_list("10.1000/ipmc.1", "card-qa")
    assert len(qa) == 1 and qa[0]["para_ids"] == ["P001"]
    ctx = api.card_read_for_compile("10.1000/ipmc.1", ["card-qa"])
    assert "驱动原理" in ctx
    assert "个人笔记" not in ctx
    with pytest.raises(ValueError):
        api.card_write("10.1000/ipmc.1", "card-bad", "x")
