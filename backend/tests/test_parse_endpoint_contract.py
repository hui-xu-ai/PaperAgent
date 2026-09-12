# -*- coding: utf-8 -*-
"""批2：解析端点契约回归（前端实际提交体 → 后端必须全部接住）。

背景：2026-09-12 批1 的教训就是"前端提交了、后端 pydantic 静默丢弃"（3 个字段）。
批2 新增 `mineru_params`（含嵌套 `mineru_api_key`）与 `paddleocr.options`，本文件用
**前端真实 payload 形状**打端点函数（不经 HTTP，直接调用端点，避免起服务）。
"""
from __future__ import annotations


class _FakeSvc:
    """记录调用参数的 SettingsService 替身。"""

    def __init__(self):
        self.saved_parse = None
        self.saved_mineru = None

    def save_parse(self, cfg):
        self.saved_parse = cfg
        return {"readback": {"ok": True, "mismatch": [], "values": {}},
                "mineru_params": cfg.get("mineru_params") or {},
                "paddleocr_options": (cfg.get("paddleocr") or {}).get("options") or {}}

    def save_mineru(self, cfg):
        self.saved_mineru = cfg

    def get_parse(self):
        return {"mode": "dual"}


class _FakeBus:
    def publish(self, *a, **k):
        return None


class _FakeContainer:
    def __init__(self, svc):
        self._svc = svc

    def get_settings_service(self):
        return self._svc

    def get_event_bus(self):
        return _FakeBus()


def _patch(monkeypatch, svc):
    from app.api import settings as api_settings
    monkeypatch.setattr(api_settings, "container", _FakeContainer(svc))


# 前端 app.js saveParse() 的真实请求体形状（字段名以那里为准）
FRONTEND_PAYLOAD = {
    "mode": "dual",
    "ai_review": True,
    "translate_gate": "wait",
    "skip_review_batch": False,
    "parse_interval_sec": 8,
    "mineru_params": {"mineru_api_key": "sk-from-frontend",
                      "language": "ch", "is_ocr": "on", "enable_table": False},
    "paddleocr": {"access_token": "tok", "base_url": "https://po", "model_version": "v1",
                  "options": {"restructurePages": False, "mergeTables": True,
                              "relevelTitles": True}},
}


def test_parse_endpoint_accepts_frontend_payload(monkeypatch):
    from app.api.settings import ParseModel, save_parse
    svc = _FakeSvc()
    _patch(monkeypatch, svc)
    out = save_parse(ParseModel(**FRONTEND_PAYLOAD))
    assert out["ok"] is True
    # 参数完整送达服务层（除 Key；Key 走 save_mineru）
    assert svc.saved_parse["mineru_params"] == {"language": "ch", "is_ocr": "on",
                                                "enable_table": False}
    assert svc.saved_parse["paddleocr"]["options"]["restructurePages"] is False
    # ★ 关键：嵌套的 Key 必须被接住（否则用户在解析 tab 填了 Key 却不生效）
    assert svc.saved_mineru == {"api_key": "sk-from-frontend", "parser": "auto"}


def test_parse_endpoint_top_level_key_still_supported(monkeypatch):
    """顶层 `mineru_api_key` 写法（脚本/agent 调用）同样生效。"""
    from app.api.settings import ParseModel, save_parse
    svc = _FakeSvc()
    _patch(monkeypatch, svc)
    payload = dict(FRONTEND_PAYLOAD)
    payload.pop("mineru_params")
    payload["mineru_api_key"] = "sk-top-level"
    save_parse(ParseModel(**payload))
    assert svc.saved_mineru == {"api_key": "sk-top-level", "parser": "auto"}


def test_parse_endpoint_no_key_field_keeps_existing(monkeypatch):
    """两种写法都没给 Key ⇒ **不动** .env 里的 Key（不清空）。"""
    from app.api.settings import ParseModel, save_parse
    svc = _FakeSvc()
    _patch(monkeypatch, svc)
    payload = dict(FRONTEND_PAYLOAD)
    payload["mineru_params"] = {"language": "en"}
    save_parse(ParseModel(**payload))
    assert svc.saved_mineru is None
    assert svc.saved_parse["mineru_params"] == {"language": "en"}


def test_parse_model_declares_all_frontend_fields():
    """契约守卫：前端提交体的每个键都必须在 ParseModel 上显式声明（批1 的坑）。"""
    from app.api.settings import ParseModel
    declared = set(ParseModel().model_dump())
    missing = [k for k in FRONTEND_PAYLOAD if k not in declared]
    assert not missing, f"ParseModel 未声明前端字段 {missing}（会被 pydantic 静默丢弃）"
