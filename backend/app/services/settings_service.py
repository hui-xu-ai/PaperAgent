# -*- coding: utf-8 -*-
"""设置服务（V03）：多供应商 LLM 配置 + 单价 + 知识库路径（SQLite，.env 兜底）。

供应商结构：
    {"id": str, "name": str, "base_url": str, "model": str,
     "api_key": str, "enabled": bool, "max_tokens": int | None,
     "reasoning_effort": str | None}
    可选 "supports_concurrency": bool 可覆盖默认并发能力（缺省按 id 查表，未知=串行）。
首次启动无配置时，从 .env 的 DEEPSEEK_API_KEY/BASE_URL/MODEL 生成默认 DeepSeek 供应商。
max_tokens：单次输出上限（可选，默认 DEFAULT_MAX_OUTPUT_TOKENS=64000，翻译用）。
reasoning_effort：思考强度（可选，None=按任务 context 自动——translate=low / compile=high，
用于 GLM 等始终思考的供应商；DeepSeek-chat 不思考不送也无需配置）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from pathlib import Path

from ..config import Settings as AppSettings
from .llm_service import DEFAULT_MAX_OUTPUT_TOKENS
from .store import Store

logger = logging.getLogger(__name__)

KEY_PROVIDERS = "providers"
KEY_ACTIVE = "active_provider"
KEY_TRANSLATION_PROVIDER = "translation_provider"  # T1：旧单条翻译配置（已迁移入池，仅兼容回读）
KEY_TRANSLATION_PROVIDERS = "translation_providers"  # 2026-09-19 翻译模型池（列表，enabled 标志）
KEY_PRICES = "prices"
KEY_KB_PATH = "kb_path"
# MinerU 主通道配置（.env 单一来源：MINERU_API_KEY / MINERU_PARSER）
KEY_MINERU = "mineru"                      # 仅作**展示兜底**（真值在 .env，见 save_mineru）
MINERU_PARSERS = ("auto", "mineru-v4", "mineru", "pymupdf")

# ---- 供应商并发能力（T：按供应商并发/串行控制）----
# 支持并发 LLM 请求的供应商（id=小写）。用户实测 glm-5.3-flash 支持 ~50 并发，
# 故 GLM/智谱系列均标为支持并发；未知供应商默认**串行**（保守，避免撞限流/额度）。
# 并发时并行 pipeline worker 数与词汇提示见 _CONCURRENT_MODEL_HINTS / get_provider_concurrency。
PROVIDER_CONCURRENCY: dict[str, bool] = {
    "deepseek": True,
    "zhipu": True,
    "glm": True,
    "glm-4": True,
    "zhipuai": True,
}
_DEFAULT_SUPPORTS_CONCURRENCY = False
# 模型名提示（get_provider_concurrency 兜底）：id 为自定义（p_xxx）时按模型名识别并发能力。
# glm-5.3-flash 实测 ~50 并发（用户 O批反馈）；deepseek 系列亦支持并发。
_CONCURRENT_MODEL_HINTS = ("glm", "deepseek")
# 支持并发时的并行 pipeline worker 数（保守：2，避免 MinerU/LLM 并发资源拥塞）
CONCURRENT_PIPELINE_WORKERS = 2
# P12-4：env 预填供应商的种子迁移标记（一次性：active 切到魔塔 ModelScope）
KEY_PROVIDER_SEED_V2 = "provider_seed_v2"
# P12 双 PDF 解析设置（mode=dual/single；ai_review=AI 仲裁开关；.env 兜底）
KEY_PARSE = "parse"
PARSE_MODES = ("dual", "single")
# 批2：MinerU 解析质量参数（.env 单一来源；auto = 按 PDF 首页智能判定，见 paperparse.core.parse_params）
MINERU_LANGUAGES = ("auto", "en", "ch")
MINERU_IS_OCR_MODES = ("auto", "on", "off")
# PaddleOCR optionalPayload：**权威定义在 paperparse.core.parse_params**（那里同时有默认值与文档依据）
try:
    from paperparse.core.parse_params import DEFAULT_PADDLEOCR_OPTIONS
except Exception:  # noqa: BLE001 - 包不可用（未 pip install）时兜底，形状必须与之保持一致
    DEFAULT_PADDLEOCR_OPTIONS = {"restructurePages": True, "mergeTables": True,
                                 "relevelTitles": True}

# kb 复制方式（T02）：copy=复制副本 / link=硬链接
KEY_KB_COPY_MODE = "kb_copy_mode"
KB_COPY_MODES = ("copy", "link")
# 2026-09-12 批1：删除「知识库纳入清单」kb_include —— 全仓无消费点（kb 产物由 layout 契约固定：
# _note.md/_wiki.md/_relations.md/en.md/document.json/images/），勾选框纯装饰（用户拍板删除）。

# AI 检索分级（T05）：L0 自动笔记 / L1 授权片段 / L2 授权全文
KEY_RETRIEVAL_MODE = "retrieval_mode"
RETRIEVAL_MODES = ("notes", "fragments", "full")
# 2026-09-12 批1：删除 retrieval_include（「AI 检索文件清单」）—— 检索侧只按 mode 走
# （chat_service._retrieval_mode），清单从无消费点，纯装饰（用户拍板删除）。
# 自动编译（Q5）：翻译完成后自动把该篇 L1 编译入队（默认开，执行仍手动控制）
KEY_AUTO_COMPILE = "auto_compile"
# 随窗口关闭自动退出（P5 点3）：前端关窗（beforeunload + 空闲心跳超时）后后台自动退出
KEY_AUTO_EXIT = "auto_exit"
# 对话思考强度（契约 3）：low/high/auto；auto = 不发 reasoning_effort（用供应商默认）。
# 说明：该设置由**前端读回后按请求回传** `POST /api/chat/stream` 的 `effort` 字段；
# 后端不把它当隐式默认（契约 1：body 缺省 = 不发送），以免与前端选择不一致。
KEY_CHAT_REASONING_EFFORT = "chat_reasoning_effort"
CHAT_REASONING_EFFORTS = ("low", "high", "auto")
# 批3：**编译思考档**（L1/L2/L3 编译任务的 reasoning_effort）。
# 取值语义（2026-09-12 用户决策 + 服务端实测）：
#   auto（默认）= **不发送** reasoning_effort ⇒ 服务端按任务自适应（= 一直以来的行为，质量不变）。
#                 ⚠️ 服务端**拒绝字面 "auto"**（实测 400: unknown variant `auto`），
#                 所以"自动"只能以"不传参"实现。
#   none / minimal / low / medium / high = 显式下发（实测均被接受；none 思考 0 字符最省）。
# 背景：`deepseek-flash` 是思考型模型，思考 token 计入 completion 并按输出价计费
# （实测 L2 输出 9,673 token 而产物仅 3,019 字符 ⇒ 约 7k 是思考）。
KEY_COMPILE_EFFORT = "compile_reasoning_effort"
KEY_TRANSLATE_EFFORT = "translate_reasoning_effort"
# 思考档**取值单一来源**（编译/翻译共用同一套；服务端实测：拒绝字面 "auto"，
# 接受 none/minimal/low/medium/high）
REASONING_EFFORT_LEVELS = ("auto", "none", "minimal", "low", "medium", "high")
COMPILE_EFFORTS = REASONING_EFFORT_LEVELS
TRANSLATE_EFFORTS = REASONING_EFFORT_LEVELS

# AI检索（paperlit）配置
KEY_LIT_EMBEDDING_API_KEY = "lit_embedding_api_key"
KEY_LIT_RERANKER_API_KEY = "lit_reranker_api_key"
KEY_LIT_EMBEDDING_MODEL = "lit_embedding_model"
KEY_LIT_RERANKER_MODEL = "lit_reranker_model"


def _mask(key: str) -> str:
    if not key:
        return ""
    return key[:6] + "…" + key[-4:] if len(key) > 12 else "***"


def _is_masked_key(submitted: str, old_key: str) -> bool:
    """判断前端提交的 api_key 是否为脱敏占位（用户未改动原值）。

    覆盖三种占位：长 key 掩码 ``abc123…xy``（含 …）、短 key 掩码 ``***``、
    前端点掩码（``••••``）。仅当提交值与旧值掩码串一致、或为纯掩码字符时才算
    占位——这样用户**新填的明文 key**不会被误判为占位，也不会把真实 key 存成掩码。
    """
    submitted = str(submitted or "").strip()
    if not submitted:
        return True  # 留空 → 保留原 key（沿用旧行为）
    if submitted == _mask(old_key):
        return True
    # 纯掩码字符（• * …）→ 视为未改动
    if re.sub(r"[•*…]", "", submitted) == "":
        return True
    return "…" in submitted


class SettingsService:
    def __init__(self, store: Store, app_settings: AppSettings | None = None,
                 env_sync: bool = False, env_path: str | None = None):
        """env_sync=True（仅生产容器注入）：保存供应商时按实际填写结果写回 .env
        （魔塔↔DEEPSEEK_*、硅基↔SILICONFLOW_*）——测试默认 False，防污染真实 .env；
        env_path 可注入（测试用 tmp .env），默认 APP_DATA_DIR/.env。"""
        self.store = store
        self.app_settings = app_settings
        self.env_sync = env_sync
        self.env_path = env_path or self._default_env_path()

    @staticmethod
    def _default_env_path() -> str:
        try:
            from ..config import APP_DATA_DIR
            return str(APP_DATA_DIR / ".env")
        except Exception:  # noqa: BLE001
            return ".env"

    # ---------------------------------------------------------- 供应商
    def _env_presets(self) -> list[dict]:
        """P12-4：.env 驱动的预填供应商（像 DeepSeek Harness 一样开箱即用）：
        - DEEPSEEK_* → 魔塔社区（ModelScope，当前 .env）或 DeepSeek 官方；
        - SILICONFLOW_* → 硅基流动（备用；无 key 时在**已有供应商**的应用里给出空白
          填写模板，用户 GUI 填 key 后保存即写回 .env）。
        每个预填条目带 env 标记（写回 .env 用），DB 旧条目不带。
        """
        presets: list[dict] = []
        s = self.app_settings
        # P（翻译截断）：env 预填供应商也带 max_tokens（可经 DEEPSEEK_MAX_TOKENS /
        # SILICONFLOW_MAX_TOKENS 覆盖，未设用默认 64000）
        ds_max = int(os.getenv("DEEPSEEK_MAX_TOKENS") or DEFAULT_MAX_OUTPUT_TOKENS)
        sf_max = int(os.getenv("SILICONFLOW_MAX_TOKENS") or DEFAULT_MAX_OUTPUT_TOKENS)
        if s and s.deepseek_api_key:
            if "modelscope" in (s.deepseek_base_url or "").lower():
                presets.append({"id": "modelscope", "name": "魔塔社区（ModelScope）",
                                "base_url": s.deepseek_base_url,
                                "model": s.deepseek_model,
                                "api_key": s.deepseek_api_key, "enabled": True,
                                "env": "DEEPSEEK", "max_tokens": ds_max,
                                "reasoning_effort": None})
            else:
                presets.append({"id": "deepseek", "name": "DeepSeek 官方",
                                "base_url": s.deepseek_base_url,
                                "model": s.deepseek_model,
                                "api_key": s.deepseek_api_key, "enabled": True,
                                "env": "DEEPSEEK", "max_tokens": ds_max,
                                "reasoning_effort": None})
        sf_key = os.getenv("SILICONFLOW_API_KEY", "").strip()
        if sf_key or self._db_has_providers():
            presets.append({
                "id": "siliconflow", "name": "硅基流动（备用）",
                "base_url": os.getenv("SILICONFLOW_BASE_URL",
                                      "https://api.siliconflow.cn/v1").strip(),
                "model": os.getenv("SILICONFLOW_MODEL",
                                   "deepseek-ai/DeepSeek-V4-Flash").strip(),
                "api_key": sf_key, "enabled": True, "env": "SILICONFLOW",
                "max_tokens": sf_max, "reasoning_effort": None})
        # 2026-09-19：Qwen2.5-7B-Instruct 预置翻译模型（8K 输出，用户只需填 QWEN_API_KEY）
        qwen_key = os.getenv("QWEN_API_KEY", "").strip()
        qwen_max = int(os.getenv("QWEN_MAX_TOKENS") or "8192")
        if qwen_key or self._db_has_translation_providers():
            presets.append({
                "id": "qwen-translate", "name": "通义千问 Qwen2.5-7B（翻译）",
                "base_url": os.getenv("QWEN_BASE_URL",
                                      "https://dashscope.aliyuncs.com/compatible-mode/v1").strip(),
                "model": os.getenv("QWEN_MODEL", "Qwen/Qwen2.5-7B-Instruct").strip(),
                "api_key": qwen_key, "enabled": True, "env": "QWEN",
                "max_tokens": qwen_max, "reasoning_effort": None})
        # P5 点1：.env 中自定义供应商（CUSTOM_PROVIDER_*）还原，跨 DB 重置仍存活
        presets.extend(self._custom_presets_from_env())
        return presets

    def _db_has_providers(self) -> bool:
        raw = self.store.get_setting(KEY_PROVIDERS)
        if not raw:
            return False
        try:
            return bool(json.loads(raw))
        except json.JSONDecodeError:
            return False

    def _db_has_translation_providers(self) -> bool:
        raw = self.store.get_setting(KEY_TRANSLATION_PROVIDERS)
        if not raw:
            return False
        try:
            return bool(json.loads(raw))
        except json.JSONDecodeError:
            return False

    def _merge_env_presets(self, providers: list[dict]) -> list[dict]:
        """DB 供应商 + env 预填合并（按 id 去重，DB 优先保留用户修改）。

        P5 点1：对自定义 env 预设另按 (base_url, model) 去重——.env 还原的自定义
        供应商 id 若与 DB 条目大小写/格式不一致，仍不重复（DB 优先）。
        """
        if not providers:  # 首次启动：env 兜底（保持旧行为：仅 env 供应商）
            return self._env_presets()
        ids = {p.get("id") for p in providers}
        seen_bm = {( (p.get("base_url") or "").strip().lower(),
                     (p.get("model") or "").strip() ) for p in providers}
        for preset in self._env_presets():
            bm = ((preset.get("base_url") or "").strip().lower(),
                  (preset.get("model") or "").strip())
            if preset["id"] in ids or bm in seen_bm:
                continue
            providers.append(preset)
            ids.add(preset["id"])
            seen_bm.add(bm)
        return providers

    def get_providers(self, masked: bool = True) -> list[dict]:
        raw = self.store.get_setting(KEY_PROVIDERS)
        providers: list[dict] = []
        if raw:
            try:
                providers = json.loads(raw)
            except json.JSONDecodeError:
                providers = []
        providers = self._merge_env_presets(providers)
        # P12-4 种子迁移（一次性）：默认激活魔塔 ModelScope（用户决策 D11/本会话）
        # P13 修复：仅当 **DB 无 active 记录**（首次/被清）才强制 modelscope；
        # 用户已激活过（KEY_ACTIVE 存在）→ 不覆盖用户选择（否则重启/刷新会跳回魔塔）。
        if self.store.get_setting(KEY_PROVIDER_SEED_V2) != "1":
            if not self.store.get_setting(KEY_ACTIVE):
                ids = [p.get("id") for p in providers]
                if "modelscope" in ids:
                    self.store.set_setting(KEY_ACTIVE, "modelscope")
            self.store.set_setting(KEY_PROVIDER_SEED_V2, "1")
        if masked:
            out = []
            for p in providers:
                q = dict(p)
                q["api_key"] = _mask(q.get("api_key", ""))
                out.append(q)
            return out
        return providers

    def save_providers(self, providers: list[dict]) -> None:
        """保存供应商列表（api_key 若为脱敏占位则保留原值；env 预填条目写回 .env）。"""
        current = self.get_providers(masked=False)
        cur_map = {p["id"]: p for p in current}
        cleaned = []
        for p in providers:
            p = dict(p)
            if _is_masked_key(p.get("api_key", ""), cur_map.get(p["id"], {}).get("api_key", "")):
                old = cur_map.get(p["id"], {})
                p["api_key"] = old.get("api_key", "")  # 脱敏占位 → 保留原 key
            if not p.get("id"):
                p["id"] = "p_" + uuid.uuid4().hex[:8]
            cleaned.append(p)
        self.store.set_setting(KEY_PROVIDERS, json.dumps(cleaned, ensure_ascii=False))
        # 若当前 active 不存在则回退：优先"第一个有 key 的供应商"（无 key 的供应商
        # LLM 不可用，回退到它=静默失效——P13 修复，原实现固定回退 cleaned[0] 即魔塔在前）
        active = self.get_active_id()
        if active not in {p["id"] for p in cleaned}:
            fallback = next((p["id"] for p in cleaned if p.get("api_key")), "")
            if not fallback and cleaned:
                fallback = cleaned[0]["id"]
            if fallback:
                self.store.set_setting(KEY_ACTIVE, fallback)
        # P12-4：env 预填条目按实际填写结果写回 .env（仅生产 env_sync=True）
        # P5 点1：自定义供应商（env 非 DEEPSEEK/SILICONFLOW）也写 .env 持久化
        if self.env_sync:
            # N4：清理**已删除**自定义供应商的 .env 残留（避免重启后 env 预填重新冒出）
            kept_ids = {p["id"] for p in cleaned}
            for old_p in current:
                if old_p["id"] not in kept_ids and not (old_p.get("env") in ("DEEPSEEK", "SILICONFLOW")):
                    try:
                        self._remove_custom_env(old_p["id"])
                    except Exception as e:  # noqa: BLE001
                        logger.warning("清理 .env 失败（%s）: %s", old_p["id"], e)
            for p in cleaned:
                env_prefix = (p.get("env") or "").strip()
                if env_prefix in ("DEEPSEEK", "SILICONFLOW"):
                    try:
                        self._sync_env_file(env_prefix, p)
                    except Exception as e:  # noqa: BLE001 - 写 .env 失败不阻塞保存
                        logger.warning("写回 .env 失败（%s）: %s", env_prefix, e)
                else:
                    try:
                        self._sync_custom_env(p)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("写回 .env 失败（自定义）: %s", e)

    # ---------------------------------------------------------- .env 写回（P12-4）
    _ENV_KEYS = {
        "DEEPSEEK": ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL"),
        "SILICONFLOW": ("SILICONFLOW_API_KEY", "SILICONFLOW_BASE_URL", "SILICONFLOW_MODEL"),
        "QWEN": ("QWEN_API_KEY", "QWEN_BASE_URL", "QWEN_MODEL"),
    }

    def _sync_env_file(self, env_prefix: str, provider: dict) -> None:
        """按实际填写结果更新 .env 对应键（只改**未注释**的 KEY= 行；无该键则追加，
        保留注释与原有顺序）。"""
        env_path = Path(self.env_path)
        if not env_path.exists():
            return
        vals = [provider.get("api_key", ""), provider.get("base_url", ""),
                provider.get("model", "")]
        text = env_path.read_text(encoding="utf-8-sig")
        lines = text.splitlines()
        changed = False
        for key, val in zip(self._ENV_KEYS[env_prefix], vals):
            replaced = False
            for i, ln in enumerate(lines):
                if ln.lstrip().startswith(key + "="):
                    lines[i] = f"{key}={val}"
                    replaced = True
                    changed = True
                    break
            if not replaced and val:
                lines.append(f"{key}={val}")
                changed = True
        if changed:
            env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 自定义供应商 → .env 持久化（P5 点1 修复：用户填自定义供应商（如智谱 glm）
    # 保存时也写 .env，满足"保存后 .env 可见 + 跨 DB 重置仍存活"预期）
    _CUSTOM_ENV_FIELDS = {"name": "NAME", "base_url": "BASE_URL",
                          "model": "MODEL", "api_key": "API_KEY",
                          "max_tokens": "MAX_TOKENS",
                          "reasoning_effort": "REASONING_EFFORT"}

    def _sync_custom_env(self, provider: dict) -> None:
        env_path = Path(self.env_path)
        if not env_path.exists():
            return
        pid = (provider.get("id") or "").strip()
        if not pid:
            return
        import re as _re
        prefix = f"CUSTOM_PROVIDER_{_re.sub(r'[^A-Za-z0-9_]', '_', pid)}_"
        text = env_path.read_text(encoding="utf-8-sig")
        lines = text.splitlines()
        lines = [ln for ln in lines if not ln.lstrip().startswith(prefix)]
        changed = False
        for field, suffix in self._CUSTOM_ENV_FIELDS.items():
            val = provider.get(field) or ""
            if val:
                lines.append(f"{prefix}{suffix}={val}")
                changed = True
        if changed:
            env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        # 运行中进程即时生效（load_config 读 os.environ）
        for field, suffix in self._CUSTOM_ENV_FIELDS.items():
            val = provider.get(field) or ""
            if val:
                os.environ[f"{prefix}{suffix}"] = str(val)

    def _remove_custom_env(self, provider_id: str) -> None:
        """N4：从 .env 删除某自定义供应商全部条目（删除供应商时清理残留）。"""
        env_path = Path(self.env_path)
        if not env_path.exists():
            return
        import re as _re
        prefix = f"CUSTOM_PROVIDER_{_re.sub(r'[^A-Za-z0-9_]', '_', provider_id)}_"
        text = env_path.read_text(encoding="utf-8-sig")
        lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith(prefix)]
        # 运行中清除 os.environ 对应条目
        for key in list(os.environ.keys()):
            if key.startswith(prefix):
                del os.environ[key]
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ------------------------------------------------ .env 通用写回 + 回读断言（批1）
    def _write_env_keys(self, values: dict[str, str]) -> None:
        """把 `{KEY: value}` 写入 .env（只改**未注释**的 `KEY=` 行；无该键则追加；文件不存在
        则创建）并同步 `os.environ`（运行中进程立即生效：`load_dotenv` 不覆盖已有变量 ⇒
        os.environ 即权威值）。

        ⚠️ **空值会写空**（用于"显式清空"）；需要"跳过空值"的调用方自行过滤。
        供应商那两套历史实现（`_sync_env_file`/`_sync_custom_env`）暂未合并——它们有
        「.env 不存在就不动」的既有语义与测试，合并留待批2，避免此刻引入漂移。
        """
        env_path = Path(self.env_path)
        lines = env_path.read_text(encoding="utf-8-sig").splitlines() \
            if env_path.exists() else []
        changed = False
        for key, val in values.items():
            new_line = f"{key}={val}"
            replaced = False
            for i, ln in enumerate(lines):
                if ln.lstrip().startswith(key + "="):
                    if lines[i] != new_line:
                        lines[i] = new_line
                        changed = True
                    replaced = True
                    break
            if not replaced:
                lines.append(new_line)
                changed = True
        if changed:
            env_path.parent.mkdir(parents=True, exist_ok=True)
            env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        for key, val in values.items():
            os.environ[key] = val

    def _readback_env_keys(self, expected: dict[str, str]) -> dict:
        """回读断言：.env 文件与 os.environ 是否都等于刚写入的值。

        返回 `{"ok": bool, "mismatch": [KEY...], "values": {KEY: 实际值}}`——
        `ok=False` 时 API 层必须显式告知用户（只读盘/被占用等），不得静默成功。
        """
        from_file: dict[str, str] = {}
        env_path = Path(self.env_path)
        if env_path.exists():
            for ln in env_path.read_text(encoding="utf-8-sig").splitlines():
                s = ln.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, v = s.split("=", 1)
                from_file[k.strip()] = v.strip()
        mismatch, values = [], {}
        for key, want in expected.items():
            actual = from_file.get(key, os.environ.get(key))
            values[key] = actual if actual is not None else ""
            if (actual or "") != (want or ""):
                mismatch.append(key)
        return {"ok": not mismatch, "mismatch": mismatch, "values": values}

    def _custom_presets_from_env(self) -> list[dict]:
        """读取 .env 的 CUSTOM_PROVIDER_<id>_* 还原自定义供应商（P5 点1 修复）。"""
        import re as _re
        groups: dict[str, dict] = {}
        for key, val in os.environ.items():
            m = _re.match(
                r"^CUSTOM_PROVIDER_([A-Za-z0-9_]+)_(NAME|BASE_URL|MODEL|API_KEY|MAX_TOKENS|REASONING_EFFORT)$",
                key)
            if not m:
                continue
            g = groups.setdefault(m.group(1), {})
            g[m.group(2).lower()] = val
        out = []
        for pid, fields in groups.items():
            if fields.get("base_url") and fields.get("model"):
                out.append({
                    "id": pid if pid.startswith("p_") else "p_" + pid,
                    "name": fields.get("name") or "自定义",
                    "base_url": fields.get("base_url"),
                    "model": fields.get("model"),
                    "api_key": fields.get("api_key") or "",
                    "enabled": True, "env": "",
                    "max_tokens": (int(fields["max_tokens"])
                                   if fields.get("max_tokens") else DEFAULT_MAX_OUTPUT_TOKENS),
                    "reasoning_effort": (fields.get("reasoning_effort") or None),
                })
        return out

    def get_active_id(self) -> str:
        active = self.store.get_setting(KEY_ACTIVE)
        if active:
            return active
        providers = self.get_providers(masked=False)
        if providers:
            first = providers[0]["id"]
            self.store.set_setting(KEY_ACTIVE, first)
            return first
        return ""

    def get_active_provider(self, masked: bool = True) -> dict | None:
        providers = self.get_providers(masked=masked)
        active_id = self.get_active_id()
        for p in providers:
            if p["id"] == active_id:
                return p
        return providers[0] if providers else None

    def set_active_provider(self, provider_id: str) -> None:
        providers = self.get_providers(masked=False)
        if not any(p["id"] == provider_id for p in providers):
            raise ValueError(f"供应商不存在: {provider_id}")
        self.store.set_setting(KEY_ACTIVE, provider_id)

    # ---------------------------------------------------------- 翻译模型池（多模型：切换/并行）
    # 2026-09-19 从单个翻译供应商升级为**池**：`translation_providers` 为 JSON 列表，每个条目
    # 带 `enabled` 标志。**启用 1 个 = 切换模式**（只用该模型）；**启用多个 = 并行模式**
    # （轮询分发各批次，提速）。都不启用 = 回落主模型。旧单条 `translation_provider` 自动迁移入池。
    def get_translation_providers(self, masked: bool = True) -> list[dict]:
        """获取翻译供应商池（全部，含未启用的）。[] = 未配置（回落主模型）。"""
        raw = self.store.get_setting(KEY_TRANSLATION_PROVIDERS)
        providers: list[dict] = []
        if raw:
            try:
                v = json.loads(raw)
                providers = v if isinstance(v, list) else []
            except json.JSONDecodeError:
                providers = []
        # 一次性迁移：旧单条 → 池
        if not providers:
            old = self.store.get_setting(KEY_TRANSLATION_PROVIDER)
            if old:
                try:
                    single = json.loads(old)
                    if isinstance(single, dict) and single.get("model"):
                        single["enabled"] = True
                        providers = [single]
                        self.store.set_setting(
                            KEY_TRANSLATION_PROVIDERS,
                            json.dumps(providers, ensure_ascii=False))
                except json.JSONDecodeError:
                    pass
        if masked:
            out = []
            for p in providers:
                q = dict(p)
                q["api_key"] = _mask(q.get("api_key", ""))
                out.append(q)
            return out
        return providers

    def get_enabled_translation_providers(self, masked: bool = False) -> list[dict]:
        """池内**已启用**的翻译供应商（实际参与翻译路由的）。"""
        return [p for p in self.get_translation_providers(masked=masked)
                if p.get("enabled")]

    def save_translation_providers(self, providers: list[dict]) -> None:
        """保存翻译供应商池（整体替换；脱敏 key 保留原值；补 id/enabled 缺省）。"""
        current = {p["id"]: p for p in self.get_translation_providers(masked=False)}
        cleaned = []
        for p in providers:
            p = dict(p)
            if not p.get("id"):
                p["id"] = "translate_" + uuid.uuid4().hex[:8]
            cur = current.get(p["id"], {})
            if _is_masked_key(p.get("api_key", ""), cur.get("api_key", "")):
                p["api_key"] = cur.get("api_key", "")
            p["enabled"] = bool(p.get("enabled"))
            cleaned.append(p)
        self.store.set_setting(KEY_TRANSLATION_PROVIDERS,
                               json.dumps(cleaned, ensure_ascii=False))
        # 迁移后清掉旧单条键（避免回读歧义）
        if self.store.get_setting(KEY_TRANSLATION_PROVIDER):
            self.store.set_setting(KEY_TRANSLATION_PROVIDER, "")
        # .env 持久化（与主供应商/MinerU 一致）
        if self.env_sync:
            try:
                # QWEN 预置供应商走 _sync_env_file（QWEN_API_KEY 等标准键）
                qwen_p = next((p for p in cleaned if p.get("env") == "QWEN"), None)
                if qwen_p:
                    self._sync_env_file("QWEN", qwen_p)
                # 其余翻译供应商走 TRANSLATE_<idx>_ 键
                non_qwen = [p for p in cleaned if p.get("env") != "QWEN"]
                if non_qwen:
                    self._sync_translate_env(non_qwen)
            except Exception as e:  # noqa: BLE001
                logger.warning("写翻译模型 .env 失败: %s", e)

    # 兼容旧调用点（单数语义 = 第一个启用的）
    def get_translation_provider(self, masked: bool = True) -> dict | None:
        """兼容包装：返回第一个**已启用**的翻译供应商（无则 None）。"""
        enabled = self.get_enabled_translation_providers(masked=masked)
        return enabled[0] if enabled else None

    def save_translation_provider(self, provider: dict | None) -> None:
        """兼容包装：按单条保存（None=清空池）。"""
        if not provider:
            self.clear_translation_provider()
            return
        provider = dict(provider)
        provider.setdefault("enabled", True)
        self.save_translation_providers([provider])

    def clear_translation_provider(self) -> None:
        """清除翻译供应商池（回落主模型）。"""
        self.store.set_setting(KEY_TRANSLATION_PROVIDERS, "")
        self.store.set_setting(KEY_TRANSLATION_PROVIDER, "")
        if self.env_sync:
            try:
                self._clear_translate_env()
            except Exception as e:  # noqa: BLE001
                logger.warning("清理翻译模型 .env 失败: %s", e)

    def _sync_translate_env(self, providers: list[dict]) -> None:
        """把翻译模型池写入 .env（TRANSLATE_<idx>_* 键），同时清理多余旧键。"""
        import re as _re
        env_path = Path(self.env_path)
        if not env_path.exists():
            return
        text = env_path.read_text(encoding="utf-8-sig")
        lines = text.splitlines()
        # 移除所有 TRANSLATE_<idx>_ 旧键
        lines = [ln for ln in lines
                 if not _re.match(r'\s*TRANSLATE_\d+_', ln.lstrip())]
        for idx, p in enumerate(providers):
            base_url = (p.get("base_url") or "").strip()
            model = (p.get("model") or "").strip()
            api_key = (p.get("api_key") or "").strip()
            max_tokens = str(p.get("max_tokens") or "")
            enabled = "1" if p.get("enabled") else "0"
            if base_url:
                lines.append(f"TRANSLATE_{idx}_BASE_URL={base_url}")
            if model:
                lines.append(f"TRANSLATE_{idx}_MODEL={model}")
            if api_key:
                lines.append(f"TRANSLATE_{idx}_API_KEY={api_key}")
            if max_tokens:
                lines.append(f"TRANSLATE_{idx}_MAX_TOKENS={max_tokens}")
            lines.append(f"TRANSLATE_{idx}_ENABLED={enabled}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        # 运行中进程即时生效
        for idx, p in enumerate(providers):
            for field, suffix in (("base_url", "BASE_URL"), ("model", "MODEL"),
                                  ("api_key", "API_KEY"), ("max_tokens", "MAX_TOKENS")):
                val = (p.get(field) or "").strip()
                if val:
                    os.environ[f"TRANSLATE_{idx}_{suffix}"] = val
            os.environ[f"TRANSLATE_{idx}_ENABLED"] = "1" if p.get("enabled") else "0"

    def _clear_translate_env(self) -> None:
        """清除 .env 中所有 TRANSLATE_<idx>_ 键。"""
        import re as _re
        env_path = Path(self.env_path)
        if not env_path.exists():
            return
        text = env_path.read_text(encoding="utf-8-sig")
        lines = [ln for ln in text.splitlines()
                 if not _re.match(r'\s*TRANSLATE_\d+_', ln.lstrip())]
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        # 清理 os.environ 中的残留
        for key in list(os.environ):
            if _re.match(r'TRANSLATE_\d+_', key):
                del os.environ[key]

    # ---------------------------------------------------------- 供应商并发能力（T）
    def provider_supports_concurrency(self, provider_id: str) -> bool:
        """供应商是否支持并发 LLM 请求（按 id 查表；未知 → 默认串行，保守）。

        仅部分供应商（如 DeepSeek）支持并发；智谱 GLM 与未知供应商 = 串行。
        """
        pid = (provider_id or "").strip().lower()
        if not pid:
            return _DEFAULT_SUPPORTS_CONCURRENCY
        return PROVIDER_CONCURRENCY.get(pid, _DEFAULT_SUPPORTS_CONCURRENCY)

    def get_provider_concurrency(self, provider: dict | None) -> bool:
        """按 provider dict 判定并发能力（provider 自带 supports_concurrency 可覆盖；
        缺省回落到按 id 查表，未知=串行保守）。供应商 id 为自定义（p_xxx 等）时额外按
        模型名提示（glm/deepseek 等）识别并发能力（O批：glm-5.3-flash 实测 ~50 并发）。"""
        if not provider:
            return _DEFAULT_SUPPORTS_CONCURRENCY
        explicit = provider.get("supports_concurrency")
        if explicit is not None:
            return bool(explicit)
        pid = str(provider.get("id") or "").strip().lower()
        if self.provider_supports_concurrency(pid):
            return True
        model = str(provider.get("model") or "").lower()
        if any(h in model for h in _CONCURRENT_MODEL_HINTS):
            return True
        return False

    def get_pipeline_workers(self) -> int:
        """并行 pipeline worker 数：激活供应商支持并发 → CONCURRENT_PIPELINE_WORKERS，
        否则 1（串行，保守默认）。"""
        active = self.get_active_provider(masked=False)
        if self.get_provider_concurrency(active):
            return CONCURRENT_PIPELINE_WORKERS
        return 1

    # ---------------------------------------------------------- 单价（T3：按供应商-模型组合）
    # 存储格式（KEY_PRICES 仍为单一 JSON）：
    #   {"by_provider_model": {"<provider_id>::<model>": {input/cached/output 三项}},
    #    "default": {全局默认三项}}
    # 兼容旧数据：旧扁平格式（仅 input/cached/output 三项）读取时封装为 default。
    _PRICE_KEYS = ("input_per_m", "cached_input_per_m", "output_per_m")

    def get_prices(self) -> dict:
        """返回完整价格结构 {"by_provider_model": {...}, "default": {...}}（供前端渲染）。

        **只读当前格式**（2026-09-12）：旧扁平格式由迁移层 `0002_settings_prices.py`
        在启动时一次性规整（见 docs/VERSIONING.md §3 / COMPAT-REGISTER A1），
        业务层不再做读时兼容。M5：`default` 恒为 0（取消全局默认单价，只按 per-model 计价）。
        """
        raw = self.store.get_setting(KEY_PRICES)
        data = None
        if raw:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = None
        default = {k: 0.0 for k in self._PRICE_KEYS}
        bpm = data.get("by_provider_model") if isinstance(data, dict) else None
        if not isinstance(bpm, dict):
            bpm = {}
        return {"by_provider_model": bpm, "default": default}

    def get_prices_for(self, provider_id: str, model: str) -> dict:
        """按 (provider_id, model) 查单价：by_provider_model 命中 → 用该值；未命中 → 0 价。
        M5：取消全局默认单价，未录入单价的模型按 0 计价。"""
        struct = self.get_prices()
        entry = struct["by_provider_model"].get(f"{provider_id}::{model}")
        if isinstance(entry, dict):
            return {k: float(entry.get(k) or 0.0) for k in self._PRICE_KEYS}
        return {k: 0.0 for k in self._PRICE_KEYS}

    def save_prices(self, prices: dict) -> None:
        """保存价格（兼容三种输入）：
        - 新完整结构 {"default": ..., "by_provider_model": ...}：default 合并、by_provider_model 整表替换；
        - 单项 {"provider_id","model", 三项}：写/更新 by_provider_model 单项（三项全空 → 删项回落默认）；
        - 旧扁平三项（无 provider_id）→ 更新 default（缺字段不覆盖）。"""
        cur = self.get_prices()
        prices = dict(prices or {})
        if "by_provider_model" in prices or "default" in prices:
            new_default = prices.get("default")
            if isinstance(new_default, dict):
                cur["default"].update(
                    {k: v for k, v in new_default.items()
                     if k in self._PRICE_KEYS and v is not None})
            new_bpm = prices.get("by_provider_model")
            if isinstance(new_bpm, dict):
                cur["by_provider_model"] = new_bpm
        else:
            pid = str(prices.get("provider_id") or "").strip()
            model = str(prices.get("model") or "").strip()
            vals = {k: prices.get(k) for k in self._PRICE_KEYS}
            if pid and model:
                key = f"{pid}::{model}"
                if all(v is None for v in vals.values()):
                    cur["by_provider_model"].pop(key, None)  # 全空 → 回落默认
                else:
                    entry = dict(cur["by_provider_model"].get(key, {}))
                    for k, v in vals.items():
                        if v is not None:
                            entry[k] = v
                    cur["by_provider_model"][key] = entry
            else:
                for k, v in vals.items():
                    if v is not None:
                        cur["default"][k] = v
        self.store.set_setting(KEY_PRICES, json.dumps(cur, ensure_ascii=False))

    def get_kb_path(self) -> str:
        return self.store.get_setting(KEY_KB_PATH, "")

    def save_kb_path(self, path: str) -> None:
        self.store.set_setting(KEY_KB_PATH, path)

    # ---------------------------------------------------------- kb 复制方式（T02）
    def get_kb_copy_mode(self) -> str:
        mode = self.store.get_setting(KEY_KB_COPY_MODE, "copy") or "copy"
        return mode if mode in KB_COPY_MODES else "copy"

    def save_kb_copy_mode(self, mode: str) -> None:
        self.store.set_setting(KEY_KB_COPY_MODE,
                               mode if mode in KB_COPY_MODES else "copy")

    # ---------------------------------------------------------- AI 检索分级（T05）
    def get_retrieval_mode(self) -> str:
        mode = self.store.get_setting(KEY_RETRIEVAL_MODE, "") or ""
        return mode if mode in RETRIEVAL_MODES else "notes"

    def save_retrieval_mode(self, mode: str) -> None:
        self.store.set_setting(KEY_RETRIEVAL_MODE,
                               mode if mode in RETRIEVAL_MODES else "notes")

    # ---------------------------------------------------------- 编译思考档（批3）
    def get_compile_effort(self) -> str:
        """L1/L2/L3 编译任务的思考档：auto（默认，=不发送参数）/ none / minimal / low / medium / high。"""
        raw = (self.store.get_setting(KEY_COMPILE_EFFORT) or "").strip().lower()
        return raw if raw in COMPILE_EFFORTS else "auto"

    def save_compile_effort(self, effort: str) -> str:
        """保存编译思考档；非法值抛 ValueError（API 层转 400），返回归一化值。"""
        val = (effort or "").strip().lower()
        if val not in COMPILE_EFFORTS:
            raise ValueError("编译思考档仅支持 " + "/".join(COMPILE_EFFORTS))
        self.store.set_setting(KEY_COMPILE_EFFORT, val)
        return val

    # ---------------------------------------------------------- 翻译思考档（批3）
    def get_translate_effort(self) -> str:
        """翻译任务的思考档：auto（默认，=不发送参数 ⇒ 服务端自适应，质量与历史一致）
        / none / minimal / low / medium / high。

        推荐默认 auto：翻译是**长输出**任务，思考 token 按输出价计费（降档能省钱），
        但思考承担"术语/缩略语一致、公式 [[MATHn]] 标签不乱动、长句结构判断"——
        掉档最可能坏在这三处，故默认不动；要省 token 可在设置中心显式选 low。
        """
        raw = (self.store.get_setting(KEY_TRANSLATE_EFFORT) or "").strip().lower()
        return raw if raw in TRANSLATE_EFFORTS else "auto"

    def save_translate_effort(self, effort: str) -> str:
        val = (effort or "").strip().lower()
        if val not in TRANSLATE_EFFORTS:
            raise ValueError("翻译思考档仅支持 " + "/".join(TRANSLATE_EFFORTS))
        self.store.set_setting(KEY_TRANSLATE_EFFORT, val)
        return val

    # ---------------------------------------------------------- 自动编译（Q5）
    def get_auto_compile(self) -> bool:
        """翻译完成后自动把该篇 L1 编译入队（默认开；执行仍由队列手动触发）。"""
        return self.store.get_setting(KEY_AUTO_COMPILE, "1") != "0"

    def save_auto_compile(self, enabled: bool) -> None:
        self.store.set_setting(KEY_AUTO_COMPILE, "1" if enabled else "0")

    # ---------------------------------------------------------- 随窗口关闭退出（P5 点3）
    def get_auto_exit(self) -> bool:
        # 默认关（用户反馈：不要因关窗监督/误杀后端；需要时在设置里手动开）
        return self.store.get_setting(KEY_AUTO_EXIT, "0") != "0"

    def save_auto_exit(self, enabled: bool) -> None:
        self.store.set_setting(KEY_AUTO_EXIT, "1" if enabled else "0")

    # ---------------------------------------------------------- 对话思考强度（契约 3）
    def get_chat_reasoning_effort(self) -> str:
        """对话思考强度：low / high / auto（默认 auto）。读回时非法值兜底为 auto。"""
        raw = (self.store.get_setting(KEY_CHAT_REASONING_EFFORT) or "").strip().lower()
        return raw if raw in CHAT_REASONING_EFFORTS else "auto"

    def save_chat_reasoning_effort(self, effort: str) -> str:
        """保存对话思考强度；非法值抛 ValueError（API 层转 400），返回归一化后的值。"""
        val = (effort or "").strip().lower()
        if val not in CHAT_REASONING_EFFORTS:
            raise ValueError("effort 仅支持 " + "/".join(CHAT_REASONING_EFFORTS))
        self.store.set_setting(KEY_CHAT_REASONING_EFFORT, val)
        return val

    def get_mineru(self) -> dict:
        """MinerU 主通道配置（**实时值**）：`.env`/os.environ 为单一来源，DB 仅历史兜底。

        2026-09-12 批1：原实现"store 优先"（写 SQLite）而引擎读 `.env` ⇒ UI 保存无效。
        现在读回顺序：os.environ（保存即写）→ DB 旧值（升级前的残留）→ 启动快照。
        """
        from ..config import live_mineru_key
        s = self.app_settings
        key = live_mineru_key(s.mineru_api_key if s else "")
        parser = (s.mineru_parser if s else "auto") or "auto"
        if not key and "MINERU_API_KEY" not in os.environ:
            raw = self.store.get_setting(KEY_MINERU)
            if raw:
                try:
                    legacy = json.loads(raw)
                    key = str(legacy.get("api_key") or "").strip()
                except json.JSONDecodeError:
                    pass
        return {"api_key": key, "parser": parser}

    def save_mineru(self, cfg: dict) -> dict:
        """保存 MinerU 主通道配置 → **写 .env + os.environ**（单一来源）→ 回读断言。

        2026-09-12 批1 修复（最严重的一条）：原实现只写 SQLite，而解析降级链
        （engine_service._parse_chain）与 paperparse（load_config → MINERU_API_KEY）
        都读 `.env`/os.environ ⇒ 用户在设置中心改 Key/通道**完全无效**。
        - 空 Key = 显式清空（写空串，不回退旧值）——"删掉 Key 回落免费通道"必须能生效；
        - 掩码占位（前端回显脱敏）视为"未改动"，保留现有 Key，不把占位串写进 .env；
        - 返回 {api_key_set, parser, readback}：readback.ok=False 表示 .env 与内存不一致
          （只读/被占用等），API 层会把它暴露给用户而不是静默成功。
        """
        from ..config import live_mineru_key
        key = str(cfg.get("api_key", "") or "").strip()
        parser = str(cfg.get("parser", "auto") or "auto").strip()
        if parser not in MINERU_PARSERS:
            raise ValueError("MinerU 解析通道必须是 " + "/".join(MINERU_PARSERS))
        if key and _is_masked_key(key, live_mineru_key()):
            key = live_mineru_key()          # 脱敏占位 → 保留原值
        self.store.set_setting(KEY_MINERU,
                               json.dumps({"api_key": key, "parser": parser},
                                          ensure_ascii=False))
        if self.env_sync:
            self._write_env_keys({"MINERU_API_KEY": key, "MINERU_PARSER": parser})
        expected = {"MINERU_API_KEY": key, "MINERU_PARSER": parser}
        return {"api_key_set": bool(key), "parser": parser,
                "readback": self._readback_env_keys(expected)}

    # ---------------------------------------------------------- PDF 解析（P12）
    def get_parse(self) -> dict:
        """PDF 解析设置（设置中心「解析」tab 的全部数据源）。

        - `mode`/`ai_review`/`translate_gate`/`parse_interval_sec`/`skip_review_batch`：SQLite（GUI 保存）
        - `mineru_params`（language/is_ocr/enable_table）：**.env 单一来源**（批2，实时读）
        - `paddleocr`（token/base_url/model + `options`）：token 等走 .env，`options` 亦 .env
        """
        out = {"mode": "dual", "ai_review": True, "paddleocr": self._paddleocr_from_env(),
               "mineru_params": self._mineru_params_from_env(),
               "translate_gate": "wait", "parse_interval_sec": 8, "skip_review_batch": False}
        raw = self.store.get_setting(KEY_PARSE)
        if raw:
            try:
                v = json.loads(raw)
                if v.get("mode") in PARSE_MODES:
                    out["mode"] = v["mode"]
                out["ai_review"] = bool(v.get("ai_review", True))
                if v.get("translate_gate") in ("wait", "auto"):
                    out["translate_gate"] = v["translate_gate"]
                if isinstance(v.get("parse_interval_sec"), (int, float)):
                    out["parse_interval_sec"] = int(
                        min(60, max(0, v["parse_interval_sec"])))
                out["skip_review_batch"] = bool(v.get("skip_review_batch", False))
                if isinstance(v.get("paddleocr"), dict):
                    # 批1 修复：**空串不覆盖 .env 非空值**——用户只改了解析模式时，
                    # 前端回传的 paddleocr 若为空（例如 .env 里配了 token 但页面未预填），
                    # 旧实现会把 .env 的非空值覆盖成空 ⇒ 辅通道静默失效。
                    out["paddleocr"].update({k: str(val).strip()
                                             for k, val in v["paddleocr"].items()
                                             if k in ("access_token", "base_url",
                                                      "model_version") and str(val).strip()})
            except json.JSONDecodeError:
                pass
        else:
            s = self.app_settings
            if s and s.parse_mode in PARSE_MODES:
                out["mode"] = s.parse_mode       # env 默认（仅无 store 记录时）
            if s:
                out["ai_review"] = s.ai_review
        return out

    @staticmethod
    def _paddleocr_from_env() -> dict:
        """PaddleOCR-VL（百度）辅通道配置：从 .env 读取（用户已填写）+ optionalPayload 参数。"""
        return {"access_token": os.getenv("PADDLEOCR_ACCESS_TOKEN", "").strip(),
                "base_url": os.getenv("PADDLEOCR_BASE_URL",
                                      "https://paddleocr.aistudio-app.com").strip(),
                "model_version": os.getenv("PADDLEOCR_MODEL_VERSION",
                                           "PaddleOCR-VL-1.6").strip(),
                "options": SettingsService._paddleocr_options_from_env()}

    @staticmethod
    def _paddleocr_options_from_env() -> dict:
        """PaddleOCR optionalPayload（restructurePages/mergeTables/relevelTitles）。

        单一判据在 `paperparse.core.parse_params`（默认全开：论文跨页表格 + 标题分级）。
        包不可 import 时（极端环境）回落同形状默认值，避免设置中心读失败。
        """
        raw = os.getenv("PADDLEOCR_OPTIONS", "").strip()
        try:
            from paperparse.core.parse_params import parse_paddleocr_options
            return parse_paddleocr_options(raw)
        except Exception:  # noqa: BLE001
            return {"restructurePages": True, "mergeTables": True, "relevelTitles": True}

    @staticmethod
    def _mineru_params_from_env() -> dict:
        """MinerU 解析质量参数（实时读 .env/os.environ；auto = 智能判定，见 parse_params）。"""
        lang = (os.getenv("MINERU_LANGUAGE", "auto") or "auto").strip().lower()
        is_ocr = (os.getenv("MINERU_IS_OCR", "auto") or "auto").strip().lower()
        table = (os.getenv("MINERU_ENABLE_TABLE", "1") or "1").strip().lower()
        return {"language": lang if lang in MINERU_LANGUAGES else "auto",
                "is_ocr": is_ocr if is_ocr in MINERU_IS_OCR_MODES else "auto",
                "enable_table": table not in ("0", "false", "no", "off")}

    @staticmethod
    def _normalize_mineru_params(raw: dict) -> dict:
        """校验并归一化前端提交的 MinerU 参数（非法值 → 400，不静默改写用户输入）。"""
        raw = raw or {}
        lang = str(raw.get("language", "auto") or "auto").strip().lower()
        if lang not in MINERU_LANGUAGES:
            raise ValueError("language 仅支持 " + "/".join(MINERU_LANGUAGES))
        is_ocr = str(raw.get("is_ocr", "auto") or "auto").strip().lower()
        if is_ocr not in MINERU_IS_OCR_MODES:
            raise ValueError("is_ocr 仅支持 " + "/".join(MINERU_IS_OCR_MODES))
        return {"language": lang, "is_ocr": is_ocr,
                "enable_table": bool(raw.get("enable_table", True))}

    @staticmethod
    def _normalize_paddleocr_options(raw: dict) -> dict:
        """校验并归一化 PaddleOCR optionalPayload（只接受已知键，值 bool）。"""
        raw = raw or {}
        out = {}
        for k, default in DEFAULT_PADDLEOCR_OPTIONS.items():
            out[k] = bool(raw.get(k, default))
        unknown = [k for k in raw if k not in DEFAULT_PADDLEOCR_OPTIONS]
        if unknown:
            raise ValueError("未知的 PaddleOCR 参数: " + ", ".join(sorted(unknown)))
        return out

    def save_parse(self, cfg: dict) -> dict:
        """保存解析设置（SQLite 控制项 + **.env 参数项**）→ 返回回读结果。

        批2：`mineru_params`（language/is_ocr/enable_table）与 `paddleocr.options` 写 `.env`
        + `os.environ`（paperparse 解析时实时读），并**回读断言**；`mineru_api_key` 若在
        本请求里一并提交，则转而走 `save_mineru`（Key 的单一来源仍是 .env）。
        """
        mode = cfg.get("mode")
        if mode not in PARSE_MODES:
            raise ValueError(f"解析模式必须是 {PARSE_MODES}")
        po = cfg.get("paddleocr") or {}
        gate = cfg.get("translate_gate", "wait")
        if gate not in ("wait", "auto"):
            gate = "wait"
        interval = int(cfg.get("parse_interval_sec", 8) or 8)
        payload = {"mode": mode, "ai_review": bool(cfg.get("ai_review", True)),
                   "translate_gate": gate,
                   "parse_interval_sec": min(60, max(0, interval)),
                   "skip_review_batch": bool(cfg.get("skip_review_batch", False)),
                   "paddleocr": {k: str(po.get(k, "")).strip()
                                 for k in ("access_token", "base_url", "model_version")}}
        params = self._normalize_mineru_params(cfg.get("mineru_params") or {})
        options = self._normalize_paddleocr_options(po.get("options") or {})
        self.store.set_setting(KEY_PARSE, json.dumps(payload, ensure_ascii=False))

        expected = {
            "MINERU_LANGUAGE": params["language"],
            "MINERU_IS_OCR": params["is_ocr"],
            "MINERU_ENABLE_TABLE": "1" if params["enable_table"] else "0",
            "PADDLEOCR_OPTIONS": json.dumps(options, ensure_ascii=False),
        }
        readback = {"ok": True, "mismatch": [], "values": {}}
        if self.env_sync:
            # P12-5：PaddleOCR 连接配置按实际填写结果写回（空值跳过 = 不改原值）
            try:
                self._sync_env_paddleocr(payload["paddleocr"])
            except Exception as e:  # noqa: BLE001 - 写 .env 失败不阻塞保存
                logger.warning("写回 .env（PaddleOCR）失败: %s", e)
            self._write_env_keys(expected)
            readback = self._readback_env_keys(expected)
            if not readback["ok"]:
                logger.error("解析参数回读不一致：%s", readback["mismatch"])
        return {"readback": readback, "mineru_params": params,
                "paddleocr_options": options}

    def _sync_env_paddleocr(self, po: dict) -> None:
        """PaddleOCR 配置 → .env PADDLEOCR_*（走统一写回 `_write_env_keys`）。

        语义保持"只写实际填写的项"（空值跳过）：用户在页面留空 = 不改 .env 原值。
        """
        values = {k: str(po.get(k, "")).strip() for k in
                  ("PADDLEOCR_ACCESS_TOKEN", "PADDLEOCR_BASE_URL",
                   "PADDLEOCR_MODEL_VERSION")}
        values = {k: v for k, v in values.items() if v}
        if values:
            self._write_env_keys(values)

    # ---------------------------------------------------------- 系统提示词（V11）
    KEY_SYSTEM_PROMPT = "system_prompt_extra"

    def get_system_prompt_extra(self) -> str:
        return self.store.get_setting(self.KEY_SYSTEM_PROMPT, "")

    def save_system_prompt_extra(self, text: str) -> None:
        self.store.set_setting(self.KEY_SYSTEM_PROMPT, text)

    # ---------------------------------------------------------- 输出模板（T06）
    KEY_MD_TEMPLATE = "md_template"
    MD_TEMPLATES = ("obsidian_bilingual", "obsidian_bilingual_alt", "plain")

    def get_md_template(self) -> str:
        tpl = self.store.get_setting(self.KEY_MD_TEMPLATE, "") or ""
        return tpl if tpl in self.MD_TEMPLATES else "obsidian_bilingual"

    def save_md_template(self, tpl: str) -> None:
        self.store.set_setting(self.KEY_MD_TEMPLATE,
                               tpl if tpl in self.MD_TEMPLATES else "obsidian_bilingual")

    # ---------------------------------------------------------- 外观 CSS（V11）
    KEY_CUSTOM_CSS = "custom_css"

    def get_custom_css(self) -> str:
        return self.store.get_setting(self.KEY_CUSTOM_CSS, "")

    def save_custom_css(self, css: str) -> None:
        self.store.set_setting(self.KEY_CUSTOM_CSS, css)

    # ---------------------------------------------------------- AI检索配置（paperlit）
    def get_lit_config(self) -> dict:
        """获取 AI检索 配置（API key 脱敏，从 .env 读取）。"""
        emb_key = os.environ.get("LIT_EMBEDDING_API_KEY", "")
        rer_key = os.environ.get("LIT_RERANKER_API_KEY", "")
        # 兜底：SILICONFLOW_API_KEY
        if not emb_key:
            emb_key = os.environ.get("SILICONFLOW_API_KEY", "")
        if not rer_key:
            rer_key = os.environ.get("SILICONFLOW_API_KEY", "")
        return {
            "embedding_api_key": _mask(emb_key) if emb_key else "",
            "reranker_api_key": _mask(rer_key) if rer_key else "",
            "embedding_model": os.environ.get("LIT_EMBEDDING_MODEL", "BAAI/bge-m3"),
            "reranker_model": os.environ.get("LIT_RERANKER_MODEL", "BAAI/bge-reranker-v2-m3"),
            "has_embedding_key": bool(emb_key),
            "has_reranker_key": bool(rer_key),
        }

    def save_lit_config(self, embedding_api_key: str = None, reranker_api_key: str = None,
                        embedding_model: str = None, reranker_model: str = None) -> dict:
        """保存 AI检索 配置到 .env（空字符串=清空，None=不修改）。返回回读结果。"""
        env_vals: dict[str, str] = {}
        if embedding_api_key is not None:
            env_vals["LIT_EMBEDDING_API_KEY"] = embedding_api_key.strip()
        if reranker_api_key is not None:
            env_vals["LIT_RERANKER_API_KEY"] = reranker_api_key.strip()
        if embedding_model is not None:
            env_vals["LIT_EMBEDDING_MODEL"] = embedding_model.strip() or "BAAI/bge-m3"
        if reranker_model is not None:
            env_vals["LIT_RERANKER_MODEL"] = reranker_model.strip() or "BAAI/bge-reranker-v2-m3"
        if env_vals:
            self._write_env_keys(env_vals)
        return self._readback_env_keys(env_vals) if env_vals else {"ok": True, "mismatch": []}

    def get_lit_api_keys(self) -> tuple[str, str]:
        """获取 AI检索 API key（明文，内部使用）。返回 (embedding_key, reranker_key)。"""
        emb_key = os.environ.get("LIT_EMBEDDING_API_KEY", "")
        rer_key = os.environ.get("LIT_RERANKER_API_KEY", "")
        if not emb_key:
            emb_key = os.environ.get("SILICONFLOW_API_KEY", "")
        if not rer_key:
            rer_key = os.environ.get("SILICONFLOW_API_KEY", "")
        return (emb_key, rer_key)

    # ---------------------------------------------------------- 汇总
    def get_all(self) -> dict:
        return {
            "providers": self.get_providers(masked=True),
            "active_provider": self.get_active_id(),
            "translation_providers": self.get_translation_providers(masked=True),
            "prices": self.get_prices(),
            "kb_path": self.get_kb_path(),
            "kb_copy_mode": self.get_kb_copy_mode(),
            "retrieval_mode": self.get_retrieval_mode(),
            "mineru": self.get_mineru(),
            "parse": self.get_parse(),
            "auto_exit": self.get_auto_exit(),
            "auto_compile": self.get_auto_compile(),
            "chat_reasoning_effort": self.get_chat_reasoning_effort(),
            "compile_reasoning_effort": self.get_compile_effort(),
            "translate_reasoning_effort": self.get_translate_effort(),
            "system_prompt_extra": self.get_system_prompt_extra(),
            "custom_css": self.get_custom_css(),
            "md_template": self.get_md_template(),
            "lit_config": self.get_lit_config(),
        }
