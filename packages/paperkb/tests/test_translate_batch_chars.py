# -*- coding: utf-8 -*-
"""翻译批次上限（2026-09-22 用户要求可调）：语义硬约束 + 超限日志。

用户要求原话："翻译批次可以设置成可调整的，但是要注意不能破坏语义结构，不能分割句子，
尽量整段发送，为了方便用户调整批次，一旦出现单批字数过多，出现报错，应该有日志输出。"

本文件锁定三件事：
1. 上限可调：`_make_batches(max_body=N)` 按 N 装批；None ⇒ 用默认常量。
2. **语义不破**：只在段边界切批 —— 任何段落要么整段在某批里，要么独自成批，绝不半段入批。
3. 超限有日志（带数字与建议值），用户据此调批次。
"""
from __future__ import annotations

import json
import logging

import pytest

from paperkb.llm import FakeLLM
from paperkb.translate import pipeline
from paperkb.translate.pipeline import (
    COMPACT_MAX_BODY_CHARS,
    MAX_BODY_CHARS,
    _make_batches,
    _run_batches,
)


def _paras(texts: list[str]) -> list[dict]:
    return [{"para_id": f"P{i:03d}", "text_en": t, "text_zh": ""}
            for i, t in enumerate(texts, 1)]


def _chars(paras, batch):
    return sum(len(paras[i]["text_en"]) for i in batch)


# ---------------------------------------------------------------- 上限可调
def test_default_limit_used_when_none():
    """max_body=None ⇒ 沿用旧常量（主模型 12000 / 紧凑 6000），默认行为不变。"""
    paras = _paras(["a" * 7000, "b" * 7000])
    normal, _ = _make_batches(paras, [0, 1], compact=False)
    assert len(normal) == 2, "两个 7000 字符段 > 12000 装不下一批"
    compact, _ = _make_batches(paras, [0, 1], compact=True)
    assert len(compact) == 2, "紧凑默认 6000 同样装不下"
    assert MAX_BODY_CHARS == 12000 and COMPACT_MAX_BODY_CHARS == 6000


def test_custom_limit_packs_more_into_one_batch():
    """上限调大 ⇒ 同一批能装更多字符（用户想要的效果）。"""
    paras = _paras(["a" * 4000, "b" * 4000, "c" * 4000])
    small, _ = _make_batches(paras, [0, 1, 2], max_body=5000)
    big, _ = _make_batches(paras, [0, 1, 2], max_body=20000)
    assert len(small) == 3 and len(big) == 1
    assert _chars(paras, big[0]) == 12000


def test_batches_never_exceed_limit_unless_single_para():
    """除"单段自身超限"这一合法例外，任何批都不超过上限。"""
    paras = _paras(["a" * 100, "b" * 100, "c" * 100, "d" * 100])
    batches, _ = _make_batches(paras, [0, 1, 2, 3], max_body=250)
    for b in batches:
        assert _chars(paras, b) <= 250, f"批超限: {_chars(paras, b)}"


# ---------------------------------------------------------------- 语义不破（硬约束）
def test_oversized_single_para_occupies_its_own_batch_intact():
    """单段超上限 ⇒ **整段独占一批**（尽量整段发送），绝不切成两半。"""
    paras = _paras(["s" * 300, "x" * 50])
    batches, _ = _make_batches(paras, [0, 1], max_body=100)
    big = [b for b in batches if 0 in b]
    assert big == [[0]], f"超长段必须独占一批，实际 {batches}"
    assert _chars(paras, big[0]) == 300, "整段发出（300 字符），不是 100"


def test_no_paragraph_is_split_or_lost():
    """所有目标段恰好出现一次、顺序不变、每批由**完整段**组成。"""
    paras = _paras(["a" * 30, "b" * 120, "c" * 45, "d" * 200, "e" * 10])
    targets = [0, 1, 2, 3, 4]
    for limit in (50, 100, 250, 1000):
        batches, _ = _make_batches(paras, targets, max_body=limit)
        flat = [i for b in batches for i in b]
        assert flat == targets, f"上限 {limit}: 段顺序/成员变了 {flat}"
        # 每批的字符数 == 成员段完整长度之和（说明没有半段入批）
        for b in batches:
            assert _chars(paras, b) == sum(len(paras[i]["text_en"]) for i in b)


def test_oversized_para_logs_actionable_warning(caplog):
    """单段超上限要留日志（带具体数字与建议值），否则用户不知道该调什么。"""
    paras = _paras(["s" * 8321])
    with caplog.at_level(logging.WARNING, logger=pipeline.logger.name):
        _make_batches(paras, [0], max_body=6000)
    assert any("单段 8321 字符 > 上限 6000" in r.getMessage() for r in caplog.records), \
        [r.getMessage() for r in caplog.records]


# ---------------------------------------------------------------- stats / 截断告警
def _translated_json(paras, batch) -> str:
    return json.dumps({"translations": [
        {"para_id": paras[i]["para_id"], "zh": "译" + paras[i]["para_id"]}
        for i in batch]})


def test_run_batches_reports_stats_and_limit():
    """stats 要能上报"用了多少上限、最大批多大、截断了几批"，供上层汇总。"""
    paras = _paras(["a" * 100, "b" * 100, "c" * 100])
    llm = FakeLLM([_translated_json(paras, [0, 1]), _translated_json(paras, [2])])
    (tr, _rej), _t, stats = _run_batches(
        llm, "", paras, [0, 1, 2], {p["para_id"]: i for i, p in enumerate(paras)},
        [], "translate", [0], compact=False, max_body=250)
    assert tr == 3
    assert stats["batch_limit"] == 250
    assert stats["max_batch_chars"] == 200      # 首批两段 = 200 字符
    assert stats["truncated_batches"] == 0


def test_truncated_batch_warns_then_repairs_single(caplog):
    """批截断：先告警（带本批字符数 + 建议值），再按单段补跑保证不漏译。"""
    paras = _paras(["a" * 100, "b" * 100, "c" * 100])
    # 首批只译出第 0 段（模拟输出触顶）→ 第 1 段进单段补跑
    llm = FakeLLM([_translated_json(paras, [0]),
                   _translated_json(paras, [1]),
                   _translated_json(paras, [2])])
    with caplog.at_level(logging.WARNING, logger=pipeline.logger.name):
        (tr, _rej), _t, stats = _run_batches(
            llm, "", paras, [0, 1, 2], {p["para_id"]: i for i, p in enumerate(paras)},
            [], "translate", [0], compact=False, max_body=250)
    assert tr == 3, "补跑后三段都应有译文（不漏译）"
    assert stats["truncated_batches"] == 1
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "批截断" in msg and "本批 200 字符，上限 250" in msg and "≤100" in msg, msg
