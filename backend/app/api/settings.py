# -*- coding: utf-8 -*-
"""设置中心 API（V03）：多供应商 CRUD / 激活 / 测试连接 / 价格 / 知识库路径。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..services import container
from ..services.llm_service import DEFAULT_MAX_OUTPUT_TOKENS, DeepSeekError, build_ai
from ..services.settings_service import _is_masked_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])


class ProviderModel(BaseModel):
    id: str = ""
    name: str
    base_url: str
    model: str
    api_key: str = ""
    enabled: bool = True
    # P13：env 预填标记（DEEPSEEK/SILICONFLOW——写回 .env 依据）。
    # 此前缺失导致 pydantic 丢弃前端提交的 env → .env 写回永不触发（用户问题 2）。
    env: str = ""
    # P（翻译截断）：单次输出 token 上限（翻译/总结共用，默认 64000）。
    max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    # reasoning_effort：思考强度（可选，None=按任务 context 自动——translate=low / compile=high；
    # 用于 GLM 等始终思考的供应商；DeepSeek-chat 无需配置）。
    reasoning_effort: str | None = None


class PricesModel(BaseModel):
    """T3：单价保存负载。provider_id/model 为空 → 更新全局默认（default）；
    带 provider_id+model → 更新该供应商-模型组合（三项全空 = 删除该项回落默认）。"""
    input_per_m: float | None = None
    cached_input_per_m: float | None = None
    output_per_m: float | None = None
    provider_id: str = ""
    model: str = ""


class MineruModel(BaseModel):
    api_key: str = ""
    parser: str = "auto"


class ParseModel(BaseModel):
    """PDF 解析设置（设置中心「解析」tab 一个请求提交全部字段）。

    2026-09-12 批1：**必须显式声明前端提交的全部字段**——pydantic 默认丢弃未声明字段，
    原先漏了 translate_gate/skip_review_batch/parse_interval_sec ⇒ 前端提交后被静默丢弃。
    批2：新增 `mineru_api_key`（None=不改，空串=显式清空）与 `mineru_params`
    （language/is_ocr/enable_table）、`paddleocr.options`（restructurePages/…）——
    后两者写 `.env` 单一来源（paperparse 解析时实时读）。
    """
    mode: str = "dual"
    ai_review: bool = True
    paddleocr: dict = {}
    translate_gate: str = "wait"            # wait=有待复核项则挂起 / auto=立即翻译
    skip_review_batch: bool = False         # 批量跳过审核直接翻译
    parse_interval_sec: int = 8             # 批量解析篇间间隔（防限流，0-60）
    mineru_api_key: str | None = None       # None=本次不动 Key
    mineru_params: dict = {}                # {language, is_ocr, enable_table}


class KbPathModel(BaseModel):
    path: str = ""


class KbRulesModel(BaseModel):
    """2026-09-12 批1：删除装饰性 `include`（「纳入清单」全仓无消费点——kb 产物由 layout
    契约固定），只留**真正生效**的复制方式。"""
    copy_mode: str = "copy"


@router.get("")
def get_settings() -> dict:
    return container.get_settings_service().get_all()


@router.get("/providers")
def list_providers() -> dict:
    svc = container.get_settings_service()
    return {"providers": svc.get_providers(masked=True),
            "active_provider": svc.get_active_id()}


@router.post("/providers")
def save_providers(providers: list[ProviderModel]) -> dict:
    """保存供应商列表（含激活回退）。保存后若 active 变化 → 热切换 LLM。"""
    svc = container.get_settings_service()
    old_active = svc.get_active_id()
    svc.save_providers([p.model_dump() for p in providers])
    new_active = svc.get_active_id()
    provider = svc.get_active_provider(masked=False)
    try:
        ready = container.apply_provider(provider)
    except DeepSeekError as e:
        raise HTTPException(400, f"供应商配置无效: {e}")
    return {"ok": True, "active_provider": new_active,
            "llm_ready": ready,
            "switched": new_active != old_active,
            "providers": svc.get_providers(masked=True)}


@router.post("/providers/{provider_id}/activate")
def activate_provider(provider_id: str) -> dict:
    svc = container.get_settings_service()
    try:
        svc.set_active_provider(provider_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    provider = svc.get_active_provider(masked=False)
    # P5 点1：配置不完整（缺 Base URL/模型/Key）不可激活
    if not (provider or {}).get("base_url") or not (provider or {}).get("model"):
        raise HTTPException(400, "供应商 Base URL/模型为空，请先编辑补全再激活")
    if not (provider or {}).get("api_key"):
        raise HTTPException(400, "供应商未配置 API Key，无法激活")
    try:
        ready = container.apply_provider(provider)
    except DeepSeekError as e:
        raise HTTPException(400, f"供应商配置无效: {e}")
    return {"ok": True, "active_provider": provider_id, "llm_ready": ready}


# ---------------------------------------------------------------- T1：翻译模型池（可存多个，单选激活）
class TranslationProviderModel(BaseModel):
    """翻译专用供应商配置（可选）。enabled=是否为当前激活项（同时只有一个）。"""
    id: str = ""
    name: str = ""
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    reasoning_effort: str | None = None
    enabled: bool = True


@router.get("/translation-providers")
def get_translation_providers() -> dict:
    """获取翻译供应商池（全部，含未启用的）。[] = 未配置（用主模型）。"""
    svc = container.get_settings_service()
    return {"translation_providers": svc.get_translation_providers(masked=True)}


@router.post("/translation-providers")
def save_translation_providers(providers: list[TranslationProviderModel]) -> dict:
    """保存翻译供应商池（整体替换；空列表=清除回落主模型）。热切换立即生效。"""
    svc = container.get_settings_service()
    payload = [p.model_dump() for p in providers]
    # 过滤空配置（无 base_url/model/api_key 的条目）
    payload = [p for p in payload
               if p.get("base_url") and p.get("model") and p.get("api_key")]
    if not payload:
        svc.clear_translation_provider()
        container.apply_translation_providers(None)
        return {"ok": True, "translation_providers": [], "cleared": True}
    svc.save_translation_providers(payload)
    enabled = svc.get_enabled_translation_providers(masked=False)
    ready = container.apply_translation_providers(enabled)
    return {"ok": True,
            "translation_providers": svc.get_translation_providers(masked=True),
            "llm_ready": ready}


@router.delete("/translation-providers")
def clear_translation_providers() -> dict:
    """清除翻译供应商池（回落主模型）。"""
    svc = container.get_settings_service()
    svc.clear_translation_provider()
    container.apply_translation_providers(None)
    return {"ok": True, "cleared": True}


# ---- 兼容旧单条端点（委托池实现） ----
@router.get("/translation-provider")
def get_translation_provider() -> dict:
    """兼容：返回第一个启用的翻译供应商。"""
    svc = container.get_settings_service()
    return {"translation_provider": svc.get_translation_provider(masked=True)}


@router.post("/translation-provider")
def save_translation_provider(body: TranslationProviderModel) -> dict:
    """兼容：保存单个翻译供应商（视为单元素池；空=清除）。"""
    svc = container.get_settings_service()
    payload = body.model_dump()
    current = svc.get_translation_provider(masked=False)
    if current:
        from app.services.settings_service import _is_masked_key
        if _is_masked_key(payload.get("api_key", ""), current.get("api_key", "")):
            payload["api_key"] = current.get("api_key", "")
    if not payload.get("base_url") or not payload.get("model") or not payload.get("api_key"):
        svc.clear_translation_provider()
        container.apply_translation_providers(None)
        return {"ok": True, "translation_provider": None, "cleared": True}
    svc.save_translation_provider(payload)
    enabled = svc.get_enabled_translation_providers(masked=False)
    ready = container.apply_translation_providers(enabled)
    return {"ok": True, "translation_provider": svc.get_translation_provider(masked=True),
            "llm_ready": ready}


@router.delete("/translation-provider")
def clear_translation_provider() -> dict:
    """兼容：清除翻译供应商池。"""
    svc = container.get_settings_service()
    svc.clear_translation_provider()
    container.apply_translation_providers(None)
    return {"ok": True, "cleared": True}


class TestProviderModel(ProviderModel):
    pass


def _resolve_test_key(p: TestProviderModel) -> str:
    """测试连接用的 Key：明文优先；空/掩码占位 → 取该供应商已存的真实 Key。

    掩码判定复用 `settings_service._is_masked_key`（单一真相源），避免这里再写一套
    "含 •/…/*" 的启发式——前端与后端两套判据正是本 bug 的成因。
    取不到（如新增供应商还没填）→ 返回 ""，由调用方给出明确报错。
    """
    key = str(p.api_key or "").strip()
    stored = ""
    try:
        for prov in container.get_settings_service().get_providers(masked=False):
            if p.id and prov.get("id") == p.id:
                stored = str(prov.get("api_key") or "")
                break
    except Exception as e:  # noqa: BLE001 - 取已存 Key 失败按"无 Key"处理，报错更明确
        logger.warning("测试连接读取已存 Key 失败 id=%s: %s", p.id, e)
        return key
    if not key or _is_masked_key(key, stored):
        return stored
    return key


@router.post("/test")
def test_provider(p: TestProviderModel) -> dict:
    """测试连接：发送最小请求验证 key/端点（消耗极小 token）。

    用户反馈（2026-09-12）：编辑已配置的供应商时，密钥框显示的是掩码占位
    （`••••••••`），前端据此直接拦下"请先填写真实 API Key"——**已保存的 Key 明明可用**。
    现在：明文优先；空/掩码占位（= 用户没改）→ 按 `id` 取该供应商**已存的真实 Key**。
    """
    payload = p.model_dump()
    payload["api_key"] = _resolve_test_key(p)
    if not payload["api_key"]:
        raise HTTPException(400, "该供应商未配置 API Key，请填写后再测试")
    try:
        ai = build_ai(payload, guard=None)
        # 最小请求："ping"（输出 ~2 tokens）
        resp = ai._client.chat.completions.create(
            model=p.model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=5,
            stream=False,
        )
        text = (resp.choices[0].message.content or "").strip()
        return {"ok": True, "reply": text[:50], "model": p.model}
    except DeepSeekError as e:
        raise HTTPException(400, f"配置无效: {e}")
    except Exception as e:  # noqa: BLE001 - openai 异常透传
        logger.warning("测试连接失败: %s", e)
        raise HTTPException(400, f"连接失败: {e}")


@router.post("/prices")
def save_prices(prices: PricesModel) -> dict:
    svc = container.get_settings_service()
    payload = prices.model_dump()
    svc.save_prices(payload)
    # T3 即时生效：default 同步到 usage 内部兜底价；per-provider 由 resolver 查 settings 实时生效
    container.get_usage().prices = svc.get_prices()["default"]
    container.get_event_bus().publish("info", "settings", "prices",
                                      "单价表已更新", {"prices": payload})
    return {"ok": True}


@router.post("/mineru")
def save_mineru(cfg: MineruModel) -> dict:
    """保存 MinerU 主通道配置（**写 .env 单一来源 + 回读断言**）。

    2026-09-12 批1：原实现只写 SQLite，全仓无消费者（引擎读 .env）⇒ UI 改 Key/通道无效。
    返回 `readback.ok=False` 时前端必须提示"配置未落盘"（只读盘/文件被占用等）。
    """
    svc = container.get_settings_service()
    try:
        result = svc.save_mineru(cfg.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not result["readback"]["ok"]:
        logger.error("MinerU 配置回读不一致：%s", result["readback"]["mismatch"])
    container.get_event_bus().publish(
        "info", "settings", "mineru",
        "MinerU 配置已更新（.env）", {"parser": result["parser"],
                                      "api_key_set": result["api_key_set"]})
    return {"ok": True, "mineru": svc.get_mineru(), **result}


class AutoExitModel(BaseModel):
    enabled: bool = True


@router.post("/auto-exit")
def save_auto_exit(body: AutoExitModel) -> dict:
    """P5 点3：随窗口关闭自动退出开关。"""
    container.get_settings_service().save_auto_exit(body.enabled)
    return {"ok": True, "enabled": body.enabled}


@router.post("/auto-compile")
def save_auto_compile(body: AutoExitModel) -> dict:
    """自动编译开关（2026-09-12 补入口）。

    `save_auto_compile` 早已存在但**没有任何 API/UI** → 一旦被关成 "0"，
    「解析+编译」会静默什么都不做（用户实测到的"文献没纳入知识库"同族问题）。
    这里补上读写入口，让该开关在设置中心-解析可见可改。
    """
    container.get_settings_service().save_auto_compile(body.enabled)
    return {"ok": True, "enabled": body.enabled}


class ChatReasoningEffortModel(BaseModel):
    effort: str = "auto"      # low | high | auto


class CompileEffortModel(BaseModel):
    effort: str = "auto"      # auto | none | minimal | low | medium | high


@router.post("/compile-effort")
def save_compile_effort(body: CompileEffortModel) -> dict:
    """编译思考档（批3）：L1/L2/L3 编译任务的 `reasoning_effort`。

    - `auto`（默认）⇒ **不发送**该参数（服务端自适应；质量与历史一致）。
      ⚠️ 服务端拒绝字面 "auto"（实测 400），故"自动"只能以不传参实现；
    - `none/minimal/low/medium/high` ⇒ 显式下发（实测均被接受；`none` 思考 0 字符最省）；
    - **非法值 → 400**（与 chat-effort 同口径：拒绝而非归一化）。
    """
    try:
        effort = container.get_settings_service().save_compile_effort(body.effort)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    container.get_event_bus().publish("info", "settings", "compile",
                                      f"编译思考档已设为 {effort}", {"effort": effort})
    return {"ok": True, "effort": effort}


@router.post("/translate-effort")
def save_translate_effort(body: CompileEffortModel) -> dict:
    """翻译思考档（批3）：整篇/分批翻译任务的 `reasoning_effort`。

    语义与编译档一致：`auto`（默认）= 不发送参数（服务端自适应 ⇒ 翻译质量与历史一致）；
    其余档位显式下发（翻译是长输出任务，降档可省输出 token，但可能影响术语/公式标签一致性）。
    """
    try:
        effort = container.get_settings_service().save_translate_effort(body.effort)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    container.get_event_bus().publish("info", "settings", "translate",
                                      f"翻译思考档已设为 {effort}", {"effort": effort})
    return {"ok": True, "effort": effort}


@router.post("/chat-reasoning-effort")
def save_chat_reasoning_effort(body: ChatReasoningEffortModel) -> dict:
    """对话思考强度（契约 3）：low / high / auto。

    - `auto`（默认）⇒ 前端请求 `POST /api/chat/stream` 时不带 `effort` ⇒ 后端不发
      `reasoning_effort`（用供应商默认）。
    - **非法值 → 400**（本端点采用"拒绝"而非"归一化"；读回侧 `get_chat_reasoning_effort()`
      仍对库里可能存在的脏值兜底为 auto，GET /api/settings 永不返回非法值）。
    - 返回 `{"ok": true, "effort": <归一化后的小写值>}`。
    """
    try:
        effort = container.get_settings_service().save_chat_reasoning_effort(body.effort)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "effort": effort}


@router.post("/parse")
def save_parse(cfg: ParseModel) -> dict:
    """保存解析设置（解析模式/仲裁/门控/间隔 + MinerU 参数与 Key + PaddleOCR 配置与参数）。

    批2：`mineru_params` 与 `paddleocr.options` 写 .env 并回读；MinerU Key **两种写法都认**
    （顶层 `mineru_api_key` 或 `mineru_params.mineru_api_key`，前端用的是后者）——判据只此一处，
    避免"前端提交了但后端没接住"的静默丢失。Key 单一来源仍是 .env。
    """
    svc = container.get_settings_service()
    params = dict(cfg.mineru_params or {})
    nested_key = params.pop("mineru_api_key", None)   # 前端把它嵌在 mineru_params 里
    cfg.mineru_params = params
    key_to_save = cfg.mineru_api_key if cfg.mineru_api_key is not None else nested_key
    try:
        result = svc.save_parse(cfg.model_dump())
        if key_to_save is not None:
            svc.save_mineru({"api_key": key_to_save,
                             "parser": "auto"})   # 批2：通道全自动（无 Key 时由门禁拒绝解析）
    except ValueError as e:
        raise HTTPException(400, str(e))
    container.get_event_bus().publish(
        "info", "settings", "parse",
        f"解析设置已更新: mode={cfg.mode} ai_review={cfg.ai_review} "
        f"mineru={result['mineru_params']}",
        {"mode": cfg.mode, "ai_review": cfg.ai_review})
    parse_now = svc.get_parse()
    if not result["readback"]["ok"]:
        logger.error("解析参数回读不一致：%s", result["readback"]["mismatch"])
    return {"ok": True, "parse": parse_now, "readback": result["readback"]}


# ---------------------------------------------------------------- 规则库（已退役）
# 2026-09-12 批1：删除「待确认学习规则」僵尸面（GET/POST /rules、POST /rules/batch）。
# 产出端（P14 生产链不产生 learned 挖掘规则）与应用端（rule_engine 只在**已归档**的
# 老降级链调用）双双退役 ⇒ 该清单永远为空、批准也不影响实际解析。前端卡片同步删除。
# ⚠️ **另一条学习闭环仍在**：domain_ai 化学式词典（rules/learned/domain.json，
#    consensus_fix 读、复核页批准写入），它不走 pending_rules/decide_rule，见
#    review_service.apply_choice —— 删这里不影响它。


@router.post("/kb-path")
def save_kb_path(body: KbPathModel) -> dict:
    svc = container.get_settings_service()
    svc.save_kb_path(body.path)
    container.get_event_bus().publish("info", "settings", "kb",
                                      "知识库路径已更新", {"path": body.path})
    return {"ok": True}


@router.post("/kb-rules")
def save_kb_rules(body: KbRulesModel) -> dict:
    """保存知识库复制方式（T02）：copy（副本）/ link（硬链接，同盘省空间）。"""
    svc = container.get_settings_service()
    svc.save_kb_copy_mode(body.copy_mode)
    container.get_event_bus().publish(
        "info", "settings", "kb",
        f"知识库复制方式已更新（{svc.get_kb_copy_mode()}）",
        {"copy_mode": svc.get_kb_copy_mode()})
    return {"ok": True, "copy_mode": svc.get_kb_copy_mode()}


@router.post("/md-template")
def save_md_template(body: TextModel) -> dict:
    """保存全局默认输出模板（T06）：对新翻译/重渲染的 kb en_zh.md 生效。"""
    svc = container.get_settings_service()
    svc.save_md_template(body.text)
    container.get_event_bus().publish(
        "info", "settings", "template",
        f"输出模板已更新: {svc.get_md_template()}", {"template": body.text})
    return {"ok": True, "template": svc.get_md_template()}


class RetrievalModel(BaseModel):
    """2026-09-12 批1：删除装饰性 `include`（「AI 检索文件清单」无消费点——
    检索侧只按 mode 走，见 chat_service._retrieval_mode）。"""
    mode: str = "notes"


@router.post("/retrieval")
def save_retrieval(body: RetrievalModel) -> dict:
    """保存 AI 检索分级（T05）：模式（notes 仅笔记 / fragments 片段 / full 全文）。"""
    svc = container.get_settings_service()
    svc.save_retrieval_mode(body.mode)
    container.get_event_bus().publish(
        "info", "settings", "retrieval",
        f"AI 检索权限已更新: {svc.get_retrieval_mode()}", {"mode": body.mode})
    return {"ok": True, "mode": svc.get_retrieval_mode()}


class TextModel(BaseModel):
    text: str = ""


# 2026-09-12 批1：删除 `GET /system-prompt`、`GET /appearance`——前端只 POST 保存
# （app.js 的 bindAppearance），无任何 GET 调用方，读回值已在 `GET /api/settings` 里。


@router.post("/system-prompt")
def save_system_prompt(body: TextModel) -> dict:
    container.get_settings_service().save_system_prompt_extra(body.text)
    container.get_event_bus().publish("info", "settings", "prompt",
                                      "系统提示词已更新", {})
    return {"ok": True}


@router.post("/appearance")
def save_appearance(body: TextModel) -> dict:
    container.get_settings_service().save_custom_css(body.text)
    container.get_event_bus().publish("info", "settings", "appearance",
                                      "外观 CSS 已更新", {})
    return {"ok": True}
