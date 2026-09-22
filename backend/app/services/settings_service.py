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
MINERU_MODEL_VERSIONS = ("vlm", "pipeline", "mineru-html")
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
# 翻译**每批正文字符上限**（2026-09-22 用户要求可调，方便按模型输出能力取舍）。
# 2026-09-23：**优先用翻译模型条目自带的 `batch_chars`**（安全冗余是模型属性，见
# `get_effective_translate_batch_chars`）；本全局值只在"条目没单独设"或"回落主模型"时生效。
# 留空/0 = 用默认：紧凑路径（专用翻译模型）14000 / 主模型路径 12000。
# 语义硬约束在 paperkb 侧保证：只在段边界切批、单段超限独占一批、超长单段按句切块。
KEY_TRANSLATE_BATCH_CHARS = "translate_batch_chars"
# 下限/上限（防手滑填 0/±天文数字）：1 千 ~ 20 万字符
TRANSLATE_BATCH_CHARS_MIN = 1000
TRANSLATE_BATCH_CHARS_MAX = 200000
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
        # 2026-09-21：智谱 GLM 内置槽位（第二家大模型，用户只填 ZHIPU_API_KEY）。
        # 为什么不用 CUSTOM_PROVIDER_：Windows 下 os.environ 的键**一律大写**，而自定义
        # 供应商的 id 还要再加一层 `p_`，写回前缀永远对不上文件里那组 ⇒ 每存一次就多留
        # 一组同义键。固定大写的 ZHIPU_* 可以原样往返，.env 不会越写越乱。
        zp_key = os.getenv("ZHIPU_API_KEY", "").strip()
        if zp_key:
            presets.append({
                "id": "zhipu", "name": "智谱 GLM",
                "base_url": os.getenv("ZHIPU_BASE_URL",
                                      "https://open.bigmodel.cn/api/paas/v4").strip(),
                "model": os.getenv("ZHIPU_MODEL", "glm-5.3-flash").strip(),
                "api_key": zp_key, "enabled": True, "env": "ZHIPU",
                "max_tokens": int(os.getenv("ZHIPU_MAX_TOKENS") or DEFAULT_MAX_OUTPUT_TOKENS),
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
                if old_p["id"] not in kept_ids and not (old_p.get("env") in ("DEEPSEEK", "SILICONFLOW", "ZHIPU")):
                    try:
                        self._remove_custom_env(old_p["id"])
                    except Exception as e:  # noqa: BLE001
                        logger.warning("清理 .env 失败（%s）: %s", old_p["id"], e)
            for p in cleaned:
                env_prefix = (p.get("env") or "").strip()
                if env_prefix in ("DEEPSEEK", "SILICONFLOW", "ZHIPU"):
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
        "ZHIPU": ("ZHIPU_API_KEY", "ZHIPU_BASE_URL", "ZHIPU_MODEL"),
        "SILICONFLOW": ("SILICONFLOW_API_KEY", "SILICONFLOW_BASE_URL", "SILICONFLOW_MODEL"),
        "QWEN": ("QWEN_API_KEY", "QWEN_BASE_URL", "QWEN_MODEL"),
    }

    def _sync_env_file(self, env_prefix: str, provider: dict) -> None:
        """按实际填写结果更新 .env 对应键（只改**未注释**的 KEY= 行；无该键则追加，
        保留注释与原有顺序），并**同步 os.environ**。

        2026-09-22（用户要求"密钥以 .env 为唯一可见来源"）：原先这里只写文件不同步进程环境
        ⇒ 保存 Key 后本次运行仍用旧值（另一套 `_write_env_keys` 会同步，两套行为不一致）。
        现在启动侧已改为 **.env 覆盖系统环境变量**（见 `config._apply_dotenv_files`），
        这里补齐"写文件即生效"，保证 文件 = os.environ = 程序实际使用值。
        """
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
            for key, val in zip(self._ENV_KEYS[env_prefix], vals):
                os.environ[key] = val        # 运行中进程立即可见（与文件保持一致）

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
        # 2026-09-21 大小写收敛：Windows 下 `os.environ` 的键**一律大写**，而这里按 DB 里的 id
        # 原样写文件 ⇒ 读回时 id 被多加一层 `p_`（`p_zhipu` 读成 `P_ZHIPU` → id `p_P_ZHIPU`），
        # 写回前缀于是跟文件里那组对不上 ⇒ **每次保存都多留一组同义键**（用户 .env 里
        # `CUSTOM_PROVIDER_p_P_*` 与 `CUSTOM_PROVIDER_p_P_P_*` 并存就是这么来的）。
        # 修法：沿用文件里已有的键名大小写，并按**小写化前缀**整组替换。
        ci = prefix.lower()
        canon = next((ln.split("=", 1)[0].strip()[:len(prefix)]
                      for ln in lines if ln.lstrip().lower().startswith(ci)), None)
        if canon:
            prefix = canon
        lines = [ln for ln in lines if not ln.lstrip().lower().startswith(ci)]
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
        ci = prefix.lower()
        # 大小写不敏感：文件里可能是小写 id，运行期 os.environ 里被 Windows 大写化
        lines = [ln for ln in text.splitlines() if not ln.lstrip().lower().startswith(ci)]
        # 运行中清除 os.environ 对应条目
        for key in list(os.environ.keys()):
            if key.lower().startswith(ci):
                del os.environ[key]
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ------------------------------------------------ .env 通用写回 + 回读断言（批1）
    def _write_env_keys(self, values: dict[str, str]) -> None:
        """把 `{KEY: value}` 写入 .env（只改**未注释**的 `KEY=` 行；无该键则追加；文件不存在
        则创建）并同步 `os.environ`（运行中进程立即生效）。**优先级口径（2026-09-22 用户拍板）**：
        `.env` ＞ 系统环境变量，且 .env 里的**空值也覆盖**（在 .env 里清空 Key ⇒ 真的失效）；
        启动侧由 `config._apply_dotenv_files` 统一应用，这里负责"写文件即生效"。

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

    # ---------------------------------------------------------- 翻译模型池（多模型备选，单选激活）
    # 2026-09-19 从单个翻译供应商升级为**池**：`translation_providers` 为 JSON 列表，每个条目
    # 带 `enabled` 标志。
    # 2026-09-22 用户决定：**改为单选**——池里可存多个模型（方便切换），但同一时刻只有一个
    # `enabled`；全部关闭 = 回落主模型。旧语义"启用多个 = 并行轮询提速"已删除：实测它并非
    # 并行（`_run_batches` 是串行循环），只是把各批**分发**给不同模型 ⇒ 同一篇译文风格/术语
    # 不一致。想换模型 = 在设置里点另一个（热切换立即生效）。
    _TRANSLATE_ENV_RE = re.compile(
        r"^TRANSLATE_(\d+)_(BASE_URL|MODEL|API_KEY|MAX_TOKENS|ENABLED|BATCH_CHARS)$")

    def _env_translation_presets(self) -> list[dict]:
        """`.env` 的 `TRANSLATE_<idx>_*` → 翻译池条目（**权威**：见 `get_translation_providers`）。

        - **API_KEY 留空 → 回落 `SILICONFLOW_API_KEY`**（与 `get_lit_api_keys` 同一约定：
          硅基流动一个 Key 同时服务翻译 / embedding / reranker）；
        - **启用但拿不到任何 Key 的条目不播种**——宁可回落主模型，也不要"界面看着启用了、
          实际调不通"；
        - `ENABLED=0` 照原样播进来（界面可见但**不参与路由**），尊重用户显式关闭；
        - `BATCH_CHARS` = 该模型单独的单批正文字符上限（0/缺省 = 用条目默认，见
          `get_effective_translate_batch_chars`）。
        写回侧：界面保存走 `save_translation_providers` → `_sync_translate_env`（按序号
        重写 `TRANSLATE_<idx>_*`，并清掉多余旧键），因此界面保存后两边始终一致。
        """
        groups: dict[int, dict] = {}
        for key, val in os.environ.items():
            m = self._TRANSLATE_ENV_RE.match(key)
            if m:
                groups.setdefault(int(m.group(1)), {})[m.group(2).lower()] = (val or "").strip()
        sf_key = os.environ.get("SILICONFLOW_API_KEY", "").strip()
        out: list[dict] = []
        for idx in sorted(groups):
            f = groups[idx]
            if not (f.get("base_url") and f.get("model")):
                continue
            enabled = str(f.get("enabled") or "1").strip().lower() not in ("0", "false", "no")
            api_key = f.get("api_key") or sf_key
            if enabled and not api_key:
                continue
            try:
                max_tokens = int(f.get("max_tokens") or 8192)
            except ValueError:
                max_tokens = 8192
            try:
                batch_chars = int(float(f.get("batch_chars") or 0))
            except ValueError:
                batch_chars = 0
            if batch_chars > 0:
                batch_chars = min(max(batch_chars, TRANSLATE_BATCH_CHARS_MIN),
                                  TRANSLATE_BATCH_CHARS_MAX)
            out.append({
                "id": f"translate_{idx}",
                "name": f"{f['model'].split('/')[-1]}（.env 翻译预设）",
                "base_url": f["base_url"], "model": f["model"], "api_key": api_key,
                "enabled": enabled, "env": "TRANSLATE",
                "max_tokens": max_tokens, "batch_chars": max(0, batch_chars),
                "reasoning_effort": None,
            })
        return out
    def get_translation_providers(self, masked: bool = True) -> list[dict]:
        """获取翻译供应商池（全部，含未启用的）。[] = 未配置（回落主模型）。

        **权威来源 = `.env` 的 `TRANSLATE_<idx>_*`（2026-09-23 用户拍板）**：与密钥同一规则，
        手改 `.env` 即生效。合并规则按**序号**：
          - 池里第 i 条 ← `.env` 第 i 槽（存在即覆盖，含 ENABLED/KEY/单批上限）；
          - `.env` 没有的序号 → 保留池里原条目；
          - `.env` 多出的序号 → 追加。
        界面保存时 `_sync_translate_env` 会把整池按序号回写 `.env`（并清多余旧键）⇒ 保存后两边
        恒等，故"界面改了却像没生效"不会发生；反之手改 `.env` 就是唯一权威。
        """
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
        providers = self._merge_translate_env_presets(providers)
        if masked:
            out = []
            for p in providers:
                q = dict(p)
                q["api_key"] = _mask(q.get("api_key", ""))
                out.append(q)
            return out
        return providers

    def _merge_translate_env_presets(self, providers: list[dict]) -> list[dict]:
        """`.env` 预设按序号权威覆盖**翻译池**条目（见 `get_translation_providers` 的规则说明）。"""
        presets = self._env_translation_presets()
        if not presets:
            return providers
        by_idx = {p["id"]: p for p in presets}   # id = "translate_<idx>"
        merged: list[dict] = []
        for i, p in enumerate(providers):
            key = f"translate_{i}"
            if key in by_idx:
                merged.append(by_idx.pop(key))
            else:
                merged.append(p)
        for key in sorted(by_idx, key=lambda k: int(k.split("_")[1])):
            merged.append(by_idx[key])
        return merged

    def get_enabled_translation_providers(self, masked: bool = False) -> list[dict]:
        """池内**已启用**的翻译供应商（实际参与翻译路由的）。

        masked=False（= 运行时口径，喂给 `build_ai`）时把每条的 `max_tokens` 换成**按该条目单批
        上限推导**的值（`llm_service.translate_output_budget`）：批次上限调大 ⇒ 输出预算跟着走。
        只在读取时推导、**不落库**，故界面（masked=True）看到的仍是用户存的原值。
        """
        pool = [p for p in self.get_translation_providers(masked=masked)
                if p.get("enabled")]
        if masked:
            return pool
        from .llm_service import translate_output_budget
        global_batch = self.get_translate_batch_chars()
        return [{**p, "max_tokens": translate_output_budget(
                    self._entry_batch_chars(p, global_batch), p.get("max_tokens"))}
                for p in pool]

    @staticmethod
    def _entry_batch_chars(entry: dict, global_batch: int = 0) -> int:
        """条目自己的单批上限（0/缺省 ⇒ 用全局设置，仍为 0 ⇒ 交给 paperkb 的代码默认）。"""
        try:
            own = int(entry.get("batch_chars") or 0)
        except (TypeError, ValueError):
            own = 0
        return own if own > 0 else int(global_batch or 0)

    def get_effective_translate_batch_chars(self) -> int:
        """翻译时实际用的单批正文字符上限：激活条目自带值 → 全局设置 → 0（用代码默认）。

        条目值优先是因为**安全冗余是模型属性**：换模型即换该模型自己的实测值，不必重填全局。
        """
        batch = self.get_translate_batch_chars()
        for p in self.get_translation_providers(masked=False):
            if p.get("enabled"):
                return self._entry_batch_chars(p, batch)
        return batch

    def save_translation_providers(self, providers: list[dict]) -> None:
        """保存翻译供应商池（整体替换；脱敏 key 保留原值；补 id/enabled 缺省）。

        2026-09-22 用户决定：**池可存多条，但同时只有一条生效**（用户在设置里单选激活哪个）。
        旧语义是"启用多个 = 按批轮询分发"，实测会让同一篇译文各批落到不同模型 ⇒ 风格/术语
        不一致，且并非真并行（`_run_batches` 是串行循环）。此处兜底：多条 enabled 只留第一条，
        其余强制关闭；**全部关闭是合法的**（= 回落主模型翻译）。

        2026-09-23：条目自带 `batch_chars`（该模型的单批正文字符上限，0 = 用默认/全局）——
        安全冗余是模型属性，故随模型一起存；保存后经 `_sync_translate_env` 回写 `.env`。
        """
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
            p["batch_chars"] = self._clamp_batch_chars(p.get("batch_chars"))
            cleaned.append(p)
        seen_active = False
        for p in cleaned:
            if p["enabled"] and seen_active:
                logger.info("翻译池单选兜底：%s 与已激活项并存 → 强制关闭", p["id"])
                p["enabled"] = False
            elif p["enabled"]:
                seen_active = True
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

    @staticmethod
    def _clamp_batch_chars(raw) -> int:
        """条目单批上限归一化：空/0/非法 → 0（用默认）；否则钳 [MIN, MAX]。"""
        if raw in (None, ""):
            return 0
        try:
            val = int(float(raw))
        except (TypeError, ValueError):
            return 0
        if val <= 0:
            return 0
        return min(max(val, TRANSLATE_BATCH_CHARS_MIN), TRANSLATE_BATCH_CHARS_MAX)

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
            batch_chars = self._clamp_batch_chars(p.get("batch_chars"))
            enabled = "1" if p.get("enabled") else "0"
            if base_url:
                lines.append(f"TRANSLATE_{idx}_BASE_URL={base_url}")
            if model:
                lines.append(f"TRANSLATE_{idx}_MODEL={model}")
            if api_key:
                lines.append(f"TRANSLATE_{idx}_API_KEY={api_key}")
            if max_tokens:
                lines.append(f"TRANSLATE_{idx}_MAX_TOKENS={max_tokens}")
            if batch_chars:
                lines.append(f"TRANSLATE_{idx}_BATCH_CHARS={batch_chars}")
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
            batch_chars = self._clamp_batch_chars(p.get("batch_chars"))
            if batch_chars:
                os.environ[f"TRANSLATE_{idx}_BATCH_CHARS"] = str(batch_chars)
            else:
                os.environ.pop(f"TRANSLATE_{idx}_BATCH_CHARS", None)

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

    # ---------------------------------------------------------- 翻译批次上限（2026-09-22）
    def get_translate_batch_chars(self) -> int:
        """每批正文字符上限；**留空/0 = 0**，表示用默认（紧凑 14000 / 主模型 12000）。"""
        raw = (self.store.get_setting(KEY_TRANSLATE_BATCH_CHARS) or "").strip()
        try:
            val = int(raw)
        except (TypeError, ValueError):
            return 0
        if val <= 0:
            return 0
        return min(max(val, TRANSLATE_BATCH_CHARS_MIN), TRANSLATE_BATCH_CHARS_MAX)

    def save_translate_batch_chars(self, chars: int | str | None) -> int:
        """保存批次上限（字符）。空/0 = 清除（回落默认）；越界钳到
        [1000, 200000]；非数字抛 ValueError（API 层转 400）。"""
        raw = "" if chars is None else str(chars).strip()
        if raw in ("", "0"):
            self.store.set_setting(KEY_TRANSLATE_BATCH_CHARS, "")
            return 0
        try:
            val = int(float(raw))
        except (TypeError, ValueError):
            raise ValueError("翻译批次上限必须是正整数（字符数）或留空") from None
        if val <= 0:
            self.store.set_setting(KEY_TRANSLATE_BATCH_CHARS, "")
            return 0
        val = min(max(val, TRANSLATE_BATCH_CHARS_MIN), TRANSLATE_BATCH_CHARS_MAX)
        self.store.set_setting(KEY_TRANSLATE_BATCH_CHARS, str(val))
        return val

    # ---------------------------------------------------------- 批次上限探测（2026-09-22）
    # 2026-09-23 简化：探测结果**不再落盘**——单批上限现在是模型条目自己的字段，探测按钮就在
    # 该模型表单里、结果直接回填输入框，不需要"上次建议值"这类全局记忆（少一处状态少一处困惑）。
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
        model_version = (os.getenv("MINERU_MODEL_VERSION", "vlm") or "vlm").strip().lower()
        lang = (os.getenv("MINERU_LANGUAGE", "auto") or "auto").strip().lower()
        is_ocr = (os.getenv("MINERU_IS_OCR", "auto") or "auto").strip().lower()
        table = (os.getenv("MINERU_ENABLE_TABLE", "1") or "1").strip().lower()
        upload_path = (os.getenv("MINERU_UPLOAD_PATH", "/file-urls/batch") or "/file-urls/batch").strip()
        submit_path = (os.getenv("MINERU_SUBMIT_PATH", "/extract/task") or "/extract/task").strip()
        poll_path = (os.getenv("MINERU_POLL_PATH", "/extract/task/{task_id}") or "/extract/task/{task_id}").strip()
        batch_results_path = (os.getenv("MINERU_BATCH_RESULTS_PATH", "/extract-results/batch/{batch_id}")
                              or "/extract-results/batch/{batch_id}").strip()
        return {"model_version": model_version if model_version in MINERU_MODEL_VERSIONS else "vlm",
                "language": lang if lang in MINERU_LANGUAGES else "auto",
                "is_ocr": is_ocr if is_ocr in MINERU_IS_OCR_MODES else "auto",
                "enable_table": table not in ("0", "false", "no", "off"),
                "api_paths": {"upload": upload_path, "submit": submit_path,
                              "poll": poll_path, "batch_results": batch_results_path}}

    @staticmethod
    def _normalize_mineru_params(raw: dict) -> dict:
        """校验并归一化前端提交的 MinerU 参数（非法值 → 400，不静默改写用户输入）。"""
        raw = raw or {}
        model_version = str(raw.get("model_version", "vlm") or "vlm").strip().lower()
        if model_version not in MINERU_MODEL_VERSIONS:
            raise ValueError("model_version 仅支持 " + "/".join(MINERU_MODEL_VERSIONS))
        lang = str(raw.get("language", "auto") or "auto").strip().lower()
        if lang not in MINERU_LANGUAGES:
            raise ValueError("language 仅支持 " + "/".join(MINERU_LANGUAGES))
        is_ocr = str(raw.get("is_ocr", "auto") or "auto").strip().lower()
        if is_ocr not in MINERU_IS_OCR_MODES:
            raise ValueError("is_ocr 仅支持 " + "/".join(MINERU_IS_OCR_MODES))
        api_paths = raw.get("api_paths") or {}
        upload_path = str(api_paths.get("upload", "/file-urls/batch") or "/file-urls/batch").strip()
        submit_path = str(api_paths.get("submit", "/extract/task") or "/extract/task").strip()
        poll_path = str(api_paths.get("poll", "/extract/task/{task_id}") or "/extract/task/{task_id}").strip()
        batch_results_path = str(api_paths.get("batch_results", "/extract-results/batch/{batch_id}")
                                 or "/extract-results/batch/{batch_id}").strip()
        return {"model_version": model_version, "language": lang, "is_ocr": is_ocr,
                "enable_table": bool(raw.get("enable_table", True)),
                "api_paths": {"upload": upload_path, "submit": submit_path,
                              "poll": poll_path, "batch_results": batch_results_path}}

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
            "MINERU_MODEL_VERSION": params["model_version"],
            "MINERU_LANGUAGE": params["language"],
            "MINERU_IS_OCR": params["is_ocr"],
            "MINERU_ENABLE_TABLE": "1" if params["enable_table"] else "0",
            "MINERU_UPLOAD_PATH": params["api_paths"]["upload"],
            "MINERU_SUBMIT_PATH": params["api_paths"]["submit"],
            "MINERU_POLL_PATH": params["api_paths"]["poll"],
            "MINERU_BATCH_RESULTS_PATH": params["api_paths"]["batch_results"],
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
            "translate_batch_chars": self.get_translate_batch_chars(),
            "system_prompt_extra": self.get_system_prompt_extra(),
            "custom_css": self.get_custom_css(),
            "md_template": self.get_md_template(),
            "lit_config": self.get_lit_config(),
        }
