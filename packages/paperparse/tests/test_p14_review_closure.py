# -*- coding: utf-8 -*-
"""S0「复核清单收口」守卫（2026-09-16）。

背景（用户："看不到的错误要可见"）：此前三类问题**静默消失**——
  · `neither`（AI 判两侧都错）：不改文本、不进复核清单、audit 也不记；
  · `skip_p_ambiguous`（冲突片段在段内多次出现 → 放弃替换）：只写进无人读的 audit 文件；
  · `misaligned` / 低重叠段（未做双通道比对）：只进 stats，界面完全看不到。
本文件锁定新的清单 schema：**blocking 区分"待确认（门控翻译）"与"质量提示（仅展示）"**，
以及 `item_kind` 区分来源（conflict / skip_ambiguous / quality / domain）。
"""
import ast
from pathlib import Path

from paperparse.core.p14_pipeline import (_build_domain_review_item,
                                          _build_quality_items,
                                          _build_review_items,
                                          _build_skip_items)

P14 = Path(__file__).resolve().parents[1] / "paperparse" / "core" / "p14_pipeline.py"


class _R:
    """最小 RepairItem 替身（只需 kind / md_idx / para_id / text）。"""

    def __init__(self, para_id="RP001", kind="body", md_idx=(3,), text="M text"):
        self.para_id = para_id
        self.kind = kind
        self.md_idx = list(md_idx)
        self.text = text


def test_quality_items_are_non_blocking():
    flags = [{"para_id": "RP002", "page": 4, "verdict": "misaligned", "overlap_set": 0.11,
              "kind": "body", "mineru_text": "M", "paddle_text": "P",
              "reason": "段首对齐失败（配对错位/误拼接）→ 本段未做双通道比对"}]
    items = _build_quality_items(flags)
    assert len(items) == 1
    it = items[0]
    assert it["blocking"] is False, "质量提示项不得门控翻译"
    assert it["item_kind"] == "quality"
    assert it["page"] == 4
    assert it["evidence"]["verify_verdict"] == "misaligned"
    assert "对齐失败" in it["ai"]["reason"]


def test_low_overlap_item_keeps_reason_and_texts():
    items = _build_quality_items([{
        "para_id": "RP003", "page": 7, "verdict": "review", "overlap_set": 0.8,
        "kind": "body", "mineru_text": "A B", "paddle_text": "A  B",
        "reason": "两通道重合度 0.80 < 0.9 → 未做字符级仲裁（仅 MinerU 结果）"}])
    assert items[0]["mineru"]["text"] == "A B"
    assert "0.80" in items[0]["ai"]["reason"]


def test_skip_items_are_blocking():
    skips = [{"para_id": "RP004", "r": _R("RP004"), "chunk": "of", "count": 3}]
    items = _build_skip_items(skips, {"RP004": 5})
    assert items[0]["blocking"] is True, "有冲突没修 → 必须待确认"
    assert items[0]["item_kind"] == "skip_ambiguous"
    assert items[0]["mineru"]["text"] == "of"
    assert "3 次" in items[0]["ai"]["reason"]


def test_conflict_items_are_blocking_and_tagged():
    class _A:
        id = 0
        verdict = "unresolved"
        reason = "类外歧义"
        confidence = 0.0

    cands = [{"para_id": "RP005", "r": _R("RP005"), "pending": [_A()]}]
    by_para = {"RP005": [{"mineru": {"text": "m"}, "paddleocr": {"text": "p"}}]}
    items = _build_review_items(cands, by_para, {"RP005": 2})
    assert items[0]["blocking"] is True
    assert items[0]["item_kind"] == "conflict"


def test_domain_item_semantics():
    """词典层项语义（2026-09-16 按用户"人不参与"调整）：
    未落地的（applied=False，电荷不匹配的化学式假设）→ **非阻断提示**；
    已自动落地的（applied=True）→ 由调用方标 `auto_resolved`，同样不算待处理。"""
    it = _build_domain_review_item(_R("RP006"), "BF-", "BF4-", [{"confidence": 0.95,
                                                                "rule": "charge"}],
                                   applied=False, page=9)
    assert it["blocking"] is False, "仅提示：不门控翻译、不需人点"
    assert it["item_kind"] == "domain"
    it2 = _build_domain_review_item(_R("RP006"), "BF-", "BF4-", [{"confidence": 0.95,
                                                                 "rule": "charge"}],
                                    applied=True, page=9)
    assert it2["blocking"] is True and it2["ai"]["applied"] is True


def test_rule_audit_items_are_blocking():
    """规则已落地 P，但 PDF 文本层支持 M（S2 实测发现 7 条/2 篇）→ 必须进复核（blocking）。"""
    from paperparse.core.p14_pipeline import _build_rule_audit_items

    class _A:
        verdict = "paddleocr"
        confidence = 1.0

    rows = [{"r": _R("RP007"), "a": _A(),
             "c": {"mineru": {"text": r"$\mathrm{EMIM-BF}_4$"},
                   "paddleocr": {"text": "EMM-BF₄"},
                   "third_vote": {"verdict": "mineru", "decisive": True,
                                  "reason": "文本层逐字命中：M✓ P✗"}},
             "page": 3, "local_text": "EMIM-BF4 was used"}]
    items = _build_rule_audit_items(rows, {"RP007": 3})
    it = items[0]
    assert it["blocking"] is True and it["item_kind"] == "rule_audit"
    assert it["ai"]["applied"] is True
    assert "文本层支持 MinerU" in it["ai"]["reason"]
    assert "恢复原文本" in it["ai"]["reason"]
    assert it["evidence"]["local_text"].startswith("EMIM-BF4")
    assert it["third_vote"]["verdict"] == "mineru"


def test_rule_audit_condition_in_source():
    """源码级守卫：只有"规则已采纳 P（conf≥0.8）且第三信号决定性支持 M"才建审计项。"""
    src = P14.read_text(encoding="utf-8")
    assert 'rule_audit_rows.append' in src
    assert '_tv.get("verdict") == "mineru"' in src
    assert 'blocking_count = len(review_items) + len(skip_items) + len(audit_items)' in src


def test_neither_verdict_enters_pending():
    """源码级守卫：`neither` 必须与 unresolved 一起进待确认（曾静默吞掉）。"""
    src = P14.read_text(encoding="utf-8")
    assert 'a.verdict in ("unresolved", "neither")' in src, \
        "neither（AI 判两侧都错）必须进复核清单"


def test_review_count_means_blocking_only():
    """源码级守卫：`count` 必须是"**真正还需要人处理**"的项数——
    blocking 且未 auto_resolved 且未落地（AI/第三信号替人决定的项都不算）。"""
    src = P14.read_text(encoding="utf-8")
    assert 'review["count"] = sum(' in src
    assert 'and not it.get("auto_resolved")' in src
    assert 'and not (it.get("ai") or {}).get("applied")' in src


# ---------------------------------------------------------------- 自动化护栏（2026-09-16）
# 用户要求"尽量降低人的参与或人不参与" ⇒ AI/第三信号替人决定后**自动落地**。
# 落地必须带结构护栏（实测事故：替换吃掉 `</sup>` 的 `>`，正文插进标签里）。

def test_protected_span_guard_rejects_partial_cut():
    from paperparse.core.p14_pipeline import _crosses_protected, _protected_spans

    text = "and BF<sub>4</sub><sup>−</sup> exist as a salt with $x_1$ rest"
    spans = _protected_spans(text)
    # 切到标签一半（起点在 `</sup>` 内部）→ 必须拒绝
    i = text.find("</sup>") + 1
    assert _crosses_protected(text, i, i + 10, spans) is True
    # 完整包住整个 `$x_1$` → 合法（P16 公式采纳）
    a, b = text.find("$"), text.find("$", text.find("$") + 1) + 1
    assert _crosses_protected(text, a, b, spans) is False
    # 插入点落在标签内部 → 拒绝
    assert _crosses_protected(text, text.find("<sub>") + 2, text.find("<sub>") + 2, spans) is True


def test_structure_ok_detects_tag_breakage():
    from paperparse.core.p14_pipeline import _structure_ok

    ok = "BF<sub>4</sub><sup>−</sup> exist"
    broken = "BF<sub>4</sub><sup>−</supregnated with IL"
    assert _structure_ok(ok, ok.replace("exist", "exists")) is True
    assert _structure_ok(ok, broken) is False          # 标签开闭差被改变
    assert _structure_ok("see $x$ here", "see $x here") is False   # $ 闭合性被改变


def test_apply_arbitrations_refuses_tag_cutting_replace():
    """端到端：`_apply_arbitrations` 遇到"片段跨越标签边界"的冲突 → 不落地（skip_protected_span）。"""
    from paperparse.core.p14_pipeline import _apply_arbitrations

    class _A:
        def __init__(self, i, verdict="paddleocr", conf=1.0):
            self.id, self.verdict, self.confidence, self.reason = i, verdict, conf, ""

    text = "and BF<sub>4</sub><sup>−</sup> exist as a salt"
    conflicts = [{"mineru": {"text": "> exist as a "},
                  "paddleocr": {"text": "regnated with IL at low tem"}}]
    out, audit = _apply_arbitrations(text, conflicts, [_A(0)], apply_min_conf=0.0)
    assert out == text, "跨标签边界的替换必须被拒绝"
    assert audit and audit[0]["action"] == "skip_protected_span"


def test_shape_guard_blocks_content_loss():
    """形状守卫：替换会删实质内容/用截断片段 → 不落地（实测：`$E_{\\mathsf{F}}$ stands for `
    被换成 `E_` ⇒ 正文丢 "stands for"、公式断开）。"""
    from paperparse.core.p14_pipeline import _replacement_shape_ok

    assert _replacement_shape_ok(r"$E _ { \mathsf { F } }$ stands for ", "E_") is False
    assert _replacement_shape_ok(r"$1 5 0 ^ { \circ } \mathrm { C }$", "150 °C") is True
    assert _replacement_shape_ok(r"$480 ~ { \mathrm { m } }$", "480 µm") is True
    assert _replacement_shape_ok("CoO @LIG", "CoO_x@LIG") is True
    assert _replacement_shape_ok("conventional", "conventio") is False   # 明显截断


def test_apply_arbitrations_skips_content_loss():
    from paperparse.core.p14_pipeline import _apply_arbitrations

    class _A:
        id = 0
        verdict = "paddleocr"
        confidence = 1.0
        reason = "AI 判 P"

    text = "potential, $E _ { \\mathsf { F } }$ stands for Fermi level, next"
    conflicts = [{"mineru": {"text": r"$E _ { \mathsf { F } }$ stands for "},
                  "paddleocr": {"text": "E_"}}]
    out, audit = _apply_arbitrations(text, conflicts, [_A()], apply_min_conf=0.0)
    assert out == text, "会删内容的替换必须被拒绝"
    assert any(a["action"] == "skip_content_loss" for a in audit)
    """AI/第三信号替人做的决定 → `auto_resolved` 非空、blocking=False（不算待处理）。"""
    from paperparse.core.p14_pipeline import _build_decided_items

    class _A:
        verdict = "paddleocr"
        confidence = 0.95
        reason = "P 有下标"

    rows = [{"a": _A(), "c": {"mineru": {"text": "CoO @LIG"},
                              "paddleocr": {"text": "CoO_x@LIG"},
                              "third_vote": {"verdict": "paddleocr"}},
             "src": "ai"}]
    it = _build_decided_items(rows, _R("RP010"), 5, "CoOx at page")[0]
    assert it["item_kind"] == "ai_decision"
    assert it["auto_resolved"] == "ai"
    assert it["blocking"] is False
    assert "AI 仲裁 替人选：PaddleOCR" in it["ai"]["reason"]
    assert it["ai"]["applied"] is True


def test_mining_uses_global_index_snapshot():
    """M8c 规则挖掘必须用**全局索引快照**（曾传未定义名 `arb` → 永远抛 NameError 被吞）。"""
    tree = ast.parse(P14.read_text(encoding="utf-8"))
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "mine_char_rules":
            found = True
            args = ast.unparse(node.args[1]) if len(node.args) > 1 else ""
            assert "arb_snapshot" in args, f"第二参数必须是 arb_snapshot，实际 {args!r}"
    assert found, "未找到 mine_char_rules 调用点"
