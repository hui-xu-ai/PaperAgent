# -*- coding: utf-8 -*-
"""LLM 服务层：DeepSeek API 客户端（翻译/总结/对话）。

T02：
- `DeepSeekAI(AIProvider)`：实现引擎的 AIProvider 抽象，供 translate_pdf /
  summarize_pdf / qna 等引擎内部调用（单次大上下文，前缀缓存命中）。
- `chat_stream()`：对话流式补全（SSE 消费）。
- `init_llm()`：创建 provider 并 `set_ai` 注入引擎（启动时调用一次）。

Token 纪律：翻译/总结走 complete()（非流式、确定性低温度）；
对话走 chat_stream()（流式）。两者共用同一 client。
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from paperparse.llm.client import AIProvider, set_ai
from paperkb.context import split_task

from ..config import Settings

logger = logging.getLogger(__name__)

# deepseek-chat 单次最大输出 token（API 限制）
# P（翻译截断修复）：8192 → 16384——单批译文（~10k 字符）常见 output >8192 被截成
# "半个 JSON" 导致 _parse_json 失败（重试同上限仍截断）。
# 整篇一次（pipeline MAX_WHOLE_CHARS=400000）：模型支持 1M 上下文 + ~128K 输出，
# 默认上限再提到 64000，让整篇译文（单次调用）不被 max_tokens 截断；分批时也足够。
# 供应商 max_tokens 可配置（GUI 表单/ProviderModel/DB/.env，env 用 *_MAX_TOKENS 覆盖），默认 64000。
DEFAULT_MAX_OUTPUT_TOKENS = 64000

# ---------------------------------------------------------------- reasoning_effort（思考强度）
# 用户决策：GLM 始终思考不能关，但请求不传 reasoning_effort 会自由深度思考，推理 token 全计入
# 输出（如 32462 输出/3566 输入）。策略：按任务 context 设 reasoning_effort——翻译类 low（省 token、
# 快速），编译/汇总类 high（深度推理）。DeepSeek-chat 不思考（可不送/送了也无碍）。
# 取值顺序：供应商显式配置 `reasoning_effort`（GUI/DB/provider 级）优先；未配置则按 context 自动映射。
# 仅对"支持该参数"的供应商/模型发送（见 REASONING_MODEL_HINTS + 显式配置），避免给不支持的端点报错。
DEFAULT_REASONING_EFFORTS: dict[str, str] = {
    # 翻译/翻译总结类：低思考（省输出 token；GLM 始终思考 → 强制 low 防自由深挖）
    "translate": "low",
    "summary": "low",
    # 编译/L1-L3/汇总类：需深度推理 → high
    "compile": "high",
    "summarize": "high",
    "l1": "high",
    "l2": "high",
    "l3": "high",
}

# 批3：**编译族**上下文标签——这些 context 的思考档由设置中心「编译思考档」决定
# （`auto` = 不发送参数 = 服务端自适应；none/minimal/low/medium/high = 显式下发）。
# 其余上下文（chat / engine 单发调用）不受该设置影响，保持既有行为。
COMPILE_EFFORT_CONTEXTS = ("compile", "l1", "l2", "l3", "summarize", "summary")
# 批3：**翻译族**上下文标签——思考档由设置中心「翻译思考档」决定（同样 auto=不发送）。
TRANSLATE_EFFORT_CONTEXTS = ("translate",)

# 模型名命中这些关键字 → 判定为"思考型"（reasoning_effort 参数有意义），才发送该参数。
# 保守启发式：不覆盖其它模型（deepseek-chat 等不命中 → 不发送 reasoning_effort，防端点报错）。
REASONING_MODEL_HINTS = ("glm", "reason", "thinking", "qwq", "moonshot", "kimi")

# 魔塔空信封（限流占位）重试前的喘息秒数（测试可 monkeypatch 为 0）
_RETRY_SLEEP_SEC = 2.0


def cache_hit_tokens(usage) -> int:
    """从各家 `usage` 里取**前缀缓存命中** token 数（2026-09-13 直打三家 API 实测）。

    用户报障：「DeepSeek 有缓存命中，智谱 glm-flash 和硅基流动的 DeepSeek 显示 0」。实测三家形状不同：
      · **DeepSeek 官方**：`prompt_cache_hit_tokens`（同时给 `prompt_tokens_details.cached_tokens`）；
      · **智谱 GLM（OpenAI 兼容）**：**只给** `prompt_tokens_details.cached_tokens`，
        **完全没有** `prompt_cache_hit_tokens` ⇒ 旧实现只读后者，于是**永远记 0**（**本应用解析 bug**）；
      · **硅基流动**：字段与 DeepSeek 同形，但它有**最小缓存块**——实测 ≈1000 token 前缀命中 0、
        ≈4000 token 前缀命中 3840 ⇒ 短前缀不命中属正常，不是解析问题。
    取值顺序：`prompt_cache_hit_tokens` → `cached_tokens` → `cache_read_input_tokens`（备用）
    → `prompt_tokens_details.cached_tokens`。dict（原始 JSON）与 SDK 对象两种形态都支持。
    """
    if usage is None:
        return 0

    def _get(key):
        if isinstance(usage, dict):
            return usage.get(key)
        return getattr(usage, key, None)

    for key in ("prompt_cache_hit_tokens", "cached_tokens", "cache_read_input_tokens"):
        val = _get(key)
        if val:
            try:
                return int(val)
            except (TypeError, ValueError):
                pass
    details = _get("prompt_tokens_details")
    if details is not None:
        val = (details.get("cached_tokens") if isinstance(details, dict)
               else getattr(details, "cached_tokens", None))
        try:
            return int(val or 0)
        except (TypeError, ValueError):
            return 0
    return 0


# ---------------------------------------------------------------- 请求体调试探针（env 开关）
# TRAPS §22：智谱前缀缓存只认 system 消息，形状已改对、供应商侧直打也命中，但应用仍恒 0 ⇒
# 唯一未验证的环节是**应用真正发出去的请求体**。这里加一次性排障日志：默认关闭（零副作用），
# 置 PAPERAGENT_LOG_REQUESTS=1 后每次调用落一行 `[llm-req]`，外加一条 `[llm-usage]` 原文 usage。
# 安全：**只打 role / 长度 / sha256 / 是否含 marker，不打 prompt 正文**（`_DBG_PREVIEW=1` 才给
# 上下文两侧各 60 字符，且已把换行折成 ⏎）。
def _req_debug_enabled() -> bool:
    import os
    return (os.getenv("PAPERAGENT_LOG_REQUESTS") or "").strip().lower() in (
        "1", "true", "yes", "on")


def _blob_sha(text: str) -> str:
    import hashlib
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def _first_diff_pos(a: str, b: str) -> int:
    """首个不同字符位置；完全相同 → -1。"""
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i
    return -1 if len(a) == len(b) else min(len(a), len(b))


def _dbg_slice(a: str, b: str, pos: int) -> str:
    import os
    if (os.getenv("PAPERAGENT_LOG_REQUESTS_PREVIEW") or "").strip() not in (
            "1", "true", "yes", "on"):
        return ""
    lo = max(0, pos - 60)
    return (f"  A@{lo}: {(a[lo:lo + 120] or '')!r}".replace("\\n", "⏎")
            + f"\n  B@{lo}: {(b[lo:lo + 120] or '')!r}".replace("\\n", "⏎"))


def debug_request(where: str, model: str, messages: list[dict], context: str = "",
                  extra: dict | None = None) -> str:
    """按 env 开关打印**真实请求体指纹**：每条消息 role/长度/sha256 + 是否含任务 marker，
    并与**同 key 上一次**调用对比（相同 → 命中缓存的条件成立）。

    只在 `PAPERAGENT_LOG_REQUESTS` 打开时动作；异常一律吞掉（排障日志绝不打断业务）。
    返回指纹串（同 key 对比用），供调用方与响应 usage 一起打。
    """
    if not _req_debug_enabled():
        return ""
    try:
        from paperkb.context import TASK_MARK

        parts, sys_text, sys_role = [], "", ""
        for m in messages:
            c = m.get("content") or ""
            if not isinstance(c, str):
                c = str(c)
            role = m.get("role") or "?"
            parts.append(f"{role}:len={len(c)},sha={_blob_sha(c)},mark={TASK_MARK in c}")
            if role == "system" and not sys_text:
                sys_text, sys_role = c, role
        e = extra or {}
        logger.warning("[llm-req] %s ctx=%s model=%s msgs=%d [%s] effort=%s max_tokens=%s%s",
                       where, context, model, len(messages), " | ".join(parts),
                       e.get("effort"), e.get("max_tokens"),
                       f" {e}" if e else "")
        digest = "|".join(parts)
        key = f"{sys_role}:{context}"
        prev = _LAST_SYS.get(key)
        _LAST_SYS[key] = (digest, sys_text)
        if prev:
            prev_digest, prev_text = prev
            if prev_digest == digest:
                logger.warning("[llm-req] 同 key(%s) 指纹与上次**完全相同** ⇒ 缓存条件成立", key)
            else:
                pos = _first_diff_pos(prev_text, sys_text)
                logger.warning(
                    "[llm-req] ⚠ 同 key(%s) system 内容**不同**：第一个差异字符位置=%s "
                    "（len %d→%d）%s", key, pos, len(prev_text), len(sys_text),
                    _dbg_slice(prev_text, sys_text, pos) if pos >= 0 else "")
        return digest
    except Exception:  # noqa: BLE001 - 排障日志绝不影响调用
        return ""


def debug_usage(where: str, context: str, model: str, usage, cache_hit: int,
                extra: dict | None = None) -> None:
    """按 env 开关打印**响应 usage 原文**（含 hit/miss token 与 finish/错误），排障用。"""
    if not _req_debug_enabled():
        return
    try:
        detail = ""
        if isinstance(usage, dict):
            detail = (f"prompt_cache_hit_tokens={usage.get('prompt_cache_hit_tokens')} "
                      f"cached_tokens={(usage.get('prompt_tokens_details') or {}).get('cached_tokens')} ")
        elif usage is not None:
            detail = ""
        logger.warning("[llm-usage] %s ctx=%s model=%s hit=%s %sprompt=%s completion=%s%s",
                       where, context, model, cache_hit, detail,
                       getattr(usage, "prompt_tokens", None) if not isinstance(usage, dict)
                       else usage.get("prompt_tokens"),
                       getattr(usage, "completion_tokens", None) if not isinstance(usage, dict)
                       else usage.get("completion_tokens"),
                       f" {extra}" if extra else "")
    except Exception:  # noqa: BLE001 - 排障日志绝不影响调用
        return


_LAST_SYS: dict[str, tuple[str, str]] = {}


class DeepSeekError(Exception):
    """LLM 调用失败（重试耗尽后抛出）。"""


class TokenBudgetExceeded(Exception):
    """Token 预算超限（防护红线触发）：任务应 fail 并反馈用户。"""

    def __init__(self, message: str, context: str = ""):
        super().__init__(message)
        self.context = context


class TokenGuard:
    """LLM 调用防护层（红线机制）。

    职责：
    1. 调用前检查：单次输入大小 + 调用次数 + 累计输入（分 context 规则）
    2. 调用后 usage 记录（真实 token + 费用 → llm_usage 表）
    3. 重试预告：大上下文重试前发事件，让用户可见消耗

    context 分级（V02 实测校准）：
    - engine 是**分步小上下文**多次调用（每次 4-14k tokens，缓存命中>99%），
      非死循环 → engine 上限放宽；
    - 真正死循环的特征是累计输入快速膨胀 → 累计输入字符上限拦截。
    """

    MAX_INPUT_CHARS_PER_CALL = 400_000  # 单次输入字符上限（防 prompt 异常膨胀）

    # context 前缀 → 限制规则（实测校准）
    CONTEXT_LIMITS = {
        "engine":  {"max_calls": 12, "max_total_input_chars": 800_000},
        "arbitration": {"max_calls": 8, "max_total_input_chars": 600_000},
        # M3b：paperkb 全文翻译（分批译文可达数十次；用户主动任务，任务粒度由
        # translate_now 前置 reset_context("translate") 控制）
        "translate": {"max_calls": 80, "max_total_input_chars": 3_000_000},
        # M4：paperkb 问答（用户高频交互；独立组防与后台任务互挤）
        "ask": {"max_calls": 200, "max_total_input_chars": 5_000_000},
        "default": {"max_calls": 6,  "max_total_input_chars": 300_000},
    }

    def __init__(self, event_bus=None, usage_service=None,
                 max_input_chars_per_call: int | None = None):
        self.event_bus = event_bus
        self.usage_service = usage_service
        self.max_input_chars = max_input_chars_per_call or self.MAX_INPUT_CHARS_PER_CALL
        # context -> {count, prompt_chars, prompt_tokens, completion_tokens}
        self._calls: dict[str, dict] = {}

    def _limits_for(self, context: str) -> dict:
        for prefix, limits in self.CONTEXT_LIMITS.items():
            if context.startswith(prefix):
                return limits
        return self.CONTEXT_LIMITS["default"]

    def reset_context(self, context: str) -> None:
        """重置某 context 的防护计数（V13：翻译任务开始前按任务粒度清零，
        避免跨论文累计导致第二篇论文翻译被误拦——engine 计数原为服务进程
        生命周期全局累计）。"""
        self._calls.pop(context, None)

    # ---------------------------------------------------------- 调用前
    def begin_call(self, context: str, input_chars: int) -> None:
        """调用前检查：单次输入大小 + 累计输入 + 调用次数红线。"""
        if input_chars > self.max_input_chars:
            self._alert("error", "guard",
                        f"单次输入过大（{input_chars} 字符 > 上限 {self.max_input_chars}），已拦截",
                        {"context": context, "input_chars": input_chars})
            raise TokenBudgetExceeded(
                f"单次输入过大（{input_chars} 字符），超出防护上限，已拦截。", context=context)
        limits = self._limits_for(context)
        rec = self._calls.setdefault(context, {"count": 0, "prompt_chars": 0,
                                               "prompt_tokens": 0,
                                               "completion_tokens": 0})
        # 累计输入检查（防死循环：重复大上下文快速膨胀）
        if rec["prompt_chars"] + input_chars > limits["max_total_input_chars"]:
            self._alert("error", "guard",
                        f"{context} 累计输入异常（{rec['prompt_chars'] + input_chars} 字符 > "
                        f"上限 {limits['max_total_input_chars']}），疑似重复调用，已拦截",
                        {"context": context, "total_chars": rec["prompt_chars"] + input_chars})
            raise TokenBudgetExceeded(
                f"{context} 累计输入超过防护上限（{limits['max_total_input_chars']} 字符），"
                f"疑似重复/循环调用，已拦截。", context=context)
        # 调用次数检查
        rec["count"] += 1
        if rec["count"] > limits["max_calls"]:
            self._alert("error", "guard",
                        f"检测到异常高频 LLM 调用：{context} 第 {rec['count']} 次 "
                        f"（上限 {limits['max_calls']}，红线）",
                        {"context": context, "count": rec["count"]})
            raise TokenBudgetExceeded(
                f"{context} 已调用 {rec['count']} 次，超过防护上限 "
                f"{limits['max_calls']} 次，已拦截。", context=context)
        rec["prompt_chars"] += input_chars

    # ---------------------------------------------------------- 调用后
    def record_usage(self, context: str, prompt_tokens: int,
                     completion_tokens: int, provider: str = "",
                     model: str = "", cache_hit_tokens: int = 0) -> None:
        """记录真实 usage（V02：落库 llm_usage + 事件 + 费用）。"""
        rec = self._calls.setdefault(context, {"count": 0, "prompt_tokens": 0,
                                               "completion_tokens": 0})
        rec["prompt_tokens"] += prompt_tokens
        rec["completion_tokens"] += completion_tokens
        if self.usage_service:
            self.usage_service.record(context, provider, model,
                                      prompt_tokens, completion_tokens,
                                      cache_hit_tokens)
        else:
            self._alert("info", "usage",
                        f"{context}: +{prompt_tokens} 输入 / +{completion_tokens} 输出 "
                        f"（累计 {rec['prompt_tokens']}/{rec['completion_tokens']}）",
                        {"context": context, "prompt_tokens": prompt_tokens,
                         "completion_tokens": completion_tokens})

    # ---------------------------------------------------------- 重试预告
    def report_retry(self, context: str, est_input_chars: int) -> None:
        """大上下文重试前预告（用户可见消耗）。"""
        self._alert("warning", "retry",
                    f"{context} 将重试（预计额外输入约 {est_input_chars} 字符 token）",
                    {"context": context, "est_input_chars": est_input_chars})

    # ---------------------------------------------------------- 内部
    def _alert(self, level: str, category: str, message: str, data: dict) -> None:
        if self.event_bus:
            self.event_bus.publish(level, "llm", category, message, data)
        elif level == "error":
            logger.error("%s", message)


class DeepSeekAI(AIProvider):
    """OpenAI 兼容供应商的引擎 AI provider（V03 泛化）：complete(prompt) -> str。

    支持任意 OpenAI 兼容端点（DeepSeek 官方 / 硅基流动 / 其他）。
    特点：
    - 单次大上下文调用（同一论文同会话 → 前缀缓存命中，省约 30 倍）
    - 低 temperature 保证翻译/总结的确定性与一致性
    - 输出被 max_tokens 截断时记录告警（不静默吞掉）
    - **TokenGuard 防护**（红线）：调用前检查、真实 usage 记账、重试预告
    - **重试白名单**：仅网络类错误重试（≤1 次），业务错误直接失败
    """

    name = "deepseek"  # AIProvider 协议兼容名

    def __init__(self, api_key: str, base_url: str, model: str,
                 timeout_sec: int = 600, max_retries: int = 1,
                 max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
                 temperature: float = 0.3, guard: TokenGuard | None = None,
                 provider_id: str = "deepseek", provider_name: str = "DeepSeek",
                 reasoning_effort: str | None = None):
        if not api_key:
            raise DeepSeekError("未配置 API Key（请在设置中心配置供应商）")
        # 兼容：openai SDK 仅用于 /api/settings/test 的连接测试；主调用走 requests 直连
        # （魔塔等端点非流式也返回 delta 信封，SDK 的 message 解析会 NoneType 崩溃）
        try:
            from openai import OpenAI
        except ImportError as e:  # pragma: no cover
            raise DeepSeekError(f"缺少 openai 依赖: {e}") from e
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout_sec)
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self.timeout_sec = timeout_sec
        self.model = model
        self.max_retries = max_retries
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.guard = guard
        self.provider_id = provider_id
        self.provider_name = provider_name
        # reasoning_effort：供应商显式配置（provider 级，优先）或 None（走 context 自动映射）。
        # _reasoning_supported：该 supplier/model 是否支持/需要 reasoning_effort——显式配置或模型名
        # 命中思考型提示（GLM 等）才发送，避免给 DeepSeek-chat 等不支持的端点传参报错。
        self.reasoning_effort = reasoning_effort
        self._reasoning_supported = bool(reasoning_effort) or any(
            h in (model or "").lower() for h in REASONING_MODEL_HINTS)
        # P12-4：魔塔免费额度失败提示（用户可在设置中心切换供应商）
        self._hint = ("（魔塔免费额度/Key 问题？请在 设置中心-模型 切换供应商）"
                      if "modelscope" in (base_url or "").lower() else "")

    # ------------------------------------------- 思考档决策（批3：编译/翻译思考档）
    @staticmethod
    def _task_effort_setting(ctx: str) -> str:
        """按上下文标签取对应「思考档」设置（编译族 / 翻译族）。

        容器未初始化（CLI/单测）或取不到 → auto（= 回落既有映射，行为不变）。
        """
        try:
            from .container import get_settings_service
            svc = get_settings_service()
            if ctx in TRANSLATE_EFFORT_CONTEXTS:
                return svc.get_translate_effort()
            return svc.get_compile_effort()
        except Exception:  # noqa: BLE001 - 取不到设置按"自动"，绝不打断翻译/编译
            return "auto"

    def _resolve_effort(self, ctx: str) -> tuple[str | None, bool]:
        """context → (reasoning_effort, 是否**显式**指定)。

        优先级：供应商级 `reasoning_effort`（DB/构造） > 任务思考档**显式档位** > 既有 context 映射。
        - 编译族/翻译族设为 `auto`（默认）⇒ **回落既有 `DEFAULT_REASONING_EFFORTS` 映射**
          （兼容旧行为：GLM 等 hints 命中的模型照旧发送 compile=high / translate=low；
          deepseek-flash 因 hints 不匹配 ⇒ 不发送 = 服务端自适应，即历史行为）；
        - 设为 none/minimal/low/medium/high ⇒ `explicit=True` ⇒ 调用方照发（覆盖映射）。
        """
        if self.reasoning_effort:
            return self.reasoning_effort, True
        if ctx in COMPILE_EFFORT_CONTEXTS or ctx in TRANSLATE_EFFORT_CONTEXTS:
            lvl = (self._task_effort_setting(ctx) or "auto").strip().lower()
            if lvl != "auto":
                return lvl, True
            return DEFAULT_REASONING_EFFORTS.get(ctx), False
        return DEFAULT_REASONING_EFFORTS.get(ctx), False

    def complete(self, prompt: str, context: str = "engine",
                 effort_context: str | None = None) -> str:
        """翻译/总结单次调用（带 TokenGuard 防护与重试白名单）。

        P12：requests 直连（兼容魔塔 delta 信封 + 思考型 reasoning_content 回退）；
        context 用于防护计数/审计（同一引擎任务共享，异常高频会触发红线）。
        批3：`effort_context` = **思考档决策用的真实上下文**（编译/翻译经 kbmeta 折成 context="engine"
        只为 TokenGuard 分组，若不额外传入，`DEFAULT_REASONING_EFFORTS` 永远取不到 compile/translate
        ⇒ 档位形同不存在，实测所有编译都跑在服务端默认重思考）。
        批5：prompt 带 `paperkb.context.TASK_MARK` 时按 marker 拆成 `[system(共享全文前缀), user(任务)]`
        两条消息（智谱前缀缓存只认 system 内容）；不带 marker 时保持单条 user（旧行为不变）。
        """
        if self.guard:
            self.guard.begin_call(context, len(prompt))
        # 共享全文前缀 / 任务 分界（2026-09-13 用户实测）：智谱 GLM 的自动前缀缓存**只对
        # `system` 消息内容生效**（单条 user 装前缀 cached_tokens 恒 0；system+user 才命中），
        # DeepSeek 官方与硅基流动则两种形状都命中。⇒ 构造点（compile/translate）用
        # `paperkb.context.with_task` 标界，这里按 marker 拆分：命中则发 [system, user]
        # 两条（共享全文块独立成 system，三家都能命中缓存），未命中维持旧单条 user。
        _sys, _user = split_task(prompt)
        if _sys:
            _messages = [{"role": "system", "content": _sys},
                         {"role": "user", "content": _user}]
        else:
            _messages = [{"role": "user", "content": _user}]
        debug_request("complete", self.model, _messages, context=str(context),
                      extra={"effort_ctx": effort_context or context})
        import requests as _req
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                if attempt > 0 and self.guard:
                    self.guard.report_retry(context, len(prompt))  # 重试消耗预告
                # reasoning_effort 决策（批3）：供应商级显式配置 > 编译族设置 > 既有 context 映射
                effort, explicit = self._resolve_effort(effort_context or context)
                payload = {"model": self.model,
                           "messages": _messages,
                           "temperature": self.temperature,
                           "max_tokens": self.max_tokens}
                # 显式档位（供应商配置/设置中心）**照发**——即使模型名不在 hints 里；
                # 只有"由 context 映射推导"的档位才受 hints 门禁约束（防误发给不支持的端点）。
                if effort and (explicit or self._reasoning_supported):
                    payload["reasoning_effort"] = effort
                resp = _req.post(
                    self._base_url + "/chat/completions",
                    headers={"Authorization": "Bearer " + self._api_key,
                             "Content-Type": "application/json"},
                    json=payload,
                    timeout=self.timeout_sec)
                resp.raise_for_status()
                data = resp.json()
                try:
                    ch = data["choices"][0]
                except (KeyError, IndexError, TypeError) as e:
                    choices = data.get("choices")
                    # P12 反馈：魔塔对免费额度耗尽/限流返回 200 + 空信封
                    # （choices: None + id 空 + usage 全 0）→ **可重试**（喘息后重试 1 次）
                    if choices is None or choices == []:
                        last_err = DeepSeekError(
                            f"空信封（choices={choices}，魔塔免费额度可能耗尽或限流）")
                        logger.warning("DeepSeek complete 空信封 attempt=%d/%d: %s",
                                       attempt + 1, self.max_retries + 1,
                                       str(data)[:120])
                        if attempt < self.max_retries:
                            import time as _time
                            _time.sleep(_RETRY_SLEEP_SEC)   # 限流喘息
                        continue
                    raise DeepSeekError(f"响应结构异常: {str(data)[:200]}") from e
                # 兼容 message / 流式 delta 双信封 + 思考型 reasoning_content 回退
                msg = ch.get("message") or ch.get("delta") or {}
                text = str(msg.get("content") or "").strip()
                if not text:
                    text = str(msg.get("reasoning_content") or "").strip()
                # P12F：思考型模型可能把 <think>...</think> 残留混进 content
                # （实测硅基流动 V4-Flash 返回 'OK</think>OK'）→ 清洗，防 JSON 解析污染
                import re as _re2
                text = _re2.sub(r"</?think>", "", text).strip()
                if not text:
                    raise DeepSeekError(
                        f"响应无内容（finish_reason={ch.get('finish_reason')}）: {str(data)[:200]}")
                usage = data.get("usage")
                if usage is not None:
                    debug_usage("complete", str(context), self.model, usage,
                                cache_hit_tokens(usage),
                                extra={"finish": ch.get("finish_reason")})
                if usage is not None and self.guard:
                    cache_hit = cache_hit_tokens(usage)
                    self.guard.record_usage(
                        context,
                        int(usage.get("prompt_tokens", 0) or 0),
                        int(usage.get("completion_tokens", 0) or 0),
                        provider=self.provider_id, model=self.model,
                        cache_hit_tokens=cache_hit)
                if ch.get("finish_reason") == "length":
                    logger.warning(
                        "DeepSeek complete 输出被 max_tokens 截断 (chars=%d, model=%s)",
                        len(text), self.model)
                return text
            except _req.Timeout as e:
                last_err = e
                logger.warning("DeepSeek complete 超时 attempt=%d/%d", attempt + 1,
                               self.max_retries + 1)
            except _req.ConnectionError as e:
                last_err = e
                logger.warning("DeepSeek complete 连接失败 attempt=%d/%d", attempt + 1,
                               self.max_retries + 1)
            except _req.HTTPError as e:
                code = e.response.status_code if e.response is not None else 0
                if code in (429,) or code >= 500:   # 限流/服务端：可重试
                    last_err = e
                    logger.warning("DeepSeek complete HTTP %s attempt=%d/%d", code,
                                   attempt + 1, self.max_retries + 1)
                else:                                # 4xx 业务错误：不重试，直接失败
                    raise DeepSeekError(
                        f"DeepSeek 调用失败（HTTP {code}）: {e}{self._hint}") from e
            except DeepSeekError:
                raise
            except Exception as e:  # noqa: BLE001 - 业务错误：不重试，直接失败
                raise DeepSeekError(
                    f"DeepSeek 调用失败（业务错误不重试）: {e}{self._hint}") from e
        raise DeepSeekError(
            f"DeepSeek 调用失败（已重试 {self.max_retries} 次）: {last_err}{self._hint}") \
            from last_err

    # ---------------------------------------------------------- 对话式（多消息）通道
    def chat_messages(self, messages: list[dict[str, str]],
                      context: str = "compile") -> str:
        """**对话式**多消息补全（paperkb 编译+翻译共用一条对话时用）。

        与 `complete()` 的差别：接收**完整 messages 列表**（含 assistant 历史），而不是单条 prompt。
        `context` 用于 TokenGuard 分组（`translate` 单列，其余归 engine）。思考档沿用
        `_resolve_effort` 的单一判据（translate=low / 其余映射），否则 GLM 会把 max_tokens
        全用在思考上、正文 0 字符（2026-09-13 实测）。
        """
        if self.guard:
            input_chars = sum(len(m.get("content") or "") for m in messages)
            self.guard.begin_call(context, input_chars)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        effort, explicit = self._resolve_effort(context)
        if effort and (explicit or self._reasoning_supported):
            payload["reasoning_effort"] = effort
        debug_request("chat_messages", self.model, messages, context=str(context),
                      extra={"effort": effort, "max_tokens": self.max_tokens})
        import requests as _req

        resp = _req.post(
            self._base_url + "/chat/completions",
            headers={"Authorization": "Bearer " + self._api_key,
                     "Content-Type": "application/json"},
            json=payload, timeout=self.timeout_sec)
        resp.raise_for_status()
        data = resp.json()
        try:
            ch = data["choices"][0]
        except (KeyError, IndexError, TypeError) as e:
            raise DeepSeekError(f"对话式补全返回异常信封: {str(data)[:200]}") from e
        msg = ch.get("message") or ch.get("delta") or {}
        text = str(msg.get("content") or "").strip()
        if not text:
            text = str(msg.get("reasoning_content") or "").strip()
        import re as _re3
        text = _re3.sub(r"</?think>", "", text).strip()
        usage = data.get("usage")
        if usage is not None:
            debug_usage("chat_messages", str(context), self.model, usage,
                        cache_hit_tokens(usage), extra={"finish": ch.get("finish_reason")})
        if usage is not None and self.guard:
            self.guard.record_usage(
                context,
                int(usage.get("prompt_tokens", 0) or 0),
                int(usage.get("completion_tokens", 0) or 0),
                provider=self.provider_id, model=self.model,
                cache_hit_tokens=cache_hit_tokens(usage))
        if ch.get("finish_reason") == "length":
            logger.warning("对话式补全输出被 max_tokens 截断（model=%s, chars=%d）",
                           self.model, len(text))
        return text


# ---------------------------------------------------------------- 对话通道

def _extract_delta_text(delta: Any) -> str:
    """openai 3.x 流式 delta.content 兼容提取（str 或 list[part]）。"""
    content = getattr(delta, "content", None)
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # 新版分片格式
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                t = item.get("text")
                if isinstance(t, dict):
                    parts.append(str(t.get("value", "")))
                elif t is not None:
                    parts.append(str(t))
            elif hasattr(item, "text"):
                t = item.text
                parts.append(t if isinstance(t, str) else getattr(t, "value", ""))
        return "".join(parts)
    return str(content)


def _extract_delta_reasoning(delta: Any) -> str:
    """流式 delta 的**思维链增量**（`delta.reasoning_content`）兼容提取。

    与 `_extract_delta_text` 分离：后者语义固定为"只取正文"（调用方依赖）。
    openai SDK 版本差异：思考型供应商（DeepSeek-R/GLM/Kimi）把该字段放在
    显式属性上，新版 SDK 也会把未建模字段收进 `model_extra`——两条路都取。
    取不到（如 deepseek-chat 不返回思维链）⇒ ""。
    """
    raw = getattr(delta, "reasoning_content", None)
    if raw is None:
        extra = getattr(delta, "model_extra", None)
        if isinstance(extra, dict):
            raw = extra.get("reasoning_content")
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):  # 分片格式：只收字符串片
        return "".join(x for x in raw if isinstance(x, str))
    return str(raw)


class ChatCompleter:
    """对话流式补全（与翻译通道共享 client，独立模型/温度可配置）。"""

    def __init__(self, api_key: str, base_url: str, model: str,
                 timeout_sec: int = 600, temperature: float = 0.7,
                 max_tokens: int = 4096, guard: TokenGuard | None = None,
                 provider_id: str = "chat",
                 reasoning_effort: str | None = None):
        if not api_key:
            raise DeepSeekError("未配置 API Key（请在设置中心配置供应商）")
        from openai import OpenAI
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout_sec)
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.guard = guard
        self.provider_id = provider_id
        # 构造级思考强度（供应商级默认）；None = 不发送（走供应商默认行为）。
        # 请求级 effort 由 stream_events(..., effort=...) 传入，优先级更高。
        self.reasoning_effort = reasoning_effort

    # ---------------------------------------------------------- reasoning_effort 判据（唯一入口）
    def _reasoning_supported(self) -> bool:
        """provider/model 是否"思考型"（复用模块级 REASONING_MODEL_HINTS，唯一判据）。"""
        return any(h in (self.model or "").lower() for h in REASONING_MODEL_HINTS)

    def _reasoning_payload_effort(self, effort: str | None = None) -> str | None:
        """判断"该不该发 reasoning_effort"的**唯一**入口；返回值非空即写进请求体。

        判据（与 DeepSeekAI._reasoning_supported 同源，不引入第二套列表）：
        - 取值：请求级 `effort` 优先，否则构造级 `self.reasoning_effort`；
        - 双空 ⇒ None = 不发送（用供应商默认，契约 1）；
        - 请求级显式值 = 调用方明确要求 ⇒ 视为"该模型支持" ⇒ 发送
          （端点真不支持由 stream_events 的降级重试兜底，不打死整轮问答）；
        - 仅构造级默认值时，按 REASONING_MODEL_HINTS 判模型是否思考型，防误发给不支持的端点。
        """
        eff = effort or self.reasoning_effort
        if not eff:
            return None
        if not (effort or self._reasoning_supported()):
            return None
        return eff

    def stream(self, context: str, messages: list[dict[str, str]],
               effort: str | None = None) -> Iterator[str]:
        """向后兼容的纯文本流：只产出正文（思维链见 `stream_events`）。"""
        for ev in self.stream_events(context, messages, effort=effort):
            if ev.get("type") == "delta":
                yield ev["text"]

    def stream_events(self, context: str, messages: list[dict[str, str]],
                      effort: str | None = None) -> Iterator[dict]:
        """流式对话补全，逐事件产出 `{"type": "delta"|"reasoning", "text": ...}`。

        - `delta`：正文增量；`reasoning`：供应商返回的思维链增量（`delta.reasoning_content`）。
        - `effort`：请求级思考强度（"low"/"high"）。None/"" ⇒ **不发送** reasoning_effort
          （供应商默认）；该参数是否发送统一由 `_reasoning_payload_effort` 判定。
        - 端点拒绝 reasoning_effort（4xx，openai SDK 在 create() 时即 raise）⇒ **去掉该参数
          降级重试一次**并记 warning，不把整轮问答打死。
        - messages 由上层组装；context 为防护计数标识（如 session:12）。
        """
        if self.guard:
            input_chars = sum(len(m.get("content", "")) for m in messages)
            self.guard.begin_call(context, input_chars)
        eff = self._reasoning_payload_effort(effort)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if eff:
            payload["reasoning_effort"] = eff
        debug_request("stream", self.model, messages, context=str(context),
                      extra={"effort": eff, "max_tokens": self.max_tokens})
        try:
            stream = self._client.chat.completions.create(**payload)
        except Exception as e:  # noqa: BLE001 - 端点不支持该参数：降级重试一次
            if not eff:
                raise
            logger.warning(
                "对话端点拒绝 reasoning_effort=%s（%s）→ 去掉该参数降级重试一次", eff, e)
            payload.pop("reasoning_effort", None)
            stream = self._client.chat.completions.create(**payload)
        usage = None
        for chunk in stream:
            # 末 chunk 携带 usage（include_usage=True 时）
            u = getattr(chunk, "usage", None)
            if u is not None:
                usage = u
            if not getattr(chunk, "choices", None):
                continue
            delta = chunk.choices[0].delta
            reasoning = _extract_delta_reasoning(delta)
            if reasoning:
                yield {"type": "reasoning", "text": reasoning}
            text = _extract_delta_text(delta)
            if text:
                yield {"type": "delta", "text": text}
        if usage is not None and self.guard:
            cache_hit = cache_hit_tokens(usage)
            debug_usage("stream", str(context), self.model, usage, cache_hit)
            self.guard.record_usage(
                context,
                int(getattr(usage, "prompt_tokens", 0) or 0),
                int(getattr(usage, "completion_tokens", 0) or 0),
                provider=self.provider_id, model=self.model,
                cache_hit_tokens=cache_hit)

    def chat_messages(self, messages: list[dict[str, str]], context: str = "compile") -> str:
        """**对话式**多消息补全（paperkb 编译+翻译共用一条对话时用）。

        与 `complete(messages, context="chat")` 的区别只在 context 语义：本方法用于**编译/翻译**
        分组（`context="compile"/"translate"`），让 TokenGuard 记账与既有链路一致。
        """
        return self.complete(messages, context=context)

    def complete(self, messages: list[dict[str, str]], context: str = "chat") -> str:
        """非流式对话补全（用于回答缓存回填/离线场景）。"""
        if self.guard:
            input_chars = sum(len(m.get("content", "")) for m in messages)
            self.guard.begin_call(context, input_chars)
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            stream=False,
        )
        # P12 反馈：魔塔限流/额度耗尽同样可能返回空信封 → 明确报错而非裸 AttributeError
        if not getattr(resp, "choices", None):
            raise DeepSeekError(
                f"对话补全返回空信封（choices 为空，魔塔免费额度可能耗尽或限流）"
                f"（魔塔免费额度/Key 问题？请在 设置中心-模型 切换供应商）")
        text = (resp.choices[0].message.content or "").strip()
        usage = getattr(resp, "usage", None)
        if usage is not None and self.guard:
            cache_hit = cache_hit_tokens(usage)
            self.guard.record_usage(
                context,
                int(getattr(usage, "prompt_tokens", 0) or 0),
                int(getattr(usage, "completion_tokens", 0) or 0),
                provider=self.provider_id, model=self.model,
                cache_hit_tokens=cache_hit)
        return text

    def complete_with_tools(self, context: str, messages: list[dict],
                            tools: list[dict]) -> tuple[str | None, list | None]:
        """非流式带 tools 的对话补全（知识库管理模式工具调用循环用）。

        返回 (content, tool_calls)：content=最终回答文本（无工具调用时）；tool_calls
        = openai 格式调用列表（[{id, function:{name, arguments}}]，可能多条）。
        """
        if self.guard:
            input_chars = sum(len(m.get("content") or "") for m in messages)
            self.guard.begin_call(context, input_chars)
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            stream=False,
            tools=tools,
        )
        if not getattr(resp, "choices", None):
            raise DeepSeekError(
                "对话补全返回空信封（限流/额度耗尽？请在 设置中心-模型 切换供应商）")
        msg = resp.choices[0].message
        tool_calls = None
        if getattr(msg, "tool_calls", None):
            tool_calls = [
                {"id": tc.id,
                 "function": {"name": tc.function.name,
                              "arguments": tc.function.arguments}}
                for tc in msg.tool_calls]
        content = (msg.content or "").strip() or None
        usage = getattr(resp, "usage", None)
        if usage is not None and self.guard:
            cache_hit = cache_hit_tokens(usage)
            self.guard.record_usage(
                context,
                int(getattr(usage, "prompt_tokens", 0) or 0),
                int(getattr(usage, "completion_tokens", 0) or 0),
                provider=self.provider_id, model=self.model,
                cache_hit_tokens=cache_hit)
        return content, tool_calls


# ---------------------------------------------------------------- 单例管理

_ai: DeepSeekAI | None = None
_chat: ChatCompleter | None = None


def build_ai(provider: dict, guard: TokenGuard | None = None) -> DeepSeekAI:
    """按供应商配置构建引擎 AI provider（V03：多供应商通用）。

    P12F：输入规模在翻译/引擎链路限制（≤10k/请求 分批；整篇一次受 TokenGuard 红线约束），
    正常响应 10-40s。**超时 600s**：翻译/编译输出为长文（几千 token 逐字生成），180s 不够，
    会误报 read timeout；600s 覆盖长译文/总结生成，输入超限仍由 TokenGuard/分批预检拦截快速失败。
    P（翻译截断）：透传供应商 max_tokens（翻译/总结共用该上限；默认 DEFAULT_MAX_OUTPUT_TOKENS）。
    """
    return DeepSeekAI(
        api_key=provider["api_key"],
        base_url=provider["base_url"],
        model=provider["model"],
        timeout_sec=600,
        max_retries=1,
        max_tokens=provider.get("max_tokens") or DEFAULT_MAX_OUTPUT_TOKENS,
        guard=guard,
        provider_id=provider.get("id", "deepseek"),
        provider_name=provider.get("name", ""),
        reasoning_effort=provider.get("reasoning_effort"),  # None → 走 context 自动映射
    )


def init_llm(provider: dict, guard: TokenGuard | None = None,
             settings: Settings | None = None) -> DeepSeekAI:
    """创建 provider、注入引擎（set_ai）、初始化对话通道（V03 供应商化）。

    provider: {id, name, base_url, model, api_key}（settings_service 提供）。
    settings 兼容旧调用（无 provider 时从 settings 构建默认 DeepSeek）。
    """
    global _ai, _chat
    if provider is None and settings is not None:
        provider = {
            "id": "deepseek", "name": "DeepSeek 官方",
            "base_url": settings.deepseek_base_url,
            "model": settings.deepseek_model,
            "api_key": settings.deepseek_api_key,
        }
    _ai = build_ai(provider, guard)
    set_ai(_ai)  # 注入引擎：ai_review（P14 字符级仲裁）走该供应商（带防护）
    _chat = ChatCompleter(
        api_key=provider["api_key"],
        base_url=provider["base_url"],
        model=provider["model"],
        timeout_sec=600,
        # 2026-09-12 实测：不传 max_tokens 会落到 `ChatCompleter` 的默认 4096，而开启思考的模型
        # **思维链与正文共用输出预算** → 实测两轮问答 4096 输出全是 reasoning、正文为空（用户看到的
        # 就是"没有回答"）。与引擎链路保持一致：供应商配的 max_tokens 优先，否则用 64000。
        max_tokens=provider.get("max_tokens") or DEFAULT_MAX_OUTPUT_TOKENS,
        guard=guard,
        provider_id=provider.get("id", "chat"),
    )
    logger.info("LLM 已初始化: provider=%s model=%s base_url=%s (guard=%s)",
                provider.get("id"), _ai.model, provider["base_url"], guard is not None)
    return _ai


def reconfigure_llm(provider: dict, guard: TokenGuard | None = None) -> DeepSeekAI:
    """热切换供应商（设置中心变更后调用，无需重启）。"""
    logger.info("LLM 热切换: %s -> %s", getattr(_ai, "provider_id", "?"),
                provider.get("id"))
    return init_llm(provider, guard)


def get_ai() -> DeepSeekAI:
    if _ai is None:
        raise DeepSeekError("LLM 未初始化：请先调用 init_llm()")
    return _ai


def get_chat() -> ChatCompleter:
    if _chat is None:
        raise DeepSeekError("LLM 未初始化：请先调用 init_llm()")
    return _chat
