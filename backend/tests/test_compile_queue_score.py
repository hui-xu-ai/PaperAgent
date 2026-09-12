# -*- coding: utf-8 -*-
"""批5：编译队列「价值分」显示修复的端点级回归测试。

用户实测：编译队列表格「价值分」显示 0.00（而知识库列表/`/api/kb-meta/scores` 里该篇是 3.48）。
根因两处：① `Compiler.queue` 缺省 value_score=0.0（显式 level 入队时从不计算分）；
② 历史行 value_score 已写 0 ⇒ 端点需按当前分兜底填充。
"""
from __future__ import annotations

import pytest

from app.api import kbmeta as kbmeta_api


class _FakeKbApi:
    def __init__(self, rows, scores):
        self._rows = rows
        self._scores = scores
        self.score_calls: list[str] = []

    def compile_status(self, status=None):
        return [dict(r) for r in self._rows]

    def value_score(self, doi):
        self.score_calls.append(doi)
        return self._scores.get(doi)


class _FakeContainer:
    def __init__(self, kbapi):
        self._kbapi = kbapi

    def get_kbapi(self):
        return self._kbapi


def _patch(monkeypatch, kbapi):
    monkeypatch.setattr(kbmeta_api, "container", _FakeContainer(kbapi))


def test_jobs_fills_missing_value_score(monkeypatch):
    """历史行（value_score=0）→ 用当前价值分兜底填充。"""
    kbapi = _FakeKbApi(
        [{"paper_doi": "10.1/a", "level": "L2", "status": "done", "value_score": 0.0},
         {"paper_doi": "10.1/a", "level": "L1", "status": "done", "value_score": 0.0}],
        {"10.1/a": {"score": 3.48, "level": "L2"}})
    _patch(monkeypatch, kbapi)
    rows = kbmeta_api.compile_jobs()
    assert [r["value_score"] for r in rows] == [3.48, 3.48]
    assert kbapi.score_calls == ["10.1/a"], "同一 DOI 只查一次（按 DOI 缓存）"


def test_jobs_keeps_real_score_untouched(monkeypatch):
    """已有真实分的行不得被改写（快照语义保留），也不去查分数。"""
    kbapi = _FakeKbApi(
        [{"paper_doi": "10.1/b", "level": "L3", "status": "queued", "value_score": 4.9}],
        {"10.1/b": {"score": 1.0}})
    _patch(monkeypatch, kbapi)
    rows = kbmeta_api.compile_jobs()
    assert rows[0]["value_score"] == 4.9
    assert kbapi.score_calls == []


def test_jobs_tolerates_score_lookup_failure(monkeypatch):
    """评分失败（无元数据/库损坏）→ 保持 0，不抛错、不阻塞队列返回。"""
    class _Boom(_FakeKbApi):
        def value_score(self, doi):
            raise RuntimeError("journal db missing")

    kbapi = _Boom([{"paper_doi": "nd-x", "level": "L1", "status": "queued",
                    "value_score": 0.0}], {})
    _patch(monkeypatch, kbapi)
    rows = kbmeta_api.compile_jobs()
    assert rows[0]["value_score"] == 0.0
