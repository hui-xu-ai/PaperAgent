# -*- coding: utf-8 -*-
"""设置服务测试（V03）：供应商脱敏 / env 兜底 / 激活。"""
from __future__ import annotations

import re

import pytest

from app.services.settings_service import SettingsService


@pytest.fixture(autouse=True)
def _clean_extra_env(monkeypatch):
    """隔离真实 .env 的 SILICONFLOW_*/CUSTOM_PROVIDER_*（P12F：用户配置后 env presets
    会多出 siliconflow 供应商，干扰本文件按预设数量断言的测试；
    T3：根 .env 的 CUSTOM_PROVIDER_* 自定义供应商同样经 load_dotenv 注入 os.environ）。
    2026-09-21：再隔离 QWEN_*/ZHIPU_*/TRANSLATE_<n>_*——翻译池现在也会从 .env 播种
    （`_env_translation_presets`），不隔离则本机 .env 会漏进池断言。"""
    import os
    for k in ("SILICONFLOW_API_KEY", "SILICONFLOW_BASE_URL", "SILICONFLOW_MODEL",
              "ZHIPU_API_KEY", "ZHIPU_BASE_URL", "ZHIPU_MODEL", "ZHIPU_MAX_TOKENS"):
        monkeypatch.delenv(k, raising=False)
    for k in list(os.environ):
        if k.startswith("CUSTOM_PROVIDER_") or k.startswith("QWEN_") or re.match(
                r"TRANSLATE_\d+_", k):
            monkeypatch.delenv(k, raising=False)


def _make_env_settings():
    """显式官方端点：不依赖运行环境 .env（P12-4 后真实 .env 是魔塔 modelscope）。"""
    from app.config import Settings
    return Settings(deepseek_api_key="sk-real-1234567890abcdef",
                    deepseek_base_url="https://api.deepseek.com",
                    deepseek_model="deepseek-chat")


def test_env_fallback(store):
    svc = SettingsService(store, app_settings=_make_env_settings())
    providers = svc.get_providers(masked=False)
    assert len(providers) == 1
    assert providers[0]["id"] == "deepseek"
    assert providers[0]["api_key"].startswith("sk-")


def test_mask_no_leak(store):
    svc = SettingsService(store, app_settings=_make_env_settings())
    masked = svc.get_providers(masked=True)
    assert "sk-real" not in masked[0]["api_key"]  # 不泄露完整 key
    assert "…" in masked[0]["api_key"]
    raw = svc.get_providers(masked=False)
    assert raw[0]["api_key"] == "sk-real-1234567890abcdef"  # 内部仍完整


def test_save_preserves_key_when_masked(store):
    svc = SettingsService(store, app_settings=_make_env_settings())
    # 前端提交脱敏占位 → 保留原 key
    svc.save_providers([{"id": "deepseek", "name": "DeepSeek 官方",
                         "base_url": "https://api.deepseek.com",
                         "model": "deepseek-chat",
                         "api_key": "sk-rea…cdef"}])
    raw = svc.get_providers(masked=False)
    assert raw[0]["api_key"] == "sk-real-1234567890abcdef"


def test_activate_and_fallback(store):
    svc = SettingsService(store, app_settings=_make_env_settings())
    svc.save_providers([
        {"id": "a", "name": "A", "base_url": "u1", "model": "m1", "api_key": "k1"},
        {"id": "b", "name": "B", "base_url": "u2", "model": "m2", "api_key": "k2"},
    ])
    assert svc.get_active_id() == "a"  # 首激活
    svc.set_active_provider("b")
    assert svc.get_active_id() == "b"
    svc.save_providers([{"id": "a", "name": "A", "base_url": "u1", "model": "m1",
                         "api_key": "k1"}])
    assert svc.get_active_id() == "a"  # active 被删 → 回退第一个


def test_fallback_prefers_provider_with_key(store):
    """P13：active 被删时回退"第一个有 key 的供应商"（无 key 供应商 LLM 不可用，
    回退到它=静默失效——原实现固定回退 cleaned[0] 即魔塔在前）。"""
    svc = SettingsService(store, app_settings=_make_env_settings())
    svc.save_providers([
        {"id": "m", "name": "魔塔", "base_url": "u1", "model": "m1", "api_key": ""},
        {"id": "s", "name": "硅基", "base_url": "u2", "model": "m2", "api_key": "k2"},
        {"id": "t", "name": "第三家", "base_url": "u3", "model": "m3", "api_key": "k3"},
    ])
    svc.set_active_provider("s")
    # active=s 被删 → 回退第一个**有 key** 的（跳过无 key 的 m，选 t 而非 m）
    svc.save_providers([
        {"id": "m", "name": "魔塔", "base_url": "u1", "model": "m1", "api_key": ""},
        {"id": "t", "name": "第三家", "base_url": "u3", "model": "m3", "api_key": "k3"},
    ])
    assert svc.get_active_id() == "t"   # 有 key 优先，不固定回退 [0]（魔塔）


# ---------------------------------------------------------------- P12-4 供应商预填
def test_env_presets_modelscope_and_seed(store):
    """P12-4：.env 为魔塔 → 预填 id=modelscope；种子迁移默认激活魔塔。"""
    from app.config import Settings
    svc = SettingsService(store, app_settings=Settings(
        deepseek_api_key="ms-real-key-1234567890",
        deepseek_base_url="https://api-inference.modelscope.cn/v1",
        deepseek_model="deepseek-ai/DeepSeek-V4-Flash"))
    providers = svc.get_providers(masked=False)
    ids = [p["id"] for p in providers]
    assert "modelscope" in ids
    assert svc.get_active_id() == "modelscope"   # 默认激活魔塔（用户决策）
    # DB 已有供应商时合并（不丢既有）
    svc.save_providers([{"id": "deepseek", "name": "DeepSeek 官方",
                         "base_url": "https://api.deepseek.com",
                         "model": "deepseek-chat", "api_key": "sk-abc"}])
    providers2 = svc.get_providers(masked=False)
    assert {p["id"] for p in providers2} >= {"deepseek", "modelscope"}
    # 用户切走后不再被种子迁移覆盖
    svc.set_active_provider("deepseek")
    assert svc.get_active_id() == "deepseek"
    svc.get_providers(masked=False)
    assert svc.get_active_id() == "deepseek"


def test_env_sync_writes_env(tmp_path, store, monkeypatch):
    """P12-4：env_sync=True 时供应商保存按实际填写结果写回 .env（SILICONFLOW_*）。"""
    from app.config import Settings
    env_path = tmp_path / ".env"
    env_path.write_text("DEEPSEEK_API_KEY=ms-old\nDEEPSEEK_BASE_URL=https://api-inference.modelscope.cn/v1\n"
                        "# SILICONFLOW_API_KEY=commented\n", encoding="utf-8")
    svc = SettingsService(store, app_settings=Settings(deepseek_api_key="ms-x"),
                          env_sync=True, env_path=str(env_path))
    svc.save_providers([
        {"id": "siliconflow", "name": "硅基流动（备用）",
         "base_url": "https://api.siliconflow.cn/v1",
         "model": "deepseek-ai/DeepSeek-V4-Flash",
         "api_key": "sk-silicon-new", "env": "SILICONFLOW"}])
    text = env_path.read_text(encoding="utf-8")
    assert "SILICONFLOW_API_KEY=sk-silicon-new" in text       # 新键写入
    assert "# SILICONFLOW_API_KEY=commented" in text          # 注释行保留
    assert "DEEPSEEK_API_KEY=ms-old" in text                   # 无关键不动


# ---------------------------------------------------------------- P12-5 解析设置
def test_parse_settings_store_overrides_env(store):
    """P12-5：GUI 保存的解析设置优先于 env 默认；ai_review 可关。"""
    from app.config import Settings
    svc = SettingsService(store, app_settings=Settings(
        parse_mode="dual", ai_review=True, deepseek_api_key="k"))
    assert svc.get_parse()["mode"] == "dual"
    assert svc.get_parse()["ai_review"] is True
    # GUI 保存：关掉 AI 仲裁 + 单通道
    svc.save_parse({"mode": "single", "ai_review": False,
                    "paddleocr": {"access_token": "tok", "base_url": "u", "model_version": "m"}})
    p = svc.get_parse()
    assert p["mode"] == "single"
    assert p["ai_review"] is False                     # env 默认不得覆盖 store
    assert p["paddleocr"]["access_token"] == "tok"
    # 再开回来
    svc.save_parse({"mode": "dual", "ai_review": True,
                    "paddleocr": {"access_token": "tok", "base_url": "u", "model_version": "m"}})
    assert svc.get_parse()["ai_review"] is True


# ---------------------------------------------------------------- kb 复制方式（T02）
# 2026-09-12 批1：删除「知识库纳入清单」kb_include / 「AI 检索文件清单」retrieval_include
# （全仓无消费点，勾选框纯装饰，用户拍板删除）——守卫见下方 test_decorative_include_keys_removed。
def test_kb_copy_mode_default_and_save(store):
    svc = SettingsService(store, app_settings=_make_env_settings())
    assert svc.get_kb_copy_mode() == "copy"
    svc.save_kb_copy_mode("link")
    assert svc.get_kb_copy_mode() == "link"


def test_kb_rules_invalid_mode_fallback(store):
    svc = SettingsService(store, app_settings=_make_env_settings())
    svc.save_kb_copy_mode("weird")
    assert svc.get_kb_copy_mode() == "copy"


def test_kb_rules_in_get_all(store):
    svc = SettingsService(store, app_settings=_make_env_settings())
    all_cfg = svc.get_all()
    assert all_cfg["kb_copy_mode"] == "copy"
    assert "kb_include" not in all_cfg and "retrieval_include" not in all_cfg


def test_decorative_include_keys_removed(store):
    """批1 守卫：装饰性纳入/检索清单的读写方法必须已删（防止有人"顺手加回来"）。"""
    svc = SettingsService(store, app_settings=_make_env_settings())
    for gone in ("get_kb_include", "save_kb_include",
                 "get_retrieval_include", "save_retrieval_include"):
        assert not hasattr(svc, gone), f"{gone} 应随装饰勾选框一并删除"


# ---------------------------------------------------------------- 输出模板（T06）
def test_md_template_default_and_save(store):
    svc = SettingsService(store, app_settings=_make_env_settings())
    assert svc.get_md_template() == "obsidian_bilingual"
    svc.save_md_template("plain")
    assert svc.get_md_template() == "plain"
    svc.save_md_template("nope")  # 非法回退
    assert svc.get_md_template() == "obsidian_bilingual"


def test_md_template_in_get_all(store):
    svc = SettingsService(store, app_settings=_make_env_settings())
    assert svc.get_all()["md_template"] == "obsidian_bilingual"


# ---------------------------------------------------------------- AI 检索分级（T05）
def test_retrieval_mode_default_and_save(store):
    svc = SettingsService(store, app_settings=_make_env_settings())
    assert svc.get_retrieval_mode() == "notes"
    svc.save_retrieval_mode("full")
    assert svc.get_retrieval_mode() == "full"
    svc.save_retrieval_mode("nope")  # 非法回退
    assert svc.get_retrieval_mode() == "notes"


# ---------------------------------------------------------------- 单价（T3：供应商-模型组合，M5：default=0）
def test_prices_default_struct(store):
    """未配置 → 返回完整结构（by_provider_model 空 + default 恒 0，M5 取消全局默认）。"""
    svc = SettingsService(store, app_settings=_make_env_settings())
    p = svc.get_prices()
    assert set(p) == {"by_provider_model", "default"}
    assert p["by_provider_model"] == {}
    assert p["default"]["input_per_m"] == 0.0
    assert p["default"]["output_per_m"] == 0.0


def test_prices_legacy_flat_read(store):
    """旧扁平格式 → get_prices 封装为 default（M5：默认价恒 0）。"""
    from app.services.settings_service import KEY_PRICES
    store.set_setting(KEY_PRICES, '{"input_per_m": 2.5, "cached_input_per_m": 0.2, '
                                  '"output_per_m": 6.0}')
    svc = SettingsService(store, app_settings=_make_env_settings())
    p = svc.get_prices()
    assert p["by_provider_model"] == {}
    assert p["default"] == {"input_per_m": 0.0, "cached_input_per_m": 0.0,
                            "output_per_m": 0.0}


def test_prices_for_resolution(store):
    """get_prices_for：by_provider_model 命中 → 用值；未命中/删项 → 回落 0（M5）。"""
    svc = SettingsService(store, app_settings=_make_env_settings())
    svc.save_prices({"provider_id": "siliconflow", "model": "deepseek-ai/DeepSeek-V4-Flash",
                     "input_per_m": 3.0, "cached_input_per_m": 0.1, "output_per_m": 9.0})
    hit = svc.get_prices_for("siliconflow", "deepseek-ai/DeepSeek-V4-Flash")
    assert hit["input_per_m"] == 3.0 and hit["output_per_m"] == 9.0
    fallback = svc.get_prices_for("deepseek", "deepseek-chat")
    assert fallback["input_per_m"] == 0.0 and fallback["output_per_m"] == 0.0
    # 单项全空保存 → 删除该项 → 回落 0
    svc.save_prices({"provider_id": "siliconflow", "model": "deepseek-ai/DeepSeek-V4-Flash",
                     "input_per_m": None, "cached_input_per_m": None, "output_per_m": None})
    assert svc.get_prices_for("siliconflow", "deepseek-ai/DeepSeek-V4-Flash")["input_per_m"] == 0.0


def test_prices_partial_default_update(store):
    """缺字段保存不覆盖未提供字段（M5：default 恒 0，仅 per-model 计价生效）。"""
    svc = SettingsService(store, app_settings=_make_env_settings())
    svc.save_prices({"output_per_m": 7.0})
    p = svc.get_prices()["default"]
    assert p["input_per_m"] == 0.0 and p["output_per_m"] == 0.0  # default 恒 0
    svc.save_prices({"provider_id": "a", "model": "m", "output_per_m": 7.0})
    assert svc.get_prices_for("a", "m")["output_per_m"] == 7.0    # per-model 生效


def test_prices_full_struct_save(store):
    """完整结构保存：per-model 单价生效，default 恒 0（M5）。"""
    svc = SettingsService(store, app_settings=_make_env_settings())
    svc.save_prices({"by_provider_model": {"a::m": {"input_per_m": 1.0,
                                                    "cached_input_per_m": 0.1,
                                                    "output_per_m": 4.0}}})
    p = svc.get_prices()
    assert p["default"]["input_per_m"] == 0.0      # default 恒 0
    assert p["default"]["output_per_m"] == 0.0
    assert p["by_provider_model"] == {"a::m": {"input_per_m": 1.0,
                                               "cached_input_per_m": 0.1,
                                               "output_per_m": 4.0}}
    assert svc.get_prices_for("a", "m")["input_per_m"] == 1.0


# ---------------------------------------------------------------- P：供应商 max_tokens（翻译截断）
def test_provider_max_tokens_default_and_configurable(store):
    """P：供应商 max_tokens 可配置且默认 64000（≥16384 旧基线仍成立）。"""
    from app.services.llm_service import DEFAULT_MAX_OUTPUT_TOKENS
    assert DEFAULT_MAX_OUTPUT_TOKENS == 64000
    assert DEFAULT_MAX_OUTPUT_TOKENS >= 16384
    svc = SettingsService(store, app_settings=_make_env_settings())
    # 默认 env 预填 → max_tokens == 默认 64000
    assert svc.get_providers(masked=False)[0]["max_tokens"] == 64000
    # 可配置：保存自定义值 → 读回
    svc.save_providers([{"id": "a", "name": "A", "base_url": "u", "model": "m",
                         "api_key": "k", "max_tokens": 32000}])
    got = [p for p in svc.get_providers(masked=False) if p["id"] == "a"][0]
    assert got["max_tokens"] == 32000
    # get_all（前端编辑回填读它，掩码版本也带 max_tokens）
    a = [p for p in svc.get_all()["providers"] if p["id"] == "a"][0]
    assert a["max_tokens"] == 32000


def test_save_preserves_key_when_short_masked(store):
    """P：短 key 被 _mask 成 '***'，保存时回传 '***'（未改动）应保留原 key。"""
    svc = SettingsService(store, app_settings=_make_env_settings())
    svc.save_providers([{"id": "a", "name": "A", "base_url": "u", "model": "m",
                         "api_key": "abc123"}])  # 短 key（≤12 → 掩码 '***'）
    svc.save_providers([{"id": "a", "name": "A", "base_url": "u", "model": "m",
                         "api_key": "***"}])     # 前端回传掩码占位（未改动）
    raw = [p for p in svc.get_providers(masked=False) if p["id"] == "a"][0]
    assert raw["api_key"] == "abc123"


# ---------------------------------------------------------------- reasoning_effort（思考强度）
def test_provider_reasoning_effort_default_none(store):
    """内置供应商默认 reasoning_effort=None（走 context 自动映射）。"""
    svc = SettingsService(store, app_settings=_make_env_settings())
    p = svc.get_providers(masked=False)[0]
    assert p.get("reasoning_effort") is None


def test_provider_reasoning_effort_custom_env_roundtrip(tmp_path, store, monkeypatch):
    """供应商 reasoning_effort：DB 保存/读回 + CUSTOM .env 持久化（GLM 等思考型供应商配置）。"""
    from app.config import Settings
    # 清空可能干扰的 CUSTOM_* env 预设
    import os
    for k in list(os.environ):
        if k.startswith("CUSTOM_PROVIDER_"):
            monkeypatch.delenv(k, raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")
    svc = SettingsService(store, app_settings=Settings(deepseek_api_key="k"),
                          env_sync=True, env_path=str(env_path))
    svc.save_providers([{"id": "zhipu", "name": "智谱", "env": "",
                         "base_url": "https://open.bigmodel.cn/api/paas/v4",
                         "model": "glm-5.3", "api_key": "sk-z",
                         "reasoning_effort": "low"}])
    # 读回（内部）：带 reasoning_effort
    got = [p for p in svc.get_providers(masked=False) if p["id"] == "zhipu"][0]
    assert got["reasoning_effort"] == "low"
    # 掩码版本也带（前端编辑回填）
    got_m = [p for p in svc.get_all()["providers"] if p["id"] == "zhipu"][0]
    assert got_m["reasoning_effort"] == "low"
    # .env 持久化 CUSTOM_PROVIDER_<id>_REASONING_EFFORT=low（id 原样 zhipu，前缀为 `CUSTOM_PROVIDER_zhipu_`）
    text = env_path.read_text(encoding="utf-8")
    assert "CUSTOM_PROVIDER_zhipu_REASONING_EFFORT=low" in text


# ============================================================ 批1（2026-09-12）回归
def test_parse_model_keeps_all_frontend_fields(store):
    """批1 硬 bug：ParseModel 必须显式声明前端提交的全部字段。

    pydantic 默认丢弃未声明字段 ⇒ 旧实现漏了 translate_gate/skip_review_batch/
    parse_interval_sec，前端提交后被静默丢弃（svc.save_parse 明明会写这三个值）。
    """
    from app.api.settings import ParseModel
    payload = {"mode": "single", "ai_review": False,
               "translate_gate": "auto", "skip_review_batch": True,
               "parse_interval_sec": 25, "paddleocr": {}}
    dumped = ParseModel(**payload).model_dump()
    for k in payload:
        assert k in dumped, f"ParseModel 丢了字段 {k}（前端提交后会被静默丢弃）"
    svc = SettingsService(store, app_settings=_make_env_settings())
    svc.save_parse(dumped)
    got = svc.get_parse()
    assert got["translate_gate"] == "auto"
    assert got["skip_review_batch"] is True
    assert got["parse_interval_sec"] == 25


def test_parse_model_defaults_match_service_defaults():
    """契约：ParseModel 默认值必须与 get_parse 的默认值一致（前端不提交时行为可预期）。"""
    from app.api.settings import ParseModel
    d = ParseModel().model_dump()
    assert d["translate_gate"] == "wait"
    assert d["skip_review_batch"] is False
    assert d["parse_interval_sec"] == 8


def test_save_mineru_writes_env_and_reads_back(tmp_path, store, monkeypatch):
    """批1 最严重项：MinerU 配置必须写 .env 并回读一致（旧实现只写 SQLite ⇒ UI 改动无效）。"""
    from app.config import Settings, live_mineru_key
    monkeypatch.delenv("MINERU_API_KEY", raising=False)
    monkeypatch.delenv("MINERU_PARSER", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("DEEPSEEK_API_KEY=ms-old\n# MINERU_API_KEY=commented\n",
                        encoding="utf-8")
    svc = SettingsService(store, app_settings=Settings(mineru_api_key=""),
                          env_sync=True, env_path=str(env_path))
    res = svc.save_mineru({"api_key": "sk-mineru-new", "parser": "mineru-v4"})
    assert res["api_key_set"] is True and res["parser"] == "mineru-v4"
    assert res["readback"]["ok"] is True, res["readback"]
    text = env_path.read_text(encoding="utf-8")
    assert "MINERU_API_KEY=sk-mineru-new" in text
    assert "MINERU_PARSER=mineru-v4" in text
    assert "DEEPSEEK_API_KEY=ms-old" in text                  # 其他键不动
    assert "# MINERU_API_KEY=commented" in text               # 注释行不动
    assert live_mineru_key() == "sk-mineru-new"               # 运行中进程即时可见
    assert svc.get_mineru()["api_key"] == "sk-mineru-new"     # 读回一致


def test_save_mineru_empty_key_clears(tmp_path, store, monkeypatch):
    """显式清空 Key（回落免费通道）必须生效，不得回退旧值。"""
    from app.config import Settings, live_mineru_key
    monkeypatch.setenv("MINERU_API_KEY", "sk-old")
    env_path = tmp_path / ".env"
    env_path.write_text("MINERU_API_KEY=sk-old\n", encoding="utf-8")
    svc = SettingsService(store, app_settings=Settings(mineru_api_key="sk-old"),
                          env_sync=True, env_path=str(env_path))
    res = svc.save_mineru({"api_key": "", "parser": "auto"})
    assert res["api_key_set"] is False
    assert live_mineru_key("sk-old") == ""
    assert "MINERU_API_KEY=\n" in env_path.read_text(encoding="utf-8")


def test_save_mineru_rejects_unknown_parser(store):
    svc = SettingsService(store, app_settings=_make_env_settings())
    with pytest.raises(ValueError):
        svc.save_mineru({"api_key": "k", "parser": "nope"})


def test_save_mineru_masked_key_keeps_existing(tmp_path, store, monkeypatch):
    """掩码占位（前端回显脱敏）不得把占位串写进 .env。"""
    from app.config import Settings, live_mineru_key
    monkeypatch.setenv("MINERU_API_KEY", "sk-real-key-1234567890")
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")
    svc = SettingsService(store, app_settings=Settings(mineru_api_key=""),
                          env_sync=True, env_path=str(env_path))
    svc.save_mineru({"api_key": "sk-rea…7890", "parser": "auto"})
    assert live_mineru_key() == "sk-real-key-1234567890"


def test_get_parse_keeps_env_paddleocr_when_store_value_empty(tmp_path, store, monkeypatch):
    """批1：store 里的空串不得覆盖 .env 非空值（否则只改解析模式会把辅通道 token 抹掉）。"""
    from app.config import Settings
    monkeypatch.setenv("PADDLEOCR_ACCESS_TOKEN", "tok-from-env")
    monkeypatch.setenv("PADDLEOCR_BASE_URL", "https://po.example")
    svc = SettingsService(store, app_settings=Settings())
    svc.save_parse({"mode": "single", "ai_review": True,
                    "paddleocr": {"access_token": "", "base_url": "",
                                  "model_version": ""}})
    po = svc.get_parse()["paddleocr"]
    assert po["access_token"] == "tok-from-env"      # 空串不覆盖
    assert po["base_url"] == "https://po.example"


# ---------------------------------------------------------------- 批2：解析参数（.env 单一来源）
_PARAM_KEYS = ("MINERU_LANGUAGE", "MINERU_IS_OCR", "MINERU_ENABLE_TABLE", "PADDLEOCR_OPTIONS")


def _parse_svc(tmp_path, store, monkeypatch):
    """带隔离 .env 的服务实例（env_sync=True 才写盘）。"""
    from app.config import Settings
    for k in _PARAM_KEYS:
        monkeypatch.delenv(k, raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")
    return SettingsService(store, app_settings=Settings(mineru_api_key="sk-x"),
                           env_sync=True, env_path=str(env_path)), env_path


def _parse_body(**kw) -> dict:
    body = {"mode": "dual", "ai_review": True, "translate_gate": "wait",
            "skip_review_batch": False, "parse_interval_sec": 8,
            "paddleocr": {"access_token": "", "base_url": "", "model_version": ""},
            "mineru_params": {}, "mineru_api_key": None}
    body.update(kw)
    return body


def test_parse_params_defaults(tmp_path, store, monkeypatch):
    """无 .env 记录 → auto/auto/表格开 + PaddleOCR 三开关全开（论文解析推荐值）。

    `mineru_params` 自 2026-09-20 起还带「模型版本」与 4 个可调 API 路径（GUI 可改），
    此处把默认值一并钉住（默认值变了就该红）。
    """
    svc, _ = _parse_svc(tmp_path, store, monkeypatch)
    p = svc.get_parse()
    assert p["mineru_params"] == {
        "language": "auto", "is_ocr": "auto", "enable_table": True,
        "model_version": "vlm",
        "api_paths": {"upload": "/file-urls/batch", "submit": "/extract/task",
                      "poll": "/extract/task/{task_id}",
                      "batch_results": "/extract-results/batch/{batch_id}"}}
    assert p["paddleocr"]["options"] == {"restructurePages": True, "mergeTables": True,
                                         "relevelTitles": True}


def test_parse_params_env_roundtrip(tmp_path, store, monkeypatch):
    """保存 → 写 .env → 回读断言 ok → get_parse 实时读回（含显式关掉 restructurePages）。"""
    svc, env_path = _parse_svc(tmp_path, store, monkeypatch)
    r = svc.save_parse(_parse_body(
        mineru_params={"language": "ch", "is_ocr": "on", "enable_table": False},
        paddleocr={"access_token": "", "base_url": "", "model_version": "",
                   "options": {"restructurePages": False, "mergeTables": True,
                               "relevelTitles": False}}))
    assert r["readback"]["ok"] is True, r["readback"]
    text = env_path.read_text(encoding="utf-8")
    assert "MINERU_LANGUAGE=ch" in text
    assert "MINERU_IS_OCR=on" in text
    assert "MINERU_ENABLE_TABLE=0" in text
    assert '"restructurePages": false' in text
    got = svc.get_parse()
    assert got["mineru_params"] == {
        "language": "ch", "is_ocr": "on", "enable_table": False,
        "model_version": "vlm",
        "api_paths": {"upload": "/file-urls/batch", "submit": "/extract/task",
                      "poll": "/extract/task/{task_id}",
                      "batch_results": "/extract-results/batch/{batch_id}"}}
    assert got["paddleocr"]["options"]["restructurePages"] is False
    assert got["paddleocr"]["options"]["relevelTitles"] is False


def test_parse_params_reject_invalid_values(tmp_path, store, monkeypatch):
    """非法值一律 400（ValueError），不静默改写用户的输入。"""
    svc, _ = _parse_svc(tmp_path, store, monkeypatch)
    with pytest.raises(ValueError):
        svc.save_parse(_parse_body(mineru_params={"language": "fr"}))
    with pytest.raises(ValueError):
        svc.save_parse(_parse_body(mineru_params={"is_ocr": "maybe"}))
    with pytest.raises(ValueError):
        svc.save_parse(_parse_body(paddleocr={"options": {"bogusSwitch": True}}))


# ================================================ 翻译池 .env 种子（2026-09-21）
def _seed_translate_env(monkeypatch, *, api_key=None, enabled="1"):
    monkeypatch.setenv("TRANSLATE_0_BASE_URL", "https://api.siliconflow.cn/v1")
    monkeypatch.setenv("TRANSLATE_0_MODEL", "Qwen/Qwen2.5-7B-Instruct")
    monkeypatch.setenv("TRANSLATE_0_MAX_TOKENS", "8192")
    monkeypatch.setenv("TRANSLATE_0_ENABLED", enabled)
    monkeypatch.delenv("TRANSLATE_0_API_KEY", raising=False)
    if api_key is not None:
        monkeypatch.setenv("TRANSLATE_0_API_KEY", api_key)


def test_translation_pool_seeded_from_env(store, monkeypatch):
    """只填一个硅基流动 Key ⇒ 翻译池自动有免费 Qwen2.5-7B（新装用户不必先点界面）。"""
    _seed_translate_env(monkeypatch)
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-fake-not-a-real-key")
    svc = SettingsService(store, app_settings=_make_env_settings())
    pool = svc.get_enabled_translation_providers(masked=False)
    assert len(pool) == 1
    assert pool[0]["model"] == "Qwen/Qwen2.5-7B-Instruct"
    assert pool[0]["api_key"] == "sk-fake-not-a-real-key"   # 留空 → 复用硅基 Key
    assert pool[0]["max_tokens"] == 8192
    assert pool[0]["id"] == "translate_0"


def test_translation_pool_seed_respects_explicit_key_and_disable(store, monkeypatch):
    """显式填了 TRANSLATE_0_API_KEY 就用它；ENABLED=0 则播进来但不参与路由。"""
    _seed_translate_env(monkeypatch, api_key="sk-qwen-key")
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-fake-not-a-real-key")
    svc = SettingsService(store, app_settings=_make_env_settings())
    assert svc.get_translation_providers(masked=False)[0]["api_key"] == "sk-qwen-key"
    assert len(svc.get_enabled_translation_providers(masked=False)) == 1

    monkeypatch.setenv("TRANSLATE_0_ENABLED", "0")
    svc2 = SettingsService(store, app_settings=_make_env_settings())
    assert len(svc2.get_translation_providers(masked=False)) == 1     # 可见
    assert svc2.get_enabled_translation_providers(masked=False) == []  # 不路由 → 回落主模型


def test_translation_pool_seed_skipped_without_any_key(store, monkeypatch):
    """启用但拿不到任何 Key ⇒ 不播种（宁可回落主模型，也不要"看着启用却调不通"）。"""
    _seed_translate_env(monkeypatch)
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    svc = SettingsService(store, app_settings=_make_env_settings())
    assert svc.get_translation_providers(masked=False) == []
    assert svc.get_enabled_translation_providers(masked=False) == []


def test_translation_pool_env_is_authoritative_over_db(store, monkeypatch):
    """2026-09-23 用户拍板：`.env` 的 TRANSLATE_* 是**权威**（与密钥同一规则，手改即生效）。

    旧语义"DB 有记录 ⇒ env 一律不生效"会让手改 .env 加翻译模型看似无效
    （用户实例实测：.env 里 TRANSLATE_1=glm-4.5-air 写了却加载不出来）。现按序号覆盖。
    """
    _seed_translate_env(monkeypatch)
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-fake-not-a-real-key")
    svc = SettingsService(store, app_settings=_make_env_settings())
    # 用户先在界面上存过一条（DB 有记录）
    svc.save_translation_providers([{
        "id": "t-db", "name": "用户选的", "base_url": "https://example.com/v1",
        "model": "user-model", "api_key": "sk-db", "enabled": False}])
    # 再手改 .env 第 0 槽 → 覆盖池里第 0 条，手改即生效
    pool = svc.get_translation_providers(masked=False)
    assert [p["id"] for p in pool] == ["translate_0"], pool
    assert [p["model"] for p in pool] == ["Qwen/Qwen2.5-7B-Instruct"]


def test_env_key_wins_over_shadowing_db_entry(store, monkeypatch):
    """同 (base_url, model) 时**主模型 Key 以 `.env` 为准**（2026-09-23 用户拍板）。

    用户实例：DB 里激活的「智谱」条目 Key 已失效（对 open.bigmodel.cn 返回 HTTP 401），
    而 `.env` 的 `ZHIPU_API_KEY` 可用；按 (base_url, model) 去重的合并逻辑让 DB 条目
    **整条遮蔽**了 .env 预设 ⇒ "在 .env 里换 Key"完全没生效，编译一直 401。
    与翻译池同规则：`.env` 权威。只覆盖 api_key，条目的 id/其它字段不动。
    """
    svc = SettingsService(store, app_settings=_make_env_settings())
    svc.save_providers([{
        "id": "p_zhipu", "name": "智谱",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-5.3-flash", "api_key": "sk-stale-db-key", "enabled": True}])
    assert svc.get_providers(masked=False)[0]["api_key"] == "sk-stale-db-key"

    monkeypatch.setenv("ZHIPU_API_KEY", "sk-fresh-env-key")
    monkeypatch.setenv("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    monkeypatch.setenv("ZHIPU_MODEL", "glm-5.3-flash")
    got = [p for p in svc.get_providers(masked=False) if p["model"] == "glm-5.3-flash"]
    assert len(got) == 1, got                        # 预设被吸收，不重复出现
    assert got[0]["api_key"] == "sk-fresh-env-key"   # Key 来自 .env（权威）
    assert got[0]["id"] == "p_zhipu"                 # 条目身份不动
    assert got[0]["name"] == "智谱"
    # 激活项仍是原来那条；翻译池不受影响
    assert svc.get_active_id() == "p_zhipu"


def test_env_key_adoption_keeps_db_key_when_same(store, monkeypatch):
    """两边 Key 相同时不做任何改写（不误报、不写日志噪音）。"""
    svc = SettingsService(store, app_settings=_make_env_settings())
    svc.save_providers([{
        "id": "p_zhipu", "name": "智谱",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-5.3-flash", "api_key": "sk-same", "enabled": True}])
    monkeypatch.setenv("ZHIPU_API_KEY", "sk-same")
    monkeypatch.setenv("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    monkeypatch.setenv("ZHIPU_MODEL", "glm-5.3-flash")
    got = [p for p in svc.get_providers(masked=False) if p["model"] == "glm-5.3-flash"]
    assert len(got) == 1 and got[0]["api_key"] == "sk-same"


def test_translation_pool_env_slots_merge_by_index(store, monkeypatch):
    """.env 第 i 槽覆盖池里第 i 条；.env 多出的槽追加；池里多出的条目保留。"""
    svc = SettingsService(store, app_settings=_make_env_settings())
    svc.save_translation_providers([
        {"id": "a", "name": "A", "base_url": "https://a.example/v1", "model": "m-a",
         "api_key": "sk-a", "enabled": True},
        {"id": "b", "name": "B", "base_url": "https://b.example/v1", "model": "m-b",
         "api_key": "sk-b", "enabled": False},
    ])
    monkeypatch.setenv("TRANSLATE_1_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    monkeypatch.setenv("TRANSLATE_1_MODEL", "glm-4.5-air")
    monkeypatch.setenv("TRANSLATE_1_API_KEY", "sk-env")
    monkeypatch.setenv("TRANSLATE_1_ENABLED", "1")
    monkeypatch.setenv("TRANSLATE_2_BASE_URL", "https://x.example/v1")
    monkeypatch.setenv("TRANSLATE_2_MODEL", "m-env-2")
    monkeypatch.setenv("TRANSLATE_2_API_KEY", "sk-env-2")
    pool = svc.get_translation_providers(masked=False)
    assert [p["model"] for p in pool] == ["m-a", "glm-4.5-air", "m-env-2"], pool
    # 槽 1 覆盖了池里第 1 条（含 ENABLED），但第 0 条（.env 无槽）保留原样
    assert [p["enabled"] for p in pool] == [True, True, True]
    assert pool[0]["id"] == "a" and pool[1]["id"] == "translate_1"


def test_translation_pool_single_active_enforced(store, monkeypatch):
    """2026-09-22 用户决定：池可存多条备选，但**同时只有一条生效**（设置里单选激活）。

    旧语义"启用多个 = 并行轮询分发批次"已删除：实测并非并行（`_run_batches` 串行），
    只是把各批分给不同模型 ⇒ 同一篇译文风格/术语不一致。
    """
    svc = SettingsService(store, app_settings=_make_env_settings())
    svc.save_translation_providers([
        {"id": "t1", "name": "A", "base_url": "https://a.example/v1", "model": "m-a",
         "api_key": "sk-a", "enabled": True},
        {"id": "t2", "name": "B", "base_url": "https://b.example/v1", "model": "m-b",
         "api_key": "sk-b", "enabled": True},
    ])
    pool = svc.get_translation_providers(masked=False)
    assert [p["id"] for p in pool] == ["t1", "t2"]        # 两条都留着（方便切换）
    assert [p["enabled"] for p in pool] == [True, False]  # 只留第一条生效
    enabled = svc.get_enabled_translation_providers(masked=False)
    assert [p["id"] for p in enabled] == ["t1"]


def test_translation_pool_all_off_falls_back_to_main(store, monkeypatch):
    """全部不激活是合法状态 = 回落主模型（池保留，随时可再激活）。"""
    svc = SettingsService(store, app_settings=_make_env_settings())
    svc.save_translation_providers([
        {"id": "t1", "name": "A", "base_url": "https://a.example/v1", "model": "m-a",
         "api_key": "sk-a", "enabled": False},
    ])
    assert svc.get_translation_providers(masked=False)[0]["enabled"] is False
    assert svc.get_enabled_translation_providers(masked=False) == []


def test_zhipu_env_slot(store, monkeypatch):
    """智谱 GLM 内置槽位：填 ZHIPU_API_KEY 即出现（不走 CUSTOM_PROVIDER_，避免 .env 越写越乱）。"""
    monkeypatch.setenv("ZHIPU_API_KEY", "zp-1234567890")
    svc = SettingsService(store, app_settings=_make_env_settings())
    z = [p for p in svc.get_providers(masked=False) if p["id"] == "zhipu"]
    assert len(z) == 1
    assert z[0]["name"] == "智谱 GLM"
    assert z[0]["model"] == "glm-5.3-flash"
    assert z[0]["base_url"] == "https://open.bigmodel.cn/api/paas/v4"
    assert z[0]["env"] == "ZHIPU"


def test_zhipu_env_slot_absent_without_key(store, monkeypatch):
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    svc = SettingsService(store, app_settings=_make_env_settings())
    assert [p["id"] for p in svc.get_providers(masked=False)] == ["deepseek"]


def test_zhipu_writeback_keeps_single_key_group(tmp_path, store, monkeypatch):
    """ZHIPU_* 写回大小写稳定：连存两次不产生第二组键（自定义供应商曾踩的坑）。"""
    from app.config import Settings
    monkeypatch.setenv("ZHIPU_API_KEY", "zp-1234567890")
    env_path = tmp_path / ".env"
    env_path.write_text("ZHIPU_API_KEY=zp-old\nZHIPU_BASE_URL=https://open.bigmodel.cn/api/paas/v4\n"
                        "ZHIPU_MODEL=glm-5.3-flash\n", encoding="utf-8")
    svc = SettingsService(store, app_settings=Settings(), env_sync=True, env_path=str(env_path))
    for _ in range(2):
        svc.save_providers(svc.get_providers(masked=False))
    text = env_path.read_text(encoding="utf-8")
    keys = [ln.split("=", 1)[0] for ln in text.splitlines() if ln.startswith("ZHIPU_")]
    assert sorted(keys) == ["ZHIPU_API_KEY", "ZHIPU_BASE_URL", "ZHIPU_MODEL"]


# ---------------------------------------- 翻译批次上限（2026-09-22 用户要求可调）
def test_translate_batch_chars_roundtrip_and_clear(store, monkeypatch):
    """0/留空 = 用默认（paperkb 侧 紧凑14000/主模型12000）；设了就存下来。"""
    svc = SettingsService(store, app_settings=_make_env_settings())
    assert svc.get_translate_batch_chars() == 0
    assert svc.save_translate_batch_chars(8000) == 8000
    assert svc.get_translate_batch_chars() == 8000
    assert svc.save_translate_batch_chars("") == 0        # 留空 = 清除
    assert svc.get_translate_batch_chars() == 0
    assert svc.get_all()["translate_batch_chars"] == 0    # 汇总里带出去给前端


def test_translate_batch_chars_clamped_and_rejects_garbage(store, monkeypatch):
    """越界钳到 [1000, 200000]（防手滑）；非数字抛 ValueError（API 层转 400）。"""
    svc = SettingsService(store, app_settings=_make_env_settings())
    assert svc.save_translate_batch_chars(50) == 1000
    assert svc.save_translate_batch_chars(999999) == 200000
    assert svc.get_translate_batch_chars() == 200000
    with pytest.raises(ValueError):
        svc.save_translate_batch_chars("abc")


def test_translate_output_budget_is_derived_from_batch_limit(store, monkeypatch):
    """2026-09-23：翻译池的输出预算由批次上限推导（只增不减），且只在运行时口径生效。

    实测输出 token ≈ 源字符 × 0.2，取 0.4 = 2 倍余量。用户把上限调大 ⇒ 预算自动跟上
    （旧行为：上限调大、预算仍卡在 8192 ⇒ 难懂的截断）。
    """
    from app.services.llm_service import translate_output_budget
    svc = SettingsService(store, app_settings=_make_env_settings())
    svc.save_translation_providers([{
        "id": "t1", "name": "Q", "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen2.5-7B-Instruct", "api_key": "sk-fake", "max_tokens": 8192,
        "enabled": True}])

    # 没设批次上限（= 用默认 14000）⇒ 14000×0.4=5600 < 8192 ⇒ 沿用模型自配值
    assert svc.get_enabled_translation_providers(masked=False)[0]["max_tokens"] == 8192
    # 上限 14000 ⇒ 5600 < 8192 ⇒ 仍是 8192
    svc.save_translate_batch_chars(14000)
    assert svc.get_enabled_translation_providers(masked=False)[0]["max_tokens"] == 8192
    # 上限 100000 ⇒ 40000 > 8192 ⇒ 预算抬到 40000（不落库）
    svc.save_translate_batch_chars(100000)
    assert svc.get_enabled_translation_providers(masked=False)[0]["max_tokens"] == 40000
    # 界面口径（masked=True）看到的仍是用户存的原值
    assert svc.get_translation_providers(masked=True)[0]["max_tokens"] == 8192

    # 纯函数语义：只增不减；两个都为 0 ⇒ 按紧凑默认（14000 → 5600），**绝不回落 64000**
    # （回落 64000 会让 8K 输出的小模型被服务端 400 拒：用户点「自动测一个值」报的错）
    assert translate_output_budget(14000, 8192) == 8192
    assert translate_output_budget(100000, 8192) == 40000
    assert translate_output_budget(1000, 64000) == 64000
    assert translate_output_budget(0, 0) == 5600
    assert translate_output_budget(None, None) == 5600


# ---------------------------------------- 单批上限：条目自带值优先（2026-09-23 用户要求）
def test_entry_batch_chars_roundtrip_and_env_sync(store, monkeypatch, tmp_path):
    """每个翻译模型条目可单独设单批上限：随条目留存，并回写 .env `TRANSLATE_<i>_BATCH_CHARS`。"""
    env_path = tmp_path / ".env"
    env_path.write_text("DEEPSEEK_API_KEY=sk-x\n", encoding="utf-8")   # 同步只在文件已存在时进行
    svc = SettingsService(store, app_settings=_make_env_settings(), env_sync=True,
                          env_path=str(env_path))
    svc.save_translation_providers([{
        "id": "t1", "name": "千问", "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen2.5-7B-Instruct", "api_key": "sk-a", "enabled": True,
        "batch_chars": 14000}])
    assert svc.get_translation_providers(masked=False)[0]["batch_chars"] == 14000
    assert "TRANSLATE_0_BATCH_CHARS=14000" in env_path.read_text(encoding="utf-8")
    # 越界钳制 / 非法归零
    svc.save_translation_providers([{
        "id": "t1", "name": "千问", "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen2.5-7B-Instruct", "api_key": "sk-a", "enabled": True,
        "batch_chars": 50}])
    assert svc.get_translation_providers(masked=False)[0]["batch_chars"] == 1000
    svc.save_translation_providers([{
        "id": "t1", "name": "千问", "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen2.5-7B-Instruct", "api_key": "sk-a", "enabled": True,
        "batch_chars": "abc"}])
    assert svc.get_translation_providers(masked=False)[0]["batch_chars"] == 0


def test_pool_save_syncs_os_environ_for_runtime_switch(store, monkeypatch, tmp_path):
    """保存池必须把 ENABLED/值同步进 os.environ——池的读取侧走 os.environ，
    只改文件不改进程 ⇒ 界面/路由还按旧模型走，表现为「点了切换瞬间又跳回去」。

    实测 bug（2026-09-23 用户报）：`_sync_translate_env` 对 int 型 `max_tokens` 直接调
    `.strip()` 抛 AttributeError，被 `except Exception` 吞掉 ⇒ 文件写了、进程没改，
    且当时池的读取已改为 `.env` 权威（os.environ 口径）⇒ 切换模型完全失效。
    """
    import os
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")
    svc = SettingsService(store, app_settings=_make_env_settings(), env_sync=True,
                          env_path=str(env_path))
    svc.save_translation_providers([
        {"id": "translate_0", "name": "千问", "base_url": "https://api.siliconflow.cn/v1",
         "model": "Qwen/Qwen2.5-7B-Instruct", "api_key": "sk-a", "enabled": True,
         "max_tokens": 8192, "batch_chars": 14000},
        {"id": "translate_1", "name": "GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4",
         "model": "glm-4.5-air", "api_key": "sk-b", "enabled": False,
         "max_tokens": 65536, "batch_chars": 0},
    ])
    assert os.environ["TRANSLATE_0_ENABLED"] == "1"        # 激活项
    assert os.environ["TRANSLATE_1_ENABLED"] == "0"
    assert os.environ["TRANSLATE_0_MAX_TOKENS"] == "8192"  # int 也要能写进去（历史崩溃点）
    assert os.environ["TRANSLATE_0_BATCH_CHARS"] == "14000"
    assert "TRANSLATE_1_BATCH_CHARS" not in os.environ     # 0 = 清键，不留脏值
    # 读回与刚保存的一致（不再出现"文件已改、进程没改"的错位）
    pool = svc.get_translation_providers(masked=False)
    assert [p["model"] for p in pool] == ["Qwen/Qwen2.5-7B-Instruct", "glm-4.5-air"]
    assert [p["enabled"] for p in pool] == [True, False]
    assert [p["model"] for p in svc.get_enabled_translation_providers(masked=False)] \
        == ["Qwen/Qwen2.5-7B-Instruct"]                     # 切换后路由确实换了模型


def test_pool_shrink_clears_stale_env_slots(store, tmp_path):
    """池缩短（删条目）后，os.environ 里旧序号的 `TRANSLATE_<i>_*` 必须一起清掉。

    读取侧（`_env_translation_presets`）走 os.environ，残留的旧槽会被当成**幽灵条目**复活：
    实测删到只剩 1 条时回读仍有 2 条，且第 2 条带着已删槽位的旧值 —— 用户会看到"保存过的
    条目还在/值对不上"。
    """
    import os
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")
    svc = SettingsService(store, app_settings=_make_env_settings(), env_sync=True,
                          env_path=str(env_path))
    qwen = {"id": "translate_0", "name": "千问",
            "base_url": "https://api.siliconflow.cn/v1",
            "model": "Qwen/Qwen2.5-7B-Instruct", "api_key": "sk-a",
            "enabled": True, "batch_chars": 14000}
    glm = {"id": "translate_1", "name": "GLM",
           "base_url": "https://open.bigmodel.cn/api/paas/v4",
           "model": "glm-4.5-air", "api_key": "sk-b",
           "enabled": False, "batch_chars": 12000}
    svc.save_translation_providers([qwen, glm])
    assert os.environ["TRANSLATE_1_BATCH_CHARS"] == "12000"
    # 删掉第 1 条 → 池只剩 1 条
    svc.save_translation_providers([qwen])
    assert "TRANSLATE_1_BATCH_CHARS" not in os.environ, "旧槽位值残留 → 会复活成幽灵条目"
    assert "TRANSLATE_1_MODEL" not in os.environ
    pool = svc.get_translation_providers(masked=False)
    assert len(pool) == 1 and pool[0]["model"] == "Qwen/Qwen2.5-7B-Instruct", pool


def test_effective_batch_chars_entry_wins_over_global(store, monkeypatch):
    """运行时上限 = 激活条目自带值 → 全局默认（安全冗余是模型属性，条目优先）。"""
    svc = SettingsService(store, app_settings=_make_env_settings())
    svc.save_translate_batch_chars(9000)                      # 全局默认
    svc.save_translation_providers([{
        "id": "t1", "name": "千问", "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen2.5-7B-Instruct", "api_key": "sk-a", "enabled": False,
        "batch_chars": 0}])
    assert svc.get_effective_translate_batch_chars() == 9000   # 都关着 ⇒ 用全局
    svc.save_translation_providers([{
        "id": "t1", "name": "千问", "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen2.5-7B-Instruct", "api_key": "sk-a", "enabled": True,
        "batch_chars": 0}])
    assert svc.get_effective_translate_batch_chars() == 9000   # 条目没单独设 ⇒ 仍用全局
    svc.save_translation_providers([{
        "id": "t1", "name": "千问", "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen2.5-7B-Instruct", "api_key": "sk-a", "enabled": True,
        "batch_chars": 14000}])
    assert svc.get_effective_translate_batch_chars() == 14000  # 条目值优先
