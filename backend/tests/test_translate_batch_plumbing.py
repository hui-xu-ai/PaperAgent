# -*- coding: utf-8 -*-
"""翻译批次上限的**传递链**（2026-09-22 用户要求可调）。

界面上的「翻译批次上限」必须真的走到 paperkb 的分批算法里，否则旋钮是假的。
链路：设置中心 → SettingsService.get_translate_batch_chars()
      → kbmeta_service.translate_now() → paperkb.api.translate_paper(batch_chars=)
      → translate.run_translate(max_body_chars=) → _make_batches(max_body=)
"""
from __future__ import annotations


class _Settings:
    def __init__(self, chars: int):
        self._chars = chars

    def get_translate_batch_chars(self) -> int:
        return self._chars


def _svc(monkeypatch, chars: int):
    from app.services import container
    from app.services import kbmeta_service as ks
    from app.services import llm_service

    monkeypatch.setattr(container, "get_settings_service", lambda: _Settings(chars))
    monkeypatch.setattr(llm_service, "get_translation_ai", lambda: None)  # 主模型路径
    monkeypatch.setattr(ks.kbapi, "configure_llm", lambda c: None)
    svc = ks.KbMetaService()
    svc._ready = True          # 跳过真实 init_kb（本测试只验证参数转发）
    return ks, svc


def test_translate_now_forwards_batch_chars(monkeypatch):
    """设置里填 8000 ⇒ 必须传到 translate_paper(batch_chars=8000)。"""
    ks, svc = _svc(monkeypatch, 8000)
    seen: dict = {}
    monkeypatch.setattr(ks.kbapi, "translate_paper",
                        lambda doc, *, compact, batch_chars=None: seen.update(
                            doc=doc, compact=compact, batch_chars=batch_chars) or {"ok": True})
    svc.translate_now("X.json")
    assert seen["batch_chars"] == 8000, seen
    assert seen["compact"] is False, "无翻译专用 AI ⇒ 主模型路径"


def test_translate_now_passes_zero_when_unset(monkeypatch):
    """没设（0）⇒ 传 0，让 paperkb 用默认（紧凑 6000 / 主模型 12000）。"""
    ks, svc = _svc(monkeypatch, 0)
    seen: dict = {}
    monkeypatch.setattr(ks.kbapi, "translate_paper",
                        lambda doc, *, compact, batch_chars=None: seen.update(
                            batch_chars=batch_chars) or {"ok": True})
    svc.translate_now("X.json")
    assert seen["batch_chars"] == 0, seen


def test_setting_read_failure_does_not_block_translate(monkeypatch):
    """读设置炸了不能挡住翻译 —— 回落默认值并留日志。"""
    from app.services import container
    from app.services import kbmeta_service as ks
    from app.services import llm_service

    def _boom():
        raise RuntimeError("db 挂了")

    monkeypatch.setattr(container, "get_settings_service", lambda: type(
        "S", (), {"get_translate_batch_chars": staticmethod(_boom)})())
    monkeypatch.setattr(llm_service, "get_translation_ai", lambda: None)
    monkeypatch.setattr(ks.kbapi, "configure_llm", lambda c: None)
    seen: dict = {}
    monkeypatch.setattr(ks.kbapi, "translate_paper",
                        lambda doc, *, compact, batch_chars=None: seen.update(
                            batch_chars=batch_chars) or {"ok": True})
    svc = ks.KbMetaService()
    svc._ready = True
    svc.translate_now("X.json")
    assert seen["batch_chars"] == 0, "读设置失败应回落默认（0），而不是中断翻译"
