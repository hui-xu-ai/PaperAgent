# -*- coding: utf-8 -*-
"""批次上限「安全上限」探测服务（2026-09-22 用户要求：点一下自动测、按安全系数给建议值）。

锁四件事：
1. **选模型顺序**：表单草稿（正在编辑的那条）→ 激活的翻译专用模型 → 主模型 → 都没有则明确报错；
2. **后台线程 + 进度**：start() 立刻返回、不阻塞请求；进度可轮询；
3. **不写任何设置**：结果只回给前端（前端负责回填表单输入框）；
4. **不落盘**（2026-09-23 简化）：值本身存在模型条目里，不再有"上次建议值"这类全局状态。
"""
from __future__ import annotations

import time

from app.services import container
from app.services import llm_service
from app.services.translate_probe_service import TranslateProbeService


class _Settings:
    def __init__(self, pool=None, main=None):
        self._pool, self._main = pool or [], main

    def get_enabled_translation_providers(self, masked: bool = True):
        return list(self._pool)

    def get_active_provider(self, masked: bool = True):
        return self._main

    def save_translate_probe_result(self, result: dict) -> None:
        raise AssertionError("2026-09-23 起探测结果不再落盘")


class _FakeAI:
    model = "fake-model"
    max_tokens = 64000
    provider_name = "假供应商"


def _wire(monkeypatch, settings, kbapi_result=None, kbapi_error=None):
    """把 container / llm_service / kbapi 三处外部依赖换成假的。"""
    monkeypatch.setattr(container, "get_settings_service", lambda: settings)
    monkeypatch.setattr(llm_service, "build_ai", lambda p, g, **kw: _FakeAI())
    monkeypatch.setattr(container, "get_guard", lambda: None)

    class _Kb:
        def probe_translate_batch(self, llm, progress_cb=None, tier_timeout=None):
            if kbapi_error:
                raise kbapi_error
            # 探测必须与生产读超时同口径（2026-09-23）：服务层把 TRANSLATE_TIMEOUT_SEC 传下来
            assert tier_timeout == llm_service.TRANSLATE_TIMEOUT_SEC, tier_timeout
            if progress_cb:
                progress_cb(1, 5, "正在测 3000 字符档（3 段）…")
            return dict(kbapi_result or {"model": llm.model, "recommended": 3600,
                                        "highest_pass": 6000, "tested": []})

    monkeypatch.setattr(container, "get_kbapi", lambda: _Kb())
    return None


def _wait(svc, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = svc.progress()
        if st["status"] in ("done", "error"):
            return st
        time.sleep(0.02)
    raise AssertionError("探测未在 %.1fs 内结束：%s" % (timeout, svc.progress()))


# ---------------------------------------------------------------- 选模型
def test_prefers_active_translation_model(monkeypatch):
    s = _Settings(pool=[{"id": "t1", "name": "翻译专用", "api_key": "k",
                         "base_url": "u", "model": "m"}],
                  main={"id": "main", "api_key": "k2", "base_url": "u2", "model": "m2"})
    _wire(monkeypatch, s)
    svc = TranslateProbeService()
    svc._run()
    st = svc.progress()
    assert st["status"] == "done" and st["via"] == "翻译专用模型"
    assert st["model"] == "fake-model"


def test_falls_back_to_main_model(monkeypatch):
    s = _Settings(pool=[], main={"id": "main", "name": "主", "api_key": "k",
                                "base_url": "u", "model": "m"})
    _wire(monkeypatch, s)
    svc = TranslateProbeService()
    svc._run()
    assert svc.progress()["via"] == "主模型"


def test_no_model_is_a_clear_error(monkeypatch):
    _wire(monkeypatch, _Settings())
    svc = TranslateProbeService()
    svc._run()
    st = svc.progress()
    assert st["status"] == "error"
    assert "没有可用的模型" in st["error"]


# ---------------------------------------------------------------- 后台 + 进度
def test_start_returns_immediately_and_reports_progress(monkeypatch):
    s = _Settings(pool=[{"id": "t1", "name": "翻译专用", "api_key": "k", "base_url": "u",
                         "model": "m"}])
    _wire(monkeypatch, s)
    svc = TranslateProbeService()
    started = svc.start()
    assert started["ok"] is True and started["task_id"]
    st = _wait(svc)
    assert st["status"] == "done" and st["result"]["recommended"] == 3600
    assert st["result"]["via"] == "翻译专用模型"
    assert st["result"]["provider_name"] == "翻译专用"      # 用供应商显示名，不用模型名


def test_draft_provider_from_form_is_used_directly(monkeypatch):
    """表单草稿优先：不必先保存/激活；测的就是你正在编辑的那条模型。"""
    s = _Settings(pool=[{"id": "t1", "name": "池里的", "api_key": "k", "base_url": "u",
                         "model": "m"}],
                  main={"id": "main", "api_key": "k2", "base_url": "u2", "model": "m2"})
    _wire(monkeypatch, s)
    svc = TranslateProbeService()
    started = svc.start({"id": "draft", "name": "正在编辑的", "api_key": "k3",
                         "base_url": "u3", "model": "m3"}, via="当前编辑的模型")
    assert started["ok"] is True
    st = _wait(svc)
    assert st["status"] == "done"
    assert st["via"] == "当前编辑的模型"
    assert st["result"]["provider_name"] == "正在编辑的"


def test_second_start_while_running_is_not_restarted(monkeypatch):
    s = _Settings(pool=[{"id": "t1", "name": "n", "api_key": "k", "base_url": "u",
                         "model": "m"}])
    _wire(monkeypatch, s)
    svc = TranslateProbeService()
    svc._set(status="running", task_id="probe_x")
    again = svc.start()
    assert again["already_running"] is True and again["task_id"] == "probe_x"


def test_probe_failure_is_surfaced_as_error(monkeypatch):
    s = _Settings(pool=[{"id": "t1", "name": "n", "api_key": "k", "base_url": "u",
                         "model": "m"}])
    _wire(monkeypatch, s, kbapi_error=RuntimeError("知识库/解析库里找不到可用的英文正文"))
    svc = TranslateProbeService()
    svc._run()
    st = svc.progress()
    assert st["status"] == "error" and "英文正文" in st["error"]


# ---------------------------------------------------------------- 不写设置、不落盘
def test_probe_never_writes_batch_chars(monkeypatch):
    """探测服务**不得**写批次上限（前端只把建议值回填表单，用户点保存才生效）。"""
    s = _Settings(pool=[{"id": "t1", "name": "n", "api_key": "k", "base_url": "u",
                         "model": "m"}])
    written: list = []
    s.save_translate_batch_chars = lambda v: written.append(v)      # type: ignore[attr-defined]
    _wire(monkeypatch, s)
    svc = TranslateProbeService()
    svc._run()
    assert svc.progress()["status"] == "done"
    assert written == [], "探测只给建议值，绝不自动写入"


# ---------------------------------------------------------------- API 层
def test_get_endpoint_only_reports_progress(monkeypatch):
    """GET 只回进度/结果，不再有"上次落盘建议值"这回显（2026-09-23 简化）。"""
    from app.api import settings as api_settings

    class _Probe:
        @staticmethod
        def progress():
            return {"status": "idle"}

    monkeypatch.setattr(container, "get_translate_probe", lambda: _Probe())
    out = api_settings.get_translate_probe()
    assert out == {"status": "idle"}


def test_probe_endpoint_rejects_incomplete_draft(monkeypatch):
    """草稿缺 Key（且池里也查不到该 id 的已存 Key）⇒ 400 明说，而不是静默回落别人。"""
    from fastapi import HTTPException

    from app.api import settings as api_settings

    class _S:
        @staticmethod
        def get_translation_providers(masked: bool = False):
            return []

    monkeypatch.setattr(container, "get_settings_service", lambda: _S())
    body = api_settings.TranslateProbeModel(id="", name="新模型",
                                            base_url="https://x/v1", model="m", api_key="")
    try:
        api_settings.start_translate_probe(body)
        raise AssertionError("缺 Key 应报 400")
    except HTTPException as e:
        assert e.status_code == 400 and "Base URL" in str(e.detail)


def test_probe_endpoint_fills_safe_max_tokens_for_small_model(monkeypatch):
    """探测请求必须带**与线上同口径**的 max_tokens：缺省会落到 build_ai 的 64000 兜底，
    而 8K 输出的小模型（Qwen2.5-7B）收到 64000 直接被服务端 400 拒 —— 实测用户报的错。
    期望：取该条目已存的 8192（不是 64000）；条目不存在时取"按单批上限推导"的安全值。
    """
    from app.api import settings as api_settings

    seen: dict = {}

    class _S:
        @staticmethod
        def get_translation_providers(masked: bool = False):
            return [{"id": "translate_0", "name": "千问", "api_key": "sk-x",
                     "base_url": "https://api.siliconflow.cn/v1",
                     "model": "Qwen/Qwen2.5-7B-Instruct", "max_tokens": 8192,
                     "batch_chars": 0, "enabled": True}]

        @staticmethod
        def get_effective_translate_batch_chars() -> int:
            return 14000

    class _ProbeSvc:
        @staticmethod
        def start(provider=None, via=""):
            seen["provider"] = provider
            return {"ok": True}

    monkeypatch.setattr(container, "get_settings_service", lambda: _S())
    monkeypatch.setattr(container, "get_translate_probe", lambda: _ProbeSvc())

    api_settings.start_translate_probe(api_settings.TranslateProbeModel(
        id="translate_0", name="千问", base_url="https://api.siliconflow.cn/v1",
        model="Qwen/Qwen2.5-7B-Instruct", api_key="sk-x"))
    assert seen["provider"]["max_tokens"] == 8192, seen["provider"]

    # 池里查不到该 id（新增条目还没保存）⇒ 用"单批上限 × 0.4"（14000×0.4=5600），仍不是 64000
    api_settings.start_translate_probe(api_settings.TranslateProbeModel(
        id="", name="新模型", base_url="https://api.siliconflow.cn/v1",
        model="Qwen/Qwen2.5-7B-Instruct", api_key="sk-x"))
    assert seen["provider"]["max_tokens"] == 5600, seen["provider"]


def test_probe_endpoint_resolves_masked_key_from_pool(monkeypatch):
    """表单里显示的是掩码占位 ⇒ 后端按 id 取池里已存的真实 Key（与「测试连接」同一约定）。"""
    from app.api import settings as api_settings

    seen: dict = {}

    class _S:
        @staticmethod
        def get_translation_providers(masked: bool = False):
            return [{"id": "t1", "name": "千问", "api_key": "sk-real-stored",
                     "base_url": "https://api.siliconflow.cn/v1",
                     "model": "Qwen/Qwen2.5-7B-Instruct", "enabled": True}]

        @staticmethod
        def get_effective_translate_batch_chars() -> int:
            return 14000

    class _ProbeSvc:
        @staticmethod
        def start(provider=None, via=""):
            seen["provider"] = provider
            seen["via"] = via
            return {"ok": True}

    monkeypatch.setattr(container, "get_settings_service", lambda: _S())
    monkeypatch.setattr(container, "get_translate_probe", lambda: _ProbeSvc())
    body = api_settings.TranslateProbeModel(
        id="t1", name="千问", base_url="https://api.siliconflow.cn/v1",
        model="Qwen/Qwen2.5-7B-Instruct", api_key="••••••••")
    api_settings.start_translate_probe(body)
    assert seen["provider"]["api_key"] == "sk-real-stored"
    assert seen["via"] == "当前编辑的模型"


# ---------------------------------------------------------------- 真实门面（不是假 kbapi）
def test_service_works_with_real_kbmeta_probe(monkeypatch):
    """服务层 → **真实 KbMetaService** → paperkb 门面（只替换最外层那个函数）。

    2026-09-23 实测 bug（用户报）：`_wire` 里那个假 kbapi 收了 `tier_timeout`，而真实
    `KbMetaService.probe_translate_batch` 只收 (llm, progress_cb) ⇒ 界面点「自动测一个值」
    直接 `KbMetaService.probe_translate_batch() got an unexpected keyword argument
    'tier_timeout'`。防的就是"**假对象比真实类更宽松**"这种掩盖签名漂移的情形，
    所以本用例故意不放假 kbapi。
    """
    from app.services.kbmeta_service import KbMetaService
    from paperkb import api as kbapi

    s = _Settings(pool=[{"id": "t1", "name": "翻译专用", "api_key": "k",
                         "base_url": "u", "model": "m"}])
    _wire(monkeypatch, s)                       # 只换 container/llm_service 的假件
    monkeypatch.setattr(KbMetaService, "_ensure", lambda self: None)
    monkeypatch.setattr(container, "get_kbapi", lambda: KbMetaService())

    seen: dict = {}

    def fake_probe(llm, progress_cb=None, tier_timeout=None):
        seen["tier_timeout"] = tier_timeout
        return {"model": llm.model, "recommended": 7200, "highest_pass": 12000, "tested": []}

    monkeypatch.setattr(kbapi, "probe_translate_batch", fake_probe)

    svc = TranslateProbeService()
    svc._run()
    st = svc.progress()
    assert st["status"] == "done", st
    # 透传到位：墙钟 = 生产读超时（否则探测会推荐生产必然超时的批次）
    assert seen["tier_timeout"] == llm_service.TRANSLATE_TIMEOUT_SEC
    assert st["result"]["recommended"] == 7200


def test_service_call_shape_binds_to_real_kbmeta_signature():
    """调用形状契约：服务层实际传的 kwarg 必须能 bind 到真实签名上（签名漂移即红）。"""
    import inspect

    from app.services.kbmeta_service import KbMetaService
    from paperkb import api as kbapi

    sig = inspect.signature(KbMetaService.probe_translate_batch)
    sig.bind(object(),                              # self（未绑定方法）
             object(),                              # llm：探测专用实例（此处只验形状）
             tier_timeout=llm_service.TRANSLATE_TIMEOUT_SEC,
             progress_cb=lambda *a: None)           # 与 translate_probe_service 的调用一致

    # 门面层同样要收这两个（两层任一漂移都要红）
    facade = inspect.signature(kbapi.probe_translate_batch)
    assert {"tier_timeout", "progress_cb"} <= set(facade.parameters)
