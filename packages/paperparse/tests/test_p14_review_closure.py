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


def test_domain_item_tagged():
    it = _build_domain_review_item(_R("RP006"), "BF-", "BF4-", [{"confidence": 0.95,
                                                                "rule": "charge"}],
                                   applied=False, page=9)
    assert it["blocking"] is True
    assert it["item_kind"] == "domain"


def test_neither_verdict_enters_pending():
    """源码级守卫：`neither` 必须与 unresolved 一起进待确认（曾静默吞掉）。"""
    src = P14.read_text(encoding="utf-8")
    assert 'a.verdict in ("unresolved", "neither")' in src, \
        "neither（AI 判两侧都错）必须进复核清单"


def test_review_count_means_blocking_only():
    """源码级守卫：合并领域项后 `count` 必须只数 blocking（否则质量提示会卡住翻译）。"""
    src = P14.read_text(encoding="utf-8")
    assert 'review["count"] = sum(1 for it in review["items"] if it.get("blocking") is not False)' in src


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
