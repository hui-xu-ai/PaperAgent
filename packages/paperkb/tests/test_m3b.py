# -*- coding: utf-8 -*-
"""M3b 单测：翻译流水线（总结+分批译文、公式标签回填、KNOWN_FIXES、写回）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from paperkb.config import Roots
from paperkb.llm import FakeLLM
from paperkb.translate import run_translate
from paperkb.translate import pipeline
from paperkb.translate.latextap import reassemble, split_text

TR_JSON = json.dumps({"translations": [{"para_id": "P001", "zh": "这是一段测试译文 [[MATH0]] 保留公式"},
                                       {"para_id": "P002", "zh": "第二段译文"},
                                       {"para_id": "P003", "zh": "$\\Nu_{2}$ 出现在文中"}]})


@pytest.fixture()
def doc_path(tmp_path: Path) -> Path:
    p = tmp_path / "document.json"
    p.write_text(json.dumps({
        "metadata": {"doi": "10.1000/t.1", "title": "A test paper",
                     "abstract": "Abstract text."},
        "ai_summary": {},
        "paragraphs": [
            {"para_id": "P001", "section": "Intro", "is_heading": False,
             "text_en": "The energy is $E = mc^2$ here.", "text_zh": ""},
            {"para_id": "P002", "section": "Intro", "is_heading": False,
             "text_en": "Second paragraph with <sup>2</sup> sup.", "text_zh": ""},
            {"para_id": "P003", "section": "Methods", "is_heading": False,
             "text_en": "\\Nu_{2} appears in text.", "text_zh": ""},
        ],
    }), encoding="utf-8")
    return p


def test_split_reassemble():
    labeled, ml = split_text("Energy $E=mc^2$ and <sup>2</sup>")
    assert "[[MATH0]]" in labeled and "[[MATH1]]" in labeled
    assert ml[0] == "$E=mc^2$"
    out = reassemble("x [[MATH1]] y [[MATH0]]", ml)
    assert out == "x <sup>2</sup> y $E=mc^2$"
    # 越界标签保留
    assert reassemble("[[MATH9]]", ml) == "[[MATH9]]"


def test_run_translate(doc_path):
    fake = FakeLLM([TR_JSON])
    r = run_translate(doc_path, fake)
    assert r["translated"] == 3
    # D16：翻译不再生成六维总结（summary 恒空；不再写回 ai_summary）
    assert r["summary"] == {}
    data = json.loads(doc_path.read_text(encoding="utf-8"))
    assert not data.get("ai_summary")
    # 公式回填：[[MATH0]] → $E = mc^2$（reassemble 逐字节）
    zh0 = data["paragraphs"][0]["text_zh"]
    assert "$E = mc^2$" in zh0
    assert "[[MATH" not in zh0
    # KNOWN_FIXES：\\Nu_2 → \\mathrm { N } _ { 2 }
    zh2 = data["paragraphs"][2]["text_zh"]
    assert "\\mathrm { N } _ { 2 }" in zh2
    # 原字段保留
    assert data["paragraphs"][0]["text_en"] == "The energy is $E = mc^2$ here."


def test_translate_retry_on_bad_json(doc_path):
    """译文批返回坏 JSON → 重试一次成功。"""
    fake = FakeLLM(["not json", TR_JSON])
    r = run_translate(doc_path, fake)
    assert r["translated"] >= 1
    assert fake._calls[0][0] == "translate"  # context 分组（翻译独立组）


def test_translate_empty_targets(tmp_path):
    p = tmp_path / "document.json"
    p.write_text(json.dumps({"metadata": {}, "paragraphs": []}), encoding="utf-8")
    r = run_translate(p, FakeLLM())
    assert r["targets"] == 0 and r["translated"] == 0


# ---------------------------------------------------------------- 整篇一次优先（M3b 演进）
def _mk_doc(tmp_path, n=3):
    """构造 n 段正文的 document.json，返回路径。每段含一个 $E=mc^2$ 公式（自成 [[MATH0]]）。"""
    paras = [{"para_id": "P%03d" % i, "section": "S", "is_heading": False,
              "text_en": "para %d body with $E=mc^2$" % i, "text_zh": ""}
             for i in range(n)]
    p = tmp_path / "document.json"
    p.write_text(json.dumps({"metadata": {}, "paragraphs": paras}), encoding="utf-8")
    return p


def test_whole_once_single_call(tmp_path):
    """整篇一次：全部可译段落同一 prompt 单次调用，calls==1、全部 text_zh 写入。"""
    p = _mk_doc(tmp_path, n=3)
    full = json.dumps({"translations": [{"para_id": "P%03d" % i, "zh": "译文%d [[MATH0]]" % i}
                                        for i in range(3)]})
    fake = FakeLLM([full])
    r = run_translate(p, fake)
    assert r["calls"] == 1                       # 单次调用（整篇一次）
    assert r["translated"] == 3
    assert fake._calls[0][0] == "translate"
    prompt = fake._calls[0][1]
    # 整篇 prompt 确实包含全部段落（[P000]/[P001]/[P002] 都在单个 prompt 里，而非分批）
    for i in range(3):
        assert ("[P%03d]" % i) in prompt
    data = json.loads(p.read_text(encoding="utf-8"))
    for i in range(3):
        assert data["paragraphs"][i]["text_zh"] == ("译文%d $E=mc^2$" % i)  # 公式回填


def test_whole_parse_fail_falls_back_to_batch(tmp_path):
    """整篇一次 JSON 解析失败 → 回退分批：calls>1 且分批仍产出全部。"""
    p = _mk_doc(tmp_path, n=3)
    good = json.dumps({"translations": [{"para_id": "P%03d" % i, "zh": "译%d" % i}
                                        for i in range(3)]})
    # 第 1 次（整篇）返回坏 JSON → 回退分批；分批用第 2 个有效 JSON。
    fake = FakeLLM(["not json", good])
    r = run_translate(p, fake)
    assert r["calls"] > 1                        # 整篇失败后回退分批（多调用）
    assert r["translated"] == 3                  # 分批仍产出全部


def test_whole_too_large_goes_direct_batch(tmp_path, monkeypatch):
    """正文总量 > MAX_WHOLE_CHARS → 直接分批（不试整篇一次）；新放大因子下仍拆批产出全部。"""
    # 阈值压到 10：正文总量必然超过 → 走 /targets 分批。
    monkeypatch.setattr(pipeline, "MAX_WHOLE_CHARS", 10)
    paras = [{"para_id": "P%03d" % i, "section": "S", "is_heading": False,
              "text_en": "para %d body" % i, "text_zh": ""} for i in range(70)]
    p = tmp_path / "document.json"
    p.write_text(json.dumps({"metadata": {}, "paragraphs": paras}), encoding="utf-8")
    # 70 段按 MAX_BATCH_PARAS=64 分成 2 批（64 + 6）→ 每次返回一批的 translations。
    fake = FakeLLM([json.dumps({"translations": [{"para_id": "P%03d" % i, "zh": "译%d" % i}
                                                 for i in range(64)]}),
                    json.dumps({"translations": [{"para_id": "P%03d" % i, "zh": "译%d" % i}
                                                 for i in range(64, 70)]})])
    r = run_translate(p, fake)
    assert r["calls"] == 2                       # 2 批 = 2 次调用（未走整篇单次）
    assert r["translated"] == 70


# ---------------------------------------------------------------- References 跳过 / 批次放大
def test_batch_factors_amplified():
    """批次因子放大生效：MAX_BATCH_PARAS / MAX_BODY_CHARS 显著上调（少批大批，减少请求/限流）。"""
    assert pipeline.MAX_BATCH_PARAS >= 64
    assert pipeline.MAX_BODY_CHARS >= 30000


def test_reference_cut_legacy_kept_for_compat():
    """（批4）`_reference_cut` 已废弃（保留仅为兼容外部调用），新代码必须用 `context_paragraphs`。"""
    paras = [
        {"section": "Intro", "text_en": "body", "is_heading": False},
        {"section": "", "text_en": "参考文献", "is_heading": True},
        {"section": "Bibliography", "text_en": "b1", "is_heading": False},
    ]
    assert pipeline._reference_cut(paras) == 1        # 旧行为仍在（兼容）


def test_targets_must_be_visible_to_model(tmp_path):
    """★批4 回归钉（用户实测 bug）：**待译段落必须 ⊆ 模型看得到的段落**。

    实测现象：某篇 66 处「（原文未提供该段内容，无法翻译。）」污染 zh.md/en_zh.md。
    根因：待译清单用旧的自建 References 判据，而参考文献条目的 `section` 被解析层误标为
    "Keywords" ⇒ 旧判据切不到 ⇒ 这些段落进了待译清单却没出现在给模型的全文里。
    """
    paras = [
        {"para_id": "P001", "section": "Intro", "is_heading": False,
         "text_en": "real body text", "text_zh": ""},
        # 尾部杂项（Acknowledgements 之后整段被 tail_cut 截掉）→ 不得进待译清单
        {"para_id": "P002", "section": "Intro", "is_heading": False,
         "text_en": "Acknowledgements", "text_zh": ""},
        {"para_id": "P003", "section": "Keywords", "is_heading": False,
         "text_en": "[1] Y. Zhang, Y. Huang, W. Sun, J. Zhang, Adv. Mater. 2023, 35, 1.",
         "text_zh": ""},
        {"para_id": "P004", "section": "Keywords", "is_heading": False,
         "text_en": "[2] B. Gorissen, D. Melancon, N. Vasios, M. Torbati, K. Bertoldi, Adv. Mater. 2020.",
         "text_zh": ""},
    ]
    p = tmp_path / "document.json"
    p.write_text(json.dumps({"metadata": {}, "paragraphs": paras}), encoding="utf-8")
    # 模型只对 P001 返回译文（其余它根本看不到原文）
    fake = FakeLLM([json.dumps({"translations": [{"para_id": "P001", "zh": "真正的正文译文"}]})])
    r = run_translate(p, fake)
    assert r["targets"] == 1, "被尾部截断/误标 section 的段落不得进入待译清单"
    # 提示词里不得出现这些不可见段落的 para_id（否则模型只能回"原文未提供"）
    assert "P003" not in fake._calls[0][1] and "P004" not in fake._calls[0][1]


def test_refusal_text_is_not_written(tmp_path):
    """★批4 防御：即便模型回了"拒绝/占位"文本，也绝不写进 text_zh（按 rejected 丢弃）。"""
    paras = [
        {"para_id": "P001", "section": "Intro", "is_heading": False,
         "text_en": "body one", "text_zh": ""},
        {"para_id": "P002", "section": "Intro", "is_heading": False,
         "text_en": "body two", "text_zh": ""},
    ]
    p = tmp_path / "document.json"
    p.write_text(json.dumps({"metadata": {}, "paragraphs": paras}), encoding="utf-8")
    fake = FakeLLM([json.dumps({"translations": [
        {"para_id": "P001", "zh": "正常译文"},
        {"para_id": "P002", "zh": "（原文未提供该段内容，无法翻译。）"},
    ]})])
    r = run_translate(p, fake)
    assert r["translated"] == 1 and r["rejected"] == 1
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["paragraphs"][0]["text_zh"] == "正常译文"
    assert data["paragraphs"][1]["text_zh"] == "", "拒绝文本不得落盘"
    assert pipeline._is_refusal("unable to translate this paragraph")
    assert not pipeline._is_refusal("这是正常译文，讨论参考文献的作用。")


def test_context_source_is_compile_snapshot_writeback_is_library(tmp_path):
    """★批4 回归钉（用户要求"编译与翻译的全文必须一样"）：

    上下文/待译清单取 `context_path`（= 生产里 `shared_doc_json` 的 **kb 快照**，与编译同源），
    而 `text_zh` 仍写回传入的 `doc_json`（library 正本 = 翻译真相源）。
    """
    lib = tmp_path / "library" / "10.1" / "document.json"
    lib.parent.mkdir(parents=True)
    lib.write_text(json.dumps({"metadata": {}, "paragraphs": [
        {"para_id": "P001", "section": "Intro", "is_heading": False,
         "text_en": "LIBRARY-ONLY body (review 修正后)", "text_zh": ""},
    ]}), encoding="utf-8")
    kb = tmp_path / "kb" / "10.1" / "document.json"
    kb.parent.mkdir(parents=True)
    kb.write_text(json.dumps({"metadata": {}, "paragraphs": [
        {"para_id": "P001", "section": "Intro", "is_heading": False,
         "text_en": "KB snapshot body (编译当时那份)", "text_zh": ""},
    ]}), encoding="utf-8")

    fake = FakeLLM([json.dumps({"translations": [{"para_id": "P001", "zh": "译文"}]})])
    r = run_translate(lib, fake, context_path=kb)

    assert r["translated"] == 1
    # ① 提示词里的全文 = kb 快照（与编译同源 ⇒ 前缀缓存可共享）
    prompt = fake._calls[0][1]
    assert "KB snapshot body" in prompt and "LIBRARY-ONLY body" not in prompt
    # ② 译文写回 library（不是 kb）
    lib_data = json.loads(lib.read_text(encoding="utf-8"))
    kb_data = json.loads(kb.read_text(encoding="utf-8"))
    assert lib_data["paragraphs"][0]["text_zh"] == "译文"
    assert kb_data["paragraphs"][0]["text_zh"] == ""


def test_context_path_default_is_legacy_same_file(tmp_path):
    """不传 context_path ⇒ 退回旧行为（同一份文件），保证向后兼容。"""
    p = tmp_path / "document.json"
    p.write_text(json.dumps({"metadata": {}, "paragraphs": [
        {"para_id": "P001", "section": "Intro", "is_heading": False,
         "text_en": "body", "text_zh": ""},
    ]}), encoding="utf-8")
    fake = FakeLLM([json.dumps({"translations": [{"para_id": "P001", "zh": "译文"}]})])
    r = run_translate(p, fake)
    assert r["translated"] == 1
    assert json.loads(p.read_text(encoding="utf-8"))["paragraphs"][0]["text_zh"] == "译文"


def test_skip_references_section(tmp_path):
    """翻译源跳过 References：References heading 之后的所有段落不翻译，其余正文保留。"""
    paras = [
        {"para_id": "P000", "section": "Intro", "is_heading": False,
         "text_en": "intro body", "text_zh": ""},
        {"para_id": "P001", "section": "Methods", "is_heading": False,
         "text_en": "methods body", "text_zh": ""},
        {"para_id": "P002", "section": "References", "is_heading": True,
         "text_en": "References", "text_zh": ""},
        {"para_id": "P003", "section": "References", "is_heading": False,
         "text_en": "ref entry one", "text_zh": ""},
        {"para_id": "P004", "section": "References", "is_heading": False,
         "text_en": "ref entry two", "text_zh": ""},
    ]
    p = tmp_path / "document.json"
    p.write_text(json.dumps({"metadata": {}, "paragraphs": paras}), encoding="utf-8")
    good = json.dumps({"translations": [{"para_id": "P000", "zh": "译intro"},
                                        {"para_id": "P001", "zh": "译methods"}]})
    fake = FakeLLM([good])
    r = run_translate(p, fake)
    assert r["targets"] == 2
    assert r["translated"] == 2
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["paragraphs"][0]["text_zh"] == "译intro"
    assert data["paragraphs"][1]["text_zh"] == "译methods"
    assert data["paragraphs"][3]["text_zh"] == ""   # References 段不翻译
    assert data["paragraphs"][4]["text_zh"] == ""


def test_do_batch_no_recursive_split(tmp_path):
    """_do_batch 去递归拆批：批内 JSON 失败只同 prompt 重试一次，不切两半级联调用。"""
    p = _mk_doc(tmp_path, n=3)
    fake = FakeLLM(["bad", "bad"])               # 两次都坏 → 应上抛，而非拆半重试
    from paperkb.context import CTX_HEADER, paper_context_with_math
    from paperkb.doc import read_document
    data = json.loads(p.read_text(encoding="utf-8"))
    paras = data.get("paragraphs")
    doc = read_document(p)
    block, math_list = paper_context_with_math(doc)
    shared = CTX_HEADER + block
    para_id_to_idx = {pp.get("para_id"): i for i, pp in enumerate(paras)}
    with pytest.raises(ValueError):
        pipeline._do_batch(fake, shared, paras, [0, 1, 2], para_id_to_idx,
                           math_list, "translate", [0])
    assert fake._calls[0][0] == "translate"
    # 同一批只发 2 次（重试一次）；若递归拆批会多于 2 次。
    assert len(fake._calls) <= 2
