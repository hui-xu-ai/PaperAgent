#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: paperparse/core/dual_ai_review.py
功能: P11/P12 双通道矛盾项 AI 批量仲裁（用户检查前先过便宜模型）：
      对 text_conflict（内容矛盾）与 latex_valid=False 的 format_diff（可疑公式）
      调用文本模型判断哪一侧更正确 / 两者皆可 / 皆错，输出 verdict+reason+confidence。
      P12：provider 泛化为 OpenAI 兼容端点（魔塔 ModelScope / 硅基流动 / DeepSeek 官方
      均可），环境链 DEEPSEEK_* → SILICONFLOW_*；一篇文献仲裁点一批上传（默认单请求）。
对外接口: OpenAICompatProvider（SiliconFlowProvider 兼容别名）/ arbitrate / build_arbitration
版本: v1.1.0 (2026-08-23)
版本历史:
  v1.0.0 初始版本（硅基流动硬编码）
  v1.1.0 P12 泛化：通用 OpenAI 兼容 provider + 环境链 + 一批一篇默认 50
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

__all__ = ["OpenAICompatProvider", "SiliconFlowProvider", "arbitrate", "synthesize"]

DEFAULT_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip() or \
    "deepseek-chat"
DEFAULT_BASE = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
VERDICTS = ("mineru", "paddleocr", "both", "neither")

_SYSTEM = (
    "你是学术论文 OCR 质检员。同一篇论文的同一位置有两个解析器输出（M=mineru 高精度、"
    "P=paddleocr）。两版文字有差异，请判断哪个更可能正确。"
    "判定规则：拼写错误/缺字多字=该侧错；LaTeX 公式与 Unicode 文本是同一公式的两种表示=both"
    "（格式差异）；页码/页眉等噪声差异=both；双方都明显错误=neither；"
    "**mineru 的公式若是碎片化表示（如 $1 0 0 ~ ^ { \\circ } \\mathrm { C }$——字符被"
    "空格拆散、LaTeX 命令穿插），即使内容等价也是识别劣化，判 P（paddleocr）**。"
    "严禁输出任何分析、推理或解释文字。只输出一个 JSON 数组，逐项对应输入顺序，"
    "形如 [{\"id\":0,\"verdict\":\"mineru|paddleocr|both|neither\","
    "\"reason\":\"中文≤25字\",\"confidence\":0.0-1.0}]。"
    "示例：输入 3 项则输出 3 个对象的数组。"
)


_SYSTEM_FORCE = (
    "你是学术论文 OCR 质检员。同一位置的 M（mineru）与 P（paddleocr）识别结果**都不完美**，"
    "但你必须**替人做决定**：综合两侧的识别语义（上下文词义、公式/单位/上下标是否成立、"
    "拼写与断词是否合理、哪一侧更可能是原文）**选出一个更接近正确的版本**。"
    "规则：**只能选 mineru 或 paddleocr**（二选一，不允许 both/neither/skip）；"
    "mineru 的公式若碎片化（如 $1 0 0 ~ ^ { \\circ } \\mathrm { C }$ 字符被空格拆散）"
    "而 paddleocr 给出可读明文 ⇒ 选 paddleocr。"
    "严禁输出任何分析、推理或解释文字。只输出一个 JSON 数组，逐项对应输入顺序，"
    "形如 [{\"id\":0,\"verdict\":\"mineru|paddleocr\","
    "\"reason\":\"中文≤25字\",\"confidence\":0.0-1.0}]。"
)


_SYSTEM_SYNTH = (
    "你是学术论文 OCR 修复专家。下面每处给出同一位置的**多个解析结果**（M=MinerU、P=PaddleOCR）、"
    "各自句内上下文，以及**PDF 自带文本层**的旁证（若有）。这些结果可能都不完美。\n"
    "你的任务：**综合所有证据做出你自己的判断**，给出该处**正确的文本**（可以与 M、P 都不同）。\n"
    "硬性约束：\n"
    "1) **只改冲突处那几个字**：单词/公式/标点/空格/上下标/单位/大小写；禁止重写整句、禁止增删实词；\n"
    "2) 公式请输出可渲染的 LaTeX（保持 `$...$` 包裹与下标 `_`/上标 `^` 写法）；\n"
    "3) 若两侧其一正确 → 直接照抄它，action=keep（保持 MinerU）或 replace（换成你选定的文本）；\n"
    "4) 若两侧都不对但可从证据推断正确写法 → action=replace，suggested_text 填**你综合后的片段**；\n"
    "5) 新增的字母/数字必须能在给出的证据里找到依据，**不要凭空造词或造数字**；\n"
    "6) 无法判断时 action=keep、suggested_text 留空、confidence 给低值。\n"
    "**第一行就输出 JSON 数组**，不要任何思考/解释/前言；每项形如："
    '{"id":<int>,"action":"keep|replace|insert|delete","suggested_text":"<片段>",'
    '"confidence":<0~1>,"reason":"<≤20字>","evidence":["M","P","local"]}'
)


_SYSTEM_SYNTH_SHORT = (
    "你是学术论文 OCR 修复专家。每项给出同一位置的 M（MinerU）/P（PaddleOCR）两个解析结果、"
    "各自句内上下文，以及可选的 PDF 文本层旁证；这些结果可能都不完美。\n"
    "逐项给出**该处正确的最终片段**（可与 M、P 都不同），或判定无需改动。\n"
    "硬性约束：1) **只改冲突处那几个字**（单词/公式/标点/空格/上下标/单位/大小写），"
    "禁止重写整句、禁止增删实词；2) 公式保持 `$...$` 与 `_`/`^` 写法；3) 新增字母/数字必须"
    "能在给出的证据里找到依据，不许凭空造；4) **公式若与 M 内容等价，只允许 k**"
    "（保留 MinerU 的 LaTeX，禁止改写成明文/HTML）；5) 判不了就 k。\n"
    "**第一行就输出 JSON 数组**（无任何思考/解释/前言），每项形如："
    '{"id":<int>,"a":"k|r|i|d","t":"<仅 r/i/d 时给最终片段>"}；'
    "a 含义：k=保持 MinerU 原文、r=替换为 t、i=插入 t、d=删除。"
)


def _short_mode() -> bool:
    """[局部] 是否用**短 schema + 关思考**（T9，默认开）。
    `PARSE_AI_SHORT=0` 回退旧的 `_SYSTEM_SYNTH` 长 schema（逃生门）。"""
    return str(os.environ.get("PARSE_AI_SHORT", "1")).strip().lower() \
        not in ("0", "false", "no", "off", "")


def _no_thinking() -> bool:
    """[局部] 是否请求关闭"思考"（T9，默认关思考）。
    `PARSE_AI_THINKING=1` 恢复让模型思考（旧行为）。"""
    return str(os.environ.get("PARSE_AI_THINKING", "0")).strip().lower() \
        not in ("1", "true", "yes", "on")


# 供应商兼容：关思考的请求体写法（魔塔/DeepSeek 系 `thinking.type=disabled`）
_THINKING_OFF_BODY = {"thinking": {"type": "disabled"}}


@dataclass
class Arbitration:
    """[全局] 一条仲裁结果"""
    id: int = 0
    diff_type: str = ""
    page: int = 0
    verdict: str = ""          # mineru|paddleocr|both|neither|unresolved
    reason: str = ""
    confidence: float = 0.0
    mineru: dict = field(default_factory=dict)
    paddleocr: dict = field(default_factory=dict)
    # ★2026-09-16（用户："让 AI 综合这些解析结果进行综合判断，给出自己的修改建议"）：
    # 综合建议三件套 —— 只有 `synthesize()` 会填；旧 `arbitrate()` 保持 None/空（向后兼容）。
    suggested_text: str = ""            # AI 综合出的**最终片段**（可与 M、P 都不同）
    action: str = ""                   # keep|replace|insert|delete
    evidence: list = field(default_factory=list)   # AI 自述用到的证据（M/P/local/dict）

    def to_dict(self) -> dict:
        out = {"id": self.id, "type": self.diff_type, "page": self.page,
               "verdict": self.verdict, "reason": self.reason,
               "confidence": round(self.confidence, 2),
               "mineru": self.mineru, "paddleocr": self.paddleocr}
        if self.suggested_text or self.action:
            out["suggested_text"] = self.suggested_text
            out["action"] = self.action
            out["evidence"] = list(self.evidence or [])
        return out


class OpenAICompatProvider:
    """[全局] 通用 OpenAI 兼容文本模型（AI 仲裁用；魔塔 ModelScope / 硅基流动 /
    DeepSeek 官方均可；key 从环境变量或显式参数读取，不落盘）。

    环境链（缺省时）：DEEPSEEK_API_KEY/BASE_URL/MODEL（当前 = 魔塔 ModelScope）
    → SILICONFLOW_API_KEY/BASE_URL（备用）。
    """

    def __init__(self, api_key: str | None = None, model: str | None = None,
                 base: str | None = None, timeout: float = 120.0,
                 on_usage=None):
        self.api_key = (api_key if api_key is not None
                        else os.getenv("DEEPSEEK_API_KEY", "").strip()
                        or os.getenv("SILICONFLOW_API_KEY", "").strip())
        self.model = model or DEFAULT_MODEL
        self.base = (base or DEFAULT_BASE).rstrip("/")
        self.timeout = timeout
        self.on_usage = on_usage   # 可选回调 on_usage(prompt_tokens, completion_tokens)

    def available(self) -> bool:
        return bool(self.api_key)

    def complete(self, messages: list[dict], *, temperature: float = 0.1,
                 max_tokens: int = 8192, extra_body: dict | None = None) -> str:
        """[全局] chat 补全（OpenAI 兼容协议）

        P12 适配思考型模型（如魔塔 deepseek-ai/DeepSeek-V4-Flash-0731）：
        content 可能为空（token 被 reasoning_content 思考占用，finish_reason=length）
        → content 为空时回退取 reasoning_content（常含 JSON 草稿）；
        max_tokens 默认 8192（思考 + 输出留足余量）。

        ★2026-09-17（T9）：新增 `extra_body`（如 `{"thinking":{"type":"disabled"}}`
        关思考）——实测现状 13 次调用里 7 次被思考挤到 `finish_reason=length`、
        输出 1,842 token/项；关思考 + 短 schema 后为 1 次调用 / 197 输出 token。
        **扩展参数不被端点接受（400/422）时自动去掉重试一次**（跨供应商降级，
        不打死整轮仲裁）。
        """
        if not self.available():
            raise RuntimeError("AI 仲裁 provider 未配置（DEEPSEEK_API_KEY / SILICONFLOW_API_KEY 均缺失）")
        # 魔塔空信封（200 + choices=null + usage 全 0）= 间歇限流占位（L013）→
        # 客户端自动重试（2s/4s/6s 退避），仲裁层 attempt 重试仅兜底
        last_data = {}
        _extra = dict(extra_body or {})
        for _attempt in range(3):
            payload = {"model": self.model, "messages": messages,
                       "temperature": temperature, "max_tokens": max_tokens}
            if _extra:
                payload.update(_extra)
            try:
                resp = requests.post(
                    "%s/chat/completions" % self.base,
                    headers={"Authorization": "Bearer %s" % self.api_key,
                             "Content-Type": "application/json"},
                    json=payload,
                    timeout=self.timeout)
                resp.raise_for_status()
            except requests.HTTPError:
                if _extra:
                    _extra = {}          # 端点不认扩展参数 → 去掉重试一次
                    continue
                raise
            data = resp.json()
            if not data.get("choices"):
                last_data = data
                time.sleep(2.0 * (_attempt + 1))
                continue
            last_data = data
            break
        data = last_data
        try:
            ch = data["choices"][0]
        except (KeyError, IndexError, TypeError) as e:
            raise RuntimeError("响应结构异常: %s" % str(data)[:400]) from e
        # 兼容两种信封：标准 message / 流式 delta（魔塔 V4-Flash-0731 返回 delta）
        msg = ch.get("message") or ch.get("delta") or {}
        text = str(msg.get("content") or "").strip()
        if not text:
            # 思考型模型：token 被 reasoning_content 思考占用 → 回退取思考内容（常含 JSON 草稿）
            text = str(msg.get("reasoning_content") or "").strip()
        if not text:
            raise RuntimeError("响应无内容（finish_reason=%s）: %s"
                               % (ch.get("finish_reason"), str(data)[:400]))
        if self.on_usage:
            try:
                u = data.get("usage") or {}
                self.on_usage(int(u.get("prompt_tokens", 0) or 0),
                              int(u.get("completion_tokens", 0) or 0))
            except Exception:  # noqa: BLE001 - 用量上报失败不阻塞
                pass
        return text


# 兼容别名（P11 期名称）
SiliconFlowProvider = OpenAICompatProvider


_RAW_LOG = Path(os.environ.get("PADDLEOCR_AI_RAW_LOG", "work/dual/ai_review_raw.log"))


def _parse_verdicts(text: str) -> list[dict]:
    """[局部] 模型输出 → 裁决列表（容错：剥代码围栏/提取 JSON 数组/
    正则提取 id+verdict 对——思考型模型常输出长篇分析而非纯 JSON）

    注意：模型可能输出解释文字/尾部备注 → 贪心 `[\\s\\S]*]` 会吞掉整个响应导致解析失败。
    策略：去围栏 → 整体 json.loads → 失败则非贪心取首个 `[...]`（平铺数组无嵌套括号）
    → 仍失败则正则提取 "id"+verdict 对。
    """
    t = re.sub(r"```(?:json)?", "", text).strip()
    for candidate in (t,):
        try:
            data = json.loads(candidate)
            if isinstance(data, list):
                return [it for it in data if isinstance(it, dict) and "verdict" in it]
        except json.JSONDecodeError:
            pass
    # 非贪心取首个 [...]（flat 数组）
    m = re.search(r"\[.*?\]", t, re.S)
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, list):
                return [it for it in data
                        if isinstance(it, dict) and "verdict" in it]
        except json.JSONDecodeError:
            pass
    # 兜底：从长文本（含思考/分析）正则提取 id + verdict 对
    pairs = re.findall(r'"id"\s*:\s*(\d+)\s*,\s*"verdict"\s*:\s*"([a-z]+)"', t)
    if not pairs:
        pairs = re.findall(r'\bid\s*[:=]\s*(\d+).{0,80}?"verdict"\s*:\s*"([a-z]+)"',
                           t, re.S)
    out = []
    for i, v in pairs:
        if v in VERDICTS:
            # P15：正则兜底提取（AI 输出分析文本+JSON 片段）无 confidence 字段
            # → 默认 0.95（AI 明确给出 verdict 即高置信；否则 conf=0 被门控
            # 拦截进复核，实证 volatili/perfor 判 P 却不落地）
            out.append({"id": int(i), "verdict": v, "confidence": 0.95,
                        "reason": ""})
    return out


def arbitrate(items: list[dict], *, provider: OpenAICompatProvider | None = None,
              batch_size: int = 50, paper: str = "",
              force: bool = False) -> list[Arbitration]:
    """[全局] 对矛盾项批量仲裁（P12：一篇文献一批——默认单请求整批上传）

    items: dual_report 的 DiffItem（dataclass 或 dict）列表
    provider: 仲裁模型（默认 OpenAICompatProvider()，环境链 DEEPSEEK_* → SILICONFLOW_*）
    只仲裁：text_conflict（内容矛盾）+ format_diff 中 latex_valid=False（可疑公式）
    说明：batch_size=50 覆盖绝大多数论文的仲裁点总数（一篇一批）；超长时自动分块
    （仍属一次批量上传，非逐条调用——按调用次数扣费）。

    `force`（★2026-09-16 用户要求"所有复核都由 AI 替人选择"）：用**强制二选一**提示词
    （`_SYSTEM_FORCE`）——只允许 mineru/paddleocr，不许 both/neither ⇒ 让 AI 借两侧识别
    语义替人做决定，而不是把"判不了"的项推给人工复核。
    """
    system = _SYSTEM_FORCE if force else _SYSTEM
    provider = provider or OpenAICompatProvider()
    if not provider.available():
        return [_unresolved(_as_dict(it), i, "AI 仲裁不可用（未配置 DEEPSEEK_API_KEY / SILICONFLOW_API_KEY）")
                for i, it in enumerate(items)]

    targets = []
    for i, it in enumerate(items):
        d = _as_dict(it)
        t = d.get("type")
        if t == "text_conflict":
            targets.append((i, d))
        elif t == "format_diff" and not d.get("evidence", {}).get("latex_valid", True):
            targets.append((i, d))
    if not targets:
        return []

    out: list[Arbitration] = []
    for start in range(0, len(targets), batch_size):
        batch = targets[start:start + batch_size]
        lines = []
        for i, it in batch:
            # P12F：800→2500——仲裁必须看**完整段落**（800 截断曾致 item13
            # 误判"M缺句尾，P更完整"：完整段落在 MinerU、缺句尾的是 PaddleOCR）
            # 阶段8f：insert 碎片（''→'f' 断词）用**上下文片段**（m_ctx 前后
            # 15 字符，如 "Efici[f]ent"）——无上下文 AI 判 mineru"P多余识别"
            _ev = it.get("evidence") or {}
            _m_ctx = _ev.get("m_ctx")
            _p_ctx = _ev.get("p_ctx")
            if _m_ctx:
                lines.append("<item id=%d> page=%d\nM: %s\nP: %s" % (
                    i, it.get("page"), _m_ctx, _p_ctx or ""))
            else:
                lines.append("<item id=%d> page=%d\nM: %s\nP: %s" % (
                    i, it.get("page"),
                    (it.get("mineru") or {}).get("text", "")[:2500],
                    (it.get("paddleocr") or {}).get("text", "")[:2500]))
        verdicts: dict = {}
        err = ""
        for attempt in range(2):   # 一次重试（模型偶发截断/少回）
            try:
                resp = provider.complete([
                    {"role": "system", "content": system},
                    {"role": "user", "content": "论文：%s\n%s" % (paper or "-", "\n\n".join(lines))},
                ], max_tokens=8192)
                try:
                    _RAW_LOG.parent.mkdir(parents=True, exist_ok=True)
                    with _RAW_LOG.open("a", encoding="utf-8") as f:
                        f.write("=== batch start=%d attempt=%d ===\n%s\n" % (start, attempt, resp))
                except Exception:
                    pass
                parsed = _parse_verdicts(resp)
                verdicts = {v.get("id"): v for v in parsed}
                # id 匹配不足 → 按位置回退（模型通常保持输入顺序）
                if len(verdicts) < len(batch):
                    by_pos = {}
                    for pos, (i, _it) in enumerate(batch):
                        if pos < len(parsed) and parsed[pos].get("verdict") in VERDICTS:
                            by_pos[i] = parsed[pos]
                    verdicts = by_pos if len(by_pos) > len(verdicts) else verdicts
                if len(verdicts) >= len(batch):
                    break
                err = "裁决数不足（%d/%d）" % (len(verdicts), len(batch))
            except Exception as e:
                # P15：**保留 attempt=0 已解析的部分结果**——此前 except 清空
                # verdicts，AI 已输出的项（如 batch 0 输出 2/20 项）也被丢成
                # unresolved（实证：attempt=1 异常 → 整批全 unresolved）。
                # 部分裁决保留（缺失项进复核清单），宁缺毋滥但不全丢。
                if not verdicts:
                    verdicts = {}
                err = str(e)[:80]
        for i, it in batch:
            v = verdicts.get(i)
            if v and v.get("verdict") in VERDICTS:
                out.append(Arbitration(
                    id=i, diff_type=it.get("type", ""), page=it.get("page", 0),
                    verdict=v["verdict"], reason=str(v.get("reason", ""))[:80],
                    confidence=float(v.get("confidence", 0) or 0),
                    mineru=it.get("mineru", {}), paddleocr=it.get("paddleocr", {})))
            else:
                out.append(_unresolved(it, i, err or "裁决解析失败"))
    return out


def _synthesize_once(batch: list, provider, paper: str, with_local: bool,
                     *, short: bool = True, no_thinking: bool = True) -> dict:
    """[局部] 单批请求 → {id: 建议 dict}（解析不到就返回已拿到的部分）。

    ★2026-09-17（T9）：`short=True` 用极短 schema（`{"id","a","t"}`）+ 小 max_tokens；
    `no_thinking=True` 一并请求关闭"思考"。二者**必须同时用**——实测只改 schema 不关思考
    时模型把预算全花在推理上、两次调用都撞满 1024 上限且 **JSON 解析 0/10**。
    """
    lines = ["论文：%s" % (paper or "-")]
    for i, it in batch:
        ev = it.get("evidence") or {}
        tv = it.get("third_vote") or {}
        m = (it.get("mineru") or {}).get("text", "")
        p = (it.get("paddleocr") or {}).get("text", "")
        lines.append("<item id=%d> page=%s type=%s" % (i, it.get("page", 0),
                                                       it.get("type", "")))
        lines.append("  差异：M=%r  P=%r" % (m[:80], p[:80]))
        if ev.get("m_ctx"):
            lines.append("  M句：%s" % str(ev["m_ctx"])[:300].replace("\n", " "))
        if ev.get("p_ctx"):
            lines.append("  P句：%s" % str(ev["p_ctx"])[:300].replace("\n", " "))
        if with_local and ev.get("local_snippet"):
            lines.append("  PDF文本层：%s" % str(ev["local_snippet"])[:300].replace("\n", " "))
        if tv.get("verdict") and tv.get("verdict") != "unknown":
            lines.append("  文本层判据：%s%s" % (tv["verdict"],
                                                "(决定性)" if tv.get("decisive") else "(未区分)"))
    got: dict = {}
    for attempt in range(2):
        _sys_prompt = _SYSTEM_SYNTH_SHORT if short else _SYSTEM_SYNTH
        _max = 1024 if short else 4096
        _extra = _THINKING_OFF_BODY if (short and no_thinking) else None
        try:
            msgs = [{"role": "system", "content": _sys_prompt},
                    {"role": "user", "content": "\n".join(lines)}]
            try:
                resp = provider.complete(msgs, max_tokens=_max, extra_body=_extra)
            except TypeError:
                # 兼容不认识 `extra_body` 的 provider/adapter（注入的第三方实现）——
                # 不能因为多传一个参数就让整篇的 AI 判定全部丢失（静默失效防线）。
                resp = provider.complete(msgs, max_tokens=_max)
            try:
                _RAW_LOG.parent.mkdir(parents=True, exist_ok=True)
                with _RAW_LOG.open("a", encoding="utf-8") as f:
                    f.write("=== synth batch=%d attempt=%d ===\n%s\n"
                            % (len(batch), attempt, resp))
            except Exception:  # noqa: BLE001
                pass
            for d in _parse_suggestions(resp):
                try:
                    got[int(d["id"])] = d
                except (TypeError, ValueError):
                    continue
            if len(got) >= len(batch):
                break
        except Exception:  # noqa: BLE001 - 保留 attempt=0 已拿到的部分
            continue
    return got


def synthesize(items: list[dict], *, provider: OpenAICompatProvider | None = None,
               batch_size: int = 10, paper: str = "",
               with_local: bool = True, depth: int = 0,
               short: bool | None = None,
               no_thinking: bool | None = None) -> list[Arbitration]:
    """[全局] **AI 综合建议**（用户 2026-09-16："让 AI 综合这些解析结果进行综合判断，
    给出自己的修改建议"）——与 `arbitrate()` 的本质区别：

      · `arbitrate`：只回答"M 和 P 哪个对"（选边）；
      · `synthesize`：**综合 M/P/句内上下文/PDF 文本层旁证**，给出 `suggested_text`
        —— 可以与两侧都不同（如 M 漏下标、P 漏字母 ⇒ 给出两者合并后的正确写法）。

    输入 items：`char_conflicts` 的冲突 dict（`mineru.text` / `paddleocr.text` /
    `evidence.m_ctx|p_ctx` / 可选 `evidence.local_snippet` 与 `third_vote`）。
    输出：`Arbitration`（含 `action`/`suggested_text`/`confidence`/`reason`/`evidence`）。

    ★2026-09-17（T9，A/B 实测换来的默认值）：`short=None` → 取 `PARSE_AI_SHORT`（默认 1）
    用极短 schema `{"id","a","t"}`；`no_thinking=None` → 取 `PARSE_AI_THINKING`（默认 0）
    一并请求关闭"思考"。实测（真实付费 AI，20 项/10 项）：现状 13 次调用 / 输出 36,832 token
    （1,842/项，7 次被推理挤到 `length`）⇒ 短 schema + 关思考后 1 次调用 / 输出 197 token
    （10/10 解析成功，语义与现状在"真正会送 AI 的项"上 5/5 一致）。两者**必须同时生效**。

    **可靠性**（实测：思考型模型在大批时会输出长篇推理而丢 JSON）：批内 2 次尝试仍拿不全时，
    把**未解析的项**降半批重试（最多 3 层）——小批时模型倾向直接给 JSON。
    """
    provider = provider or OpenAICompatProvider()
    short = _short_mode() if short is None else short
    no_thinking = _no_thinking() if no_thinking is None else no_thinking
    if not provider.available():
        return [_unresolved(_as_dict(it), i, "AI 综合建议不可用（未配置 Key）")
                for i, it in enumerate(items)]
    out_map: dict = {}
    work = [(k, it) for k, it in enumerate(items)]
    size = max(1, batch_size)
    for start in range(0, len(work), size):
        batch = work[start:start + size]
        out_map.update(_synthesize_once(batch, provider, paper, with_local,
                                        short=short, no_thinking=no_thinking))
    if depth < 3:
        missing = [(i, it) for i, it in work if i not in out_map]
        # 注意：**全部未解析时也要重试**（此前条件写成 len(missing) < len(work) ⇒ 整批失败
        # 反而不重试，实测第 1 次调用全丢、只能靠第 2 次预算救回）。
        if missing and len(work) > 2:
            half = max(2, max(1, size // 2))
            sub = synthesize([it for _i, it in missing], provider=provider,
                             batch_size=half, paper=paper, with_local=with_local,
                             depth=depth + 1, short=short, no_thinking=no_thinking)
            for (i, _it), a in zip(missing, sub):
                if a is not None and getattr(a, "action", ""):
                    out_map[i] = {"id": i, "action": a.action,
                                  "suggested_text": a.suggested_text,
                                  "confidence": a.confidence, "reason": a.reason,
                                  "evidence": a.evidence}
    out: list[Arbitration] = []
    for i, it in enumerate(items):
        d = out_map.get(i)
        if not d:
            out.append(_unresolved(_as_dict(it), i, "AI 综合建议未返回"))
            continue
        act = str(d.get("action", "")).strip().lower()
        sug = str(d.get("suggested_text", "") or "").strip()
        if act not in ("keep", "replace", "insert", "delete"):
            act = "keep"
        out.append(Arbitration(
            id=i, diff_type=it.get("type", ""), page=it.get("page", 0),
            # verdict 仍是"选边"语义（兼容旧统计/落地路径）：replace 类默认记 paddleocr；
            # 真正的文本以 suggested_text/action 为准（见 p14 的 AI 综合建议分支）。
            verdict=("paddleocr" if act in ("replace", "insert", "delete") else "mineru"),
            reason=str(d.get("reason", ""))[:60],
            confidence=float(d.get("confidence", 0) or 0),
            mineru=it.get("mineru", {}), paddleocr=it.get("paddleocr", {}),
            suggested_text=sug, action=act,
            evidence=[str(x) for x in (d.get("evidence") or [])][:6]))
    return out


_SHORT_ACTION = {"k": "keep", "r": "replace", "i": "insert", "d": "delete",
                 "keep": "keep", "replace": "replace", "insert": "insert",
                 "delete": "delete", "": "keep"}


def _parse_suggestions(text: str) -> list[dict]:
    """[局部] 综合建议输出解析：扫所有 `{...}` 小块（思考型模型会夹带散文）。

    比 `_parse_verdicts` 更宽松：只要块里有 `id` + (`action` 或短写 `a`) 即认。
    ★2026-09-17（T9 短 schema）：接受 `{"id":1,"a":"k|r|i|d","t":"…"}` 并归一成
    `{"id","action","suggested_text","confidence","reason"}`——短写能把输出从
    1,842 token/项压到 ~20 token/项（实测）。
    """
    out: list[dict] = []

    def _norm(d: dict) -> dict:
        if "action" in d:
            return d
        act = _SHORT_ACTION.get(str(d.get("a", "")).strip().lower(), "keep")
        return {"id": d.get("id"), "action": act,
                "suggested_text": d.get("t", d.get("suggested_text", "")),
                "confidence": d.get("c", d.get("confidence", 0.8)),
                "reason": d.get("r", "")}

    for m in re.finditer(r"\{[^{}]*\}", text or ""):
        try:
            d = json.loads(m.group(0))
        except ValueError:
            continue
        if isinstance(d, dict) and "id" in d and ("action" in d or "a" in d):
            out.append(_norm(d))
    if out:
        return out
    # 兜底：剥围栏后整体 json.loads
    t = re.sub(r"```(?:json)?", "", text or "").strip()
    try:
        data = json.loads(t)
        if isinstance(data, list):
            return [_norm(d) for d in data
                    if isinstance(d, dict) and "id" in d
                    and ("action" in d or "a" in d)]
    except ValueError:
        pass
    return []


def _as_dict(it) -> dict:
    """[局部] DiffItem dataclass / dict → dict"""
    if isinstance(it, dict):
        return it
    return {"type": getattr(it, "diff_type", ""), "page": getattr(it, "page", 0),
            "mineru": getattr(it, "mineru", {}) or {},
            "paddleocr": getattr(it, "paddleocr", {}) or {},
            "evidence": getattr(it, "evidence", {}) or {}}


def _unresolved(it: dict, i: int, reason: str) -> Arbitration:
    return Arbitration(id=i, diff_type=it.get("type", ""), page=it.get("page", 0),
                       verdict="unresolved", reason=reason, confidence=0.0,
                       mineru=it.get("mineru", {}), paddleocr=it.get("paddleocr", {}))


def build_arbitration(review_items: list[dict], arbitrations: list[Arbitration]) -> list[dict]:
    """[全局] 仲裁结果合并进 review 清单（供 review.json 落盘）

    按 report_idx 匹配（review 是 report.items 的子集，位置索引不可用）。
    """
    by_id = {a.id: a for a in arbitrations}
    merged = []
    for it in review_items:
        a = by_id.get(it.get("report_idx"))
        if a:
            it = dict(it)
            it["ai"] = {"verdict": a.verdict, "reason": a.reason,
                        "confidence": a.confidence}
        merged.append(it)
    return merged
