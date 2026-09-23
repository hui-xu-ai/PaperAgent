# -*- coding: utf-8 -*-
"""编译提示词质量契约 + L3 上下文预算 + `_note.md` 核心概念段（2026-09-23 提示词升级的守卫）。

背景：同日 A/B 实测（同模型、同共享前缀、只换任务段）显示候选提示词在 wiki 批判覆盖
5/10→10/10、段落引用 9→30、定量结果 4→39 上更好，代价 +31% 输出 / +44% 耗时。本文件把
「候选之所以更好」的那几个特征**钉成断言**，防止后续重构把检查项、证据规则、长度下限悄悄删掉。

这几条不是"格式好看"，而是产出质量的自变量：
- 逐项检查清单 → 模型才知道"批判"要回答什么（旧版只说"写一段方法论批判"）；
- 证据硬规则 → 反幻觉 + 区分「作者主张」与「证据支持度」；
- 长度**下限**（实测上限对 glm 类模型无效）→ 决定输出深度；
- wiki 段在 L1 段之前 → 深度优先。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from paperkb.compile import (
    Compiler,
    _L3_MIN_KEEP,
    _L3_MIN_SCORE,
    _L3_NOTE_CHARS,
    _L3_WIKI_CHARS,
    _NO_LATEX_RULE,
    _filter_by_score,
    _prompt_l1,
    _prompt_l2,
    _prompt_l1_l2_merged,
    _render_note,
)
from paperkb.config import Roots
from paperkb.context import shared_ctx, split_task
from paperkb.doi import doi_to_dirname
from paperkb.models import PaperMeta


def _mk_doc():
    from paperkb.doc import PaperDoc, Para

    doc = PaperDoc(doi="10.1/a", title="Prompt contract paper")
    doc.paragraphs = [
        Para(para_id="P000", section="Introduction", text_en="Introduction", is_heading=True),
        Para(para_id="P001", section="Introduction", text_en="The energy is $E=mc^2$."),
        Para(para_id="P002", section="Introduction", text_en="Second paragraph."),
    ]
    doc.sections = [{"section": "Introduction", "count": 2}]
    return doc


def _meta() -> dict:
    return {"title": "Prompt contract paper", "abstract": "A.", "keywords": ["ipmc"],
            "year": "2026", "journal": "J", "doi": "10.1/a"}


def _user_part(prompt: str, shared: str) -> str:
    assert prompt.startswith(shared)
    return prompt[len(shared):]


# ---------------------------------------------------------------- 1) 提示词质量契约

def test_merged_prompt_carries_checklist_and_evidence_rules():
    """合并编译任务段必须带：逐项检查清单 + 证据硬规则 + 权威性校准。"""
    doc = _mk_doc()
    user = _user_part(_prompt_l1_l2_merged(_meta(), doc, "JIF 5.0"), shared_ctx(doc))

    for anchor in ("对照与混杂", "样本量与统计", "表征证据链", "结论外推",
                   "数据与代码的可得性", "最小可行的下一步"):
        assert anchor in user, f"L2 检查项缺失: {anchor}"
    for anchor in ("原文未报告", "作者主张", "证据支持度", "段落 ID"):
        assert anchor in user, f"证据硬规则缺失: {anchor}"
    assert "资深审稿人" in user and "权威性校准" in user
    assert "禁止" in user and "抬高结论" in user      # 权威性只调审视强度，不抬结论


def test_checklist_present_in_standalone_l1_l2_prompts():
    """分离路径（_prompt_l1/_prompt_l2）与合并路径同一口径，别只改一处。"""
    doc = _mk_doc()
    l1_user = _user_part(_prompt_l1(_meta(), doc, ""), shared_ctx(doc))
    l2_user = _user_part(_prompt_l2(_meta(), doc, "L1CTX"), shared_ctx(doc))

    for anchor in ("对照与混杂", "最小可行的下一步"):
        assert anchor in l2_user
    assert "科研知识编译助手" in l1_user and "科研深度编译专家" in l2_user
    for anchor in ("原文未报告", "证据支持度"):
        assert anchor in l1_user and anchor in l2_user


def test_length_rules_are_floors_not_caps():
    """长度约束写**下限**（实测 glm 类模型不遵守上限，上限只会占位）。"""
    doc = _mk_doc()
    user = _user_part(_prompt_l1_l2_merged(_meta(), doc, ""), shared_ctx(doc))

    assert "不少于 200 字" in user        # wiki 每节
    assert "不少于 80 字" in user         # 六维每维
    assert "wiki 每节 **不超过" not in user


def test_wiki_section_precedes_l1_section():
    """wiki（L2 深度）写在 L1 之前——深度优先，实测模型更愿意写透。"""
    doc = _mk_doc()
    user = _user_part(_prompt_l1_l2_merged(_meta(), doc, ""), shared_ctx(doc))
    assert user.index("## L2 深度分析") < user.index("## L1 知识卡")


def test_shared_prefix_byte_identical_across_paths():
    """升级只动任务段：三路共享前缀仍逐字节一致（否则提示词缓存失效）。"""
    doc = _mk_doc()
    shared = shared_ctx(doc)
    merged = _prompt_l1_l2_merged(_meta(), doc, "J")
    l1 = _prompt_l1(_meta(), doc, "J")
    l2 = _prompt_l2(_meta(), doc, "L1CTX")
    for p in (merged, l1, l2):
        sys_part, _task = split_task(p)
        assert sys_part == shared


def test_no_latex_rule_allows_unicode_scripts():
    """反 LaTeX 硬约束仍在，且显式允许 Unicode 上下标（避免模型为写 K+ 而抄 LaTeX）。"""
    assert "严禁使用 LaTeX" in _NO_LATEX_RULE
    assert "美元符号" in _NO_LATEX_RULE
    assert "\u207b" in _NO_LATEX_RULE and "\u00c5" in _NO_LATEX_RULE   # ⁻  Å


# ---------------------------------------------------------------- 2) L3 上下文预算

@pytest.fixture()
def kb_env(tmp_path: Path):
    roots = Roots(data_dir=tmp_path / "data",
                  library_dir=tmp_path / "library",
                  kb_dir=tmp_path / "kb").ensure()
    return roots


def _write_note_and_wiki(roots: Roots, doi: str, note_chars: int, wiki_chars: int) -> None:
    d = roots.kb_dir / doi_to_dirname(doi)
    d.mkdir(parents=True, exist_ok=True)
    (d / "_note.md").write_text(
        "## 一句话贡献\n> 一句话\n\n## 六维总结\n### 研究背景\n" + ("背景内容。" * note_chars),
        encoding="utf-8")
    (d / "_wiki.md").write_text(
        "---\ntype: paper-wiki\ndoi: %s\n---\n\n## 方法论批判\n" % doi
        + ("批判内容。" * wiki_chars), encoding="utf-8")


def test_l3_budgets_widened(kb_env):
    """note 1200 / wiki 1500：旧值（1500/750）让 wiki 只拿到 note 的一半。"""
    assert _L3_NOTE_CHARS == 1200 and _L3_WIKI_CHARS == 1500

    doi = "10.1000/budget.1"
    _write_note_and_wiki(kb_env, doi, note_chars=4000, wiki_chars=4000)
    compiler = Compiler(store=None, journals=None, roots=kb_env)
    ctx = compiler._compiled_context(doi)         # noqa: SLF001

    assert len(ctx) > 2000, f"L3 每篇上下文应显著超过旧上限（1500+750=2250 的一半），实得 {len(ctx)}"
    assert "批判内容" in ctx and "背景内容" in ctx


def test_l3_score_filter_with_keep_guard():
    """阈值过滤生效；但过滤后不足 _L3_MIN_KEEP 篇时退回原列表（不把 L3 掐死）。"""
    hits = [{"score": s} for s in (0.9, 0.8, 0.7, 0.6, 0.5, 0.45, 0.1, 0.05, 0.01, 0.0)]
    kept, dropped = _filter_by_score(hits, _L3_MIN_SCORE, _L3_MIN_KEEP)
    assert len(kept) == 6 and dropped == 4

    low = [{"score": 0.1}] * 9
    kept2, dropped2 = _filter_by_score(low, _L3_MIN_SCORE, _L3_MIN_KEEP)
    assert kept2 == low and dropped2 == 0


# ---------------------------------------------------------------- 3) _note.md 核心概念段

def _meta_obj() -> PaperMeta:
    return PaperMeta(doi="10.1000/note.1", title="Note paper")


def test_render_note_includes_core_concepts_with_definitions():
    note = _render_note(_meta_obj(), {
        "one_liner": "贡献",
        "concepts": [{"name": "IPMC actuator", "definition": "离子聚合物金属复合材料致动器"}],
        "tags": ["soft-robotics"],
    })
    assert "## 核心概念" in note
    assert "**IPMC actuator**：离子聚合物金属复合材料致动器" in note
    assert note.index("## 核心概念") < note.index("## 概念标签")


def test_render_note_without_concepts_degrades_to_placeholder():
    note = _render_note(_meta_obj(), {"one_liner": "x"})
    assert "## 核心概念\n(无)" in note or "## 核心概念\n\n(无)" in note


def test_extract_note_summary_keeps_core_concepts():
    """L3 拿到的上下文里必须有概念定义（否则跨文献分析只有一串裸词）。"""
    note = ("## 一句话贡献\n> 贡献\n\n## 六维总结\n### 研究背景\n背景。\n\n"
            "## 核心概念\n- **IPMC actuator**：离子聚合物金属复合材料致动器\n\n"
            "## 概念标签\n#soft-robotics\n")
    summary = Compiler._extract_note_summary(note, 1200)  # noqa: SLF001
    assert "IPMC actuator" in summary and "致动器" in summary
    assert "soft-robotics" in summary or "概念标签" in summary
