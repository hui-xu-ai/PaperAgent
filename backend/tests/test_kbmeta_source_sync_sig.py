# -*- coding: utf-8 -*-
"""回归：`KbMetaService.source_sync` 必须把 force 作为**关键字**传给 paperkb 门面。

既有 bug（2026-09-12 实测发现，非本轮引入）：
`kbmeta_service.source_sync` 写成 `kbapi.sync_source_to_kb(doi, self._roots, force=force)`，
而 paperkb 门面的签名早已是 `sync_source_to_kb(doi, force=False)`（roots 由内部 store 提供）
→ 多传的位置参数被绑到 `force`，与 `force=force` 冲突 →
**`POST /api/kb-meta/source/sync`（前端「纳入kb」/「重新同步」按钮）恒返回 400**
`got multiple values for argument 'force'`。
既有测试全绿是因为它们都用 fake 替身（`source_sync(self, doi, force)`），没走真门面签名。

本测试用一个**与真门面同签名**的替身，所以旧写法会直接 TypeError。
"""
from __future__ import annotations

import pytest

from app.services import kbmeta_service as m


class FakeApiWithRealSignature:
    """签名与 `paperkb.api.sync_source_to_kb(doi, force=False)` 一致。"""

    def __init__(self):
        self.calls: list[dict] = []

    def sync_source_to_kb(self, doi, force=False):
        self.calls.append({"doi": doi, "force": force})
        return {"copied": ["source.pdf"], "skipped": []}


@pytest.fixture()
def svc(monkeypatch):
    fake = FakeApiWithRealSignature()
    monkeypatch.setattr(m, "kbapi", fake)
    monkeypatch.setattr(m.KbMetaService, "_ensure", lambda self: None)
    # 绕开 __init__（它会连真实 paperkb 单例）
    return m.KbMetaService.__new__(m.KbMetaService), fake


def test_source_sync_passes_force_as_keyword(svc):
    service, fake = svc
    service.source_sync("10.1002/adma.202407106", force=True)
    assert fake.calls == [{"doi": "10.1002/adma.202407106", "force": True}]


def test_source_sync_default_force_false(svc):
    """默认冻结语义：不传 force → False（只补缺、不覆盖）。"""
    service, fake = svc
    service.source_sync("10.1/x")
    assert fake.calls == [{"doi": "10.1/x", "force": False}]
