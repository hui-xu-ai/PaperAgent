# -*- coding: utf-8 -*-
"""R1 不变量测试：L1 _note.md 六维由 paperkb Compiler（LLM）产出，非 kb_service 本地组装。

证据链（假 store + 假 LLM，不调真实 API）：
- kb/<DOI>/document.json **不含 ai_summary**；
- 若 _note.md 仍由旧 kb_service._build_note（读空 ai_summary）组装，则六维全为「(无)」；
- 现断言 _note.md 六维 = FakeLLM 返回的 LLM JSON 文本（「PLA 增强 PFSR」等），
  证明唯一 writer 是 paperkb Compiler._compile_l1（compile.py:146/150），非本地组装。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from paperkb import api
from paperkb.config import Roots
from paperkb.llm import FakeLLM
from paperkb.models import PaperMeta

DOI = "10.1000/ipmc.1"
# FakeLLM 返回的 Compiler L1 输出（六维=text+paras）
L1_JSON = json.dumps({
    "one_liner": "提出 PLA 增强 PFSR 的 3D 打印 IPMC 驱动器",
    "background": {"text": "IPMC 是软体机器人关键致动器", "paras": ["P001"]},
    "method": {"text": "FFF 3D 打印 + PLA 增强", "paras": ["P005"]},
    "result": {"text": "弯曲角 169 度", "paras": ["P010"]},
    "conclusion": {"text": "可编程变形", "paras": ["P012"]},
    "innovation": {"text": "PLA 增强打印", "paras": ["P005"]},
    "limitation": {"text": "需进一步验证", "paras": []},
    "concepts": [{"name": "IPMC actuator", "definition": "离子聚合物金属复合材料致动器"}],
    "tags": ["soft-robotics", "3D-printing"],
}, ensure_ascii=False)


@pytest.fixture()
def kb_env(tmp_path: Path):
    roots = Roots(data_dir=tmp_path / "data",
                  library_dir=tmp_path / "library",
                  kb_dir=tmp_path / "kb").ensure()
    api.init_kb(roots)
    api.configure_llm(FakeLLM())
    yield roots
    # 隔离：重置 paperkb 全局单例，避免污染其它 backend 测试
    api._store = None       # noqa: SLF001
    api._journals = None    # noqa: SLF001
    api._compiler = None    # noqa: SLF001
    api._settings = None    # noqa: SLF001
    from paperkb import llm
    llm._client = None      # noqa: SLF001


def _setup_no_summary_paper(roots: Roots) -> None:
    """入库 meta + kb/<DOI>/document.json（document.json **不含 ai_summary**）。"""
    from paperkb.doi import doi_to_dirname

    meta = PaperMeta(doi=DOI, title="3D printing of IPMC",
                     journal="SCIENCE CHINA-MATERIALS", year="2026",
                     abstract="IPMC actuators for soft robotics.",
                     keywords=["IPMC", "3D printing"], times_cited=3,
                     source_file="test.bib", issn="2095-8226")
    api._need_store().upsert_meta(meta)  # noqa: SLF001
    d = roots.kb_dir / doi_to_dirname(DOI)
    d.mkdir(parents=True, exist_ok=True)
    doc = {
        "metadata": {"doi": DOI, "title": meta.title},
        # 关键：不写 ai_summary，若走 kb_service 本地组装则六维会变「(无)」
        "sections": [{"section": "Introduction", "count": 2}],
        "paragraphs": [
            {"para_id": "P001", "section": "Introduction",
             "text_en": "IPMC are smart materials.", "is_heading": False},
            {"para_id": "P005", "section": "Methods",
             "text_en": "FFF printing with PLA.", "is_heading": False},
            {"para_id": "P010", "section": "Methods",
             "text_en": "Bending angle 169 deg.", "is_heading": False},
            {"para_id": "P012", "section": "Methods",
             "text_en": "Programmable deformation.", "is_heading": False},
        ],
    }
    (d / "document.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def test_l1_note_six_dim_writer_is_paperkb_compiler(kb_env):
    """某 DOI 编译后 _note.md 六维来自 Compiler LLM，非本地 ai_summary 组装。"""
    from paperkb import llm as llm_mod
    from paperkb.doi import doi_to_dirname

    _setup_no_summary_paper(kb_env)
    llm_mod.get_llm()._responses.append(L1_JSON)  # noqa: SLF001
    r = api.compile_now(DOI, "L1")
    assert r["status"] == "done"

    note = kb_env.kb_dir / doi_to_dirname(DOI) / "_note.md"
    text = note.read_text(encoding="utf-8")
    # 六维 = Compiler LLM JSON（来自 FakeLLM），逐维断言
    assert "PLA 增强 PFSR" in text            # one_liner
    assert "IPMC 是软体机器人关键致动器" in text   # background
    assert "FFF 3D 打印 + PLA 增强" in text       # method
    assert "弯曲角 169 度" in text               # result
    assert "[P001]" in text and "[P010]" in text  # 段落引用
    assert "#soft-robotics" in text and "#3D-printing" in text  # 概念标签
    # 反证：document.json 无 ai_summary，若走 kb_service 本地组装六维全为「(无)」→ 必须缺省
    assert "(无)" not in text, "六维不应出现本地组装占位 (无)"
