# -*- coding: utf-8 -*-
"""检索评测指标单测（tools/retrieval_eval.py 的评分口径）。

为什么要锁（2026-09-21）：指标层出过一次真错——同一篇文献被 DOI/RID/目录名
几种键写法重复召回时，按"别名命中"计分会把一篇算成两篇，nDCG 算出 **1.22 > 1**。
回归门（阈值不过退出码 1）的结论全靠这几个函数，口径错了门就是废的。
"""
from __future__ import annotations

import importlib.util
import math
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "retrieval_eval", Path(__file__).resolve().parents[3] / "tools" / "retrieval_eval.py")
assert _SPEC and _SPEC.loader
ev = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ev)


def _g(*keys: str) -> set[str]:
    return set(keys)


class TestMetrics:
    def test_alias_double_count_not_scored_twice(self):
        """同一篇（两种键写法）召回两次 → 只算一次命中，nDCG 不得 > 1。"""
        groups = [_g("10.1/a", "10.1_a")]
        ranked = [_g("10.1_a"), _g("10.1/a")]
        hits = ev.rank_hits(ranked, groups)
        assert hits == [1]
        assert ev.ndcg_at_k(hits, len(groups)) == pytest.approx(1.0)
        assert ev.recall_at_k(hits, len(groups), 10) == 1.0

    def test_one_recall_credits_one_document(self):
        """一条召回结果只归一篇文献（别名重叠时按 groups 顺序取第一个）。"""
        groups = [_g("10.1/a"), _g("10.1/a", "10.1/b")]
        hits = ev.rank_hits([_g("10.1/a", "10.1/b")], groups)
        assert hits == [1]

    def test_recall_and_mrr_from_ranks(self):
        groups = [_g("a"), _g("b"), _g("c")]
        ranked = [_g("x"), _g("b"), _g("y"), _g("a")]
        hits = ev.rank_hits(ranked, groups)
        assert hits == [2, 4]
        assert ev.recall_at_k(hits, len(groups), 10) == pytest.approx(2 / 3)
        assert ev.recall_at_k(hits, len(groups), 2) == pytest.approx(1 / 3)
        assert ev.mrr(hits) == 0.5
        dcg = 1 / math.log2(3) + 1 / math.log2(5)            # 排名 2、4 的二元增益
        idcg = 1 + 1 / math.log2(3) + 1 / math.log2(4)       # 理想：前三名全中
        assert ev.ndcg_at_k(hits, len(groups), 10) == pytest.approx(dcg / idcg)

    def test_no_hit_scores_zero(self):
        assert ev.rank_hits([_g("x")], [_g("a")]) == []
        assert ev.mrr([]) == 0.0
        assert ev.recall_at_k([], 1, 10) == 0.0
        assert ev.ndcg_at_k([], 1, 10) == 0.0

    def test_ndcg_never_exceeds_one(self):
        groups = [_g("a"), _g("b")]
        ranked = [_g("a"), _g("b", "a"), _g("b")]
        assert ev.ndcg_at_k(ev.rank_hits(ranked, groups), 2, 10) <= 1.0
