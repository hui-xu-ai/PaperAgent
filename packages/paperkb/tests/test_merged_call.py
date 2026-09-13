# -*- coding: utf-8 -*-
"""合并单次调用（`paperkb.merged`）契约测试。

背景：智谱隐式缓存在"相邻调用间隔 < ~120s"时恒不命中，而一次流转的 4 步紧连 ⇒ 全文前缀
被重复计价 4 次。用户决策（2026-09-13）：针对智谱把 **L1+L2+L3+翻译合并成一次请求**。
本测试锁定四件事：
1. `merged_task` 的 user 里**四个子任务文本与既有构造点逐字节一致**（不产生第二套任务描述）；
2. user **不含共享全文前缀**（前缀仍由 system 承载）且不出现 `TASK_MARK`（否则会被拆错）；
3. `parse_merged` 对"整体 JSON / 带围栏 / 前后有解释文字 / 四项缺一"的解析与缺项判定；
4. `translation_coverage` 按 para_id 计覆盖（给回退判据用）。
"""
from __future__ import annotations

import json

from paperkb.context import TASK_MARK, split_task
from paperkb.merged import (_task_of, merged_task, parse_merged,  # noqa: F401
                            translation_coverage)


# ---------------------------------------------------------------- 最小夹具（与 test_task_split 同构）
def _mk_doc():
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


def _meta() -> dict:
    return {"doi": "10.1/a", "title": "Shared ctx paper", "authors": ["A. Author"],
            "abstract": "IPMC actuators."}


def test_task_of_matches_split_task():
    """`_task_of` 必须与既有 `split_task` 同一分界（不引入第二套切分逻辑）。"""
    from paperkb.compile import _prompt_l1

    prompt = _prompt_l1(_meta(), _mk_doc(), "JIF 5.0")
    assert _task_of(prompt) == split_task(prompt)[1]


def test_merged_task_reuses_existing_task_texts():
    """四个子任务必须原样复用既有构造点产出的任务文本。

    第 ② 项传的是"同源但无正文段落"的 doc（合并调用里正文已在 system，user 不再内联章节片段）
    —— 任务指令本体仍逐字节来自 `_prompt_l2`，不产生第二套描述。
    """
    from paperkb.compile import _prompt_l1, _prompt_l2, _prompt_l3
    from paperkb.translate.pipeline import _translate_task

    doc, meta = _mk_doc(), _meta()
    ids = ["P001", "P002"]
    user = merged_task(meta, doc, "JIF 5.0", target_ids=ids)
    ph = "(同一次调用内，请以上面 ① 的产出为准)"
    doc_no_body = type(doc)(doi="10.1/a", title="Shared ctx paper")
    doc_no_body.sections = list(doc.sections or [])

    assert _task_of(_prompt_l1(meta, doc, "JIF 5.0")) in user
    assert _task_of(_prompt_l2(meta, doc_no_body, ph)) in user
    assert _task_of(_prompt_l3(meta, doc, ph, ph)) in user
    assert _task_of(_translate_task(ids)) in user


def test_merged_task_has_no_shared_prefix_and_no_marker():
    """user 里不得含共享全文前缀、不得含 `TASK_MARK`，也不得把正文整篇重塞一遍。"""
    doc, meta = _mk_doc(), _meta()
    user = merged_task(meta, doc, "", target_ids=["P001"])
    assert TASK_MARK not in user
    assert "## 论文全文" not in user          # 全文块属于 system 侧
    assert "[P001] The energy is" not in user  # 正文块（带 para_id 标记）不得重复塞回
    assert '"translations"' in user           # 输出规格必须在
    assert "P001" in user                     # 待译清单内联


def test_parse_merged_happy_path():
    payload = {
        "l1": {"one_liner": "一句话", "concepts": []},
        "l2_md": "# 详细笔记\n## 章节要点\n- x [P001]",
        "l3": {"summary": "摘要", "wiki": "## 研究设计"},
        "translations": [{"para_id": "P001", "zh": "译文一"}],
    }
    parsed = parse_merged(json.dumps(payload, ensure_ascii=False))
    assert parsed["l1"]["one_liner"] == "一句话"
    assert parsed["l2_md"].startswith("# 详细笔记")
    assert parsed["l3"]["summary"] == "摘要"
    assert parsed["translations"][0]["zh"] == "译文一"


def test_parse_merged_tolerates_fence_and_prose():
    body = json.dumps({"l1": {"one_liner": "x"}, "translations": [
        {"para_id": "P001", "zh": "y"}]}, ensure_ascii=False)
    raw = f"好的，结果如下：\n```json\n{body}\n```\n以上。"
    parsed = parse_merged(raw)
    assert parsed["l1"]["one_liner"] == "x"
    assert parsed["translations"][0]["para_id"] == "P001"


def test_parse_merged_partial_reports_missing_parts():
    """只回了译文（笔记缺失）⇒ 必须如实反映缺项，供调用方"缺什么补什么"。"""
    raw = json.dumps({"translations": [{"para_id": "P001", "zh": "只有译文"}]},
                     ensure_ascii=False)
    parsed = parse_merged(raw)
    assert parsed["l1"] is None
    assert parsed["l2_md"] == ""
    assert parsed["l3"] is None
    assert len(parsed["translations"]) == 1


def test_parse_merged_bad_json_returns_empty_parts():
    parsed = parse_merged("模型没按要求输出，只有一段中文说明。")
    assert (parsed["l1"], parsed["l2_md"], parsed["l3"], parsed["translations"]) == (
        None, "", None, [])


def test_translation_coverage_counts_only_target_ids():
    trans = [{"para_id": "P001", "zh": "x"},
             {"para_id": "P002", "zh": "  "},
             {"para_id": "P999", "zh": "外部段"}]
    assert translation_coverage(trans, ["P001", "P002"]) == 0.5
    assert translation_coverage(trans, []) == 1.0


# ---------------------------------------------------------------- 对话方式：笔记任务 / 翻译任务

def test_notes_task_l1_only_and_l1l2():
    """L1-only 与 L1+L2 的任务文本形状（L2 只在需要时出现；两条都复用既有任务文本）。"""
    from paperkb.compile import _prompt_l1, _prompt_l2
    from paperkb.merged import _doc_task_only, notes_task

    doc, meta = _mk_doc(), _meta()
    l1_only = notes_task(meta, doc, "JIF 5.0", levels=("L1",))
    assert _task_of(_prompt_l1(meta, doc, "JIF 5.0")) in l1_only
    assert '"l2_md"' not in l1_only                    # 只要 L1 时不给 L2 键
    assert '"l1"' in l1_only

    l1l2 = notes_task(meta, doc, "JIF 5.0", levels=("L1", "L2"))
    assert _task_of(_prompt_l1(meta, doc, "JIF 5.0")) in l1l2
    assert _task_of(_prompt_l2(meta, _doc_task_only(doc),
                               "(同一次调用内，请以上面 ① 的产出为准)")) in l1l2
    assert '"l2_md"' in l1l2
    assert "## 论文全文" not in l1l2 and TASK_MARK not in l1l2


def test_notes_task_is_byte_stable():
    """同一 doc/meta/levels 两次构造必须逐字节相同（对话前缀能命中缓存的前提）。"""
    from paperkb.merged import notes_task

    doc, meta = _mk_doc(), _meta()
    a = notes_task(meta, doc, "JIF", levels=("L1", "L2"))
    b = notes_task(meta, doc, "JIF", levels=("L1", "L2"))
    assert a == b


def test_translate_task_matches_pipeline_text():
    from paperkb.merged import translate_task
    from paperkb.translate.pipeline import _translate_task

    assert translate_task(["P001", "P002"]) == _task_of(_translate_task(["P001", "P002"]))


def test_parse_notes_partial_and_translations_parser():
    from paperkb.merged import parse_notes, parse_translations

    assert parse_notes('{"l1": {"one_liner": "x"}}')["l2_md"] == ""
    assert parse_notes("```json\n{\"l1\": {\"one_liner\": \"y\"}, \"l2_md\": \"# a\"}\n```")[
        "l2_md"] == "# a"
    assert parse_notes("没有 JSON")["l1"] is None

    tr = parse_translations('说明：\n{"translations":[{"para_id":"P001","zh":"一"}]}\n完毕')
    assert tr and tr[0]["para_id"] == "P001"
    assert parse_translations("无") == []
