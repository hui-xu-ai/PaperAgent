# -*- coding: utf-8 -*-
"""翻译流水线（自 paperparse.llm.m5combined 迁入，适配 paperkb：dict 操作 + LLM 注入）。

- 译文任务：**共享全文前缀（header + 原文全文块）** + 翻译任务后缀；最终产出每段 text_zh
  （写回 document.json，供双语/中文版本）。公式 [[MATHn]] 全局标签 + reassemble 逐字节回填
  + KNOWN_FIXES 语义修正。
- 与编译共享前缀（D20/cache）：翻译与编译的请求都包含**同一份「全文」作为稳定前缀**（相同
  系统/结构前缀 + 相同论文全文块），使第 2+ 次调用命中「提示词缓存」（全文按缓存价）。前缀
  来源：``paperkb.context.shared_ctx``（字节一致）。
- ★整篇先（当前模型 1M 上下文 + ~128K 输出）：全部可译段落放同一 prompt 一次翻译 →
  连贯/术语一致/调用少（calls≈1）；仅当正文超 MAX_WHOLE_CHARS 或整篇 JSON 解析失败
  （输出截断等）才退化分批。
- D16：**翻译不再生成六维总结**（summary 恒空；六维总结交由 L1 编译 _note.md），全文块
  本身不含「独立 summary 步骤」，译文提示词仅在任务后缀描述翻译动作。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from ..context import CTX_HEADER, context_paragraphs, paper_context_with_math, with_task
from ..doc import read_document
from .fixes import KNOWN_FIXES
from .latextap import reassemble

logger = logging.getLogger(__name__)

# 分批因子放大（用户反馈：避免"很多小批"触发限流/大量请求）：
# 从多小批 → 少数大批。MAX_BATCH_PARAS 8→64、MAX_BODY_CHARS 10000→30000，配 max_tokens=64000
# （llm_service.DEFAULT_MAX_OUTPUT_TOKENS），单批译文输出不截断。保持串行（不并发）。
MAX_BODY_CHARS = 30000
MAX_BATCH_PARAS = 64
MAX_CALLS = 64
MAX_TOTAL_INPUT_CHARS = 300000
# 整篇一次阈值：全部可译段落正文总量 ≤ 此值才尝试整篇一次。取值需 ≤ TokenGuard 单次
# 输入字符红线（llm_service.MAX_INPUT_CHARS_PER_CALL = 400_000），否则整篇 prompt 会触发防护。
MAX_WHOLE_CHARS = 400000

# 紧凑模式（小上下文翻译专用模型）：
# 跳过共享全文前缀（专用模型无需提示词缓存共享），段落内联到 prompt，分块更小。
# 2026-09-19 实测（混元 MT-7B via 硅基流动）：**输出侧才是真正的瓶颈**——每批输出 ~1900 token
# 即截断（服务端/模型硬上限，max_tokens 请求参数无法覆盖），并非 32K 上下文装不下输入。
# 2026-09-19 实测安全边界（Qwen2.5-7B-Instruct via 硅基流动）：
# 日志推断——输出 ≤3000 token 稳定成功（2333/2862/2985/2130），4134 截断。
# 译文 token ≈ 英文源字符数（1:1），JSON 开销 ~200 token。
# 安全输出 ~2500 token → 正文上限 8000 字符（留 ~500 token 安全余量）。
# 段数 ≤15（过多段 JSON 结构开销增大）。超长段落（>COMPACT_UNIT_MAX）走句子切块。
# 2026-09-20 下调（用户反馈：仍有截断补跑重复消耗）：8000→6000、段数 15→12。
# 完整性保证：_make_batches 只整段入批（size+len(t)>max_body 即开新批，不切段中）；
# 超长单段（>COMPACT_UNIT_MAX）走 _split_sentences 按句切块。故降阈值不破坏段/句完整。
COMPACT_MAX_BODY_CHARS = 6000
COMPACT_MAX_BATCH_PARAS = 12
COMPACT_MAX_CALLS = 200
COMPACT_MAX_WHOLE_CHARS = 10000
# 单翻译单元（整段或长段的句子块）英文上限——超过则触发句子级切块。
COMPACT_UNIT_MAX = 5000

# 翻译源跳过 References（用户反馈：参考文献无需翻译，浪费 token/请求）。
# **2026-09-12 批4 修正（单一判据）**：跳过的判据不再自己写一套，而是**直接复用共享上下文的
# `context_paragraphs(doc)`**（= 尾部杂项截断 + References 类章节过滤）。
# 旧实现自带 `_reference_cut`（只看 section/heading 名）⇒ 与共享上下文口径不一致：实测某篇的
# 参考文献条目被解析层误标成 `section="Keywords"`，`_reference_cut` 切不到 ⇒ 这些段落进了
# "待译清单"却**没有出现在给模型的全文里** ⇒ 模型只能回「（原文未提供该段内容，无法翻译。）」
# 66 处（zh.md/en_zh.md 各 66），严重污染译文。现在"能翻译的段落"严格 ⊆ "模型看得到的段落"。


def _reference_cut(paras: list[dict]) -> int | None:
    """（已废弃，批4）旧的自建 References 判据 —— 保留仅为兼容外部调用。

    ⚠️ **不要在新代码里使用**：它与共享上下文的口径不一致（只看 section/heading 名，拦不住
    section 被误标的参考文献条目），曾导致 66 处「（原文未提供该段内容，无法翻译。）」污染译文。
    现行单一判据 = `paperkb.context.context_paragraphs(doc)`（见 `run_translate`）。
    """
    from ..context import _is_ref_section

    for i, pp in enumerate(paras):
        if _is_ref_section(pp.get("section") or ""):
            return i
        if pp.get("is_heading") and _is_ref_section(pp.get("text_en") or ""):
            return i
    return None


_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# HTML 标签清理（2026-09-19 用户反馈：译文含 <sup>[21,22]</sup> 等标签无法渲染）
# 模型有时会在译文中保留原文的 HTML 标签（特别是上标引用），需要清理。
# 保留 [[MATHn]] 和 [[NOTE]] 等自定义标签，只清理标准 HTML 标签。
_HTML_TAG_RE = re.compile(r'</?(?:sup|sub|em|strong|b|i|u|span|div|p|br|a|img|table|tr|td|th|ul|ol|li|h[1-6])[^>]*>', re.IGNORECASE)


def _strip_html_tags(text: str) -> str:
    """清理标准 HTML 标签（保留 [[MATHn]] 等自定义标记）。"""
    return _HTML_TAG_RE.sub('', text or '')


# 嵌套 $...$ 修复（2026-09-20 用户反馈：zh.md/en_zh.md 中出现 $\mathrm{Co(O$_{x}$/P$_{x}$)@P-LIG}$）
# LLM 有时会错误地把 [[MATHn]] 展开为嵌套 $...$ 格式（如 $_{x}$），导致 LaTeX 渲染失败。
# 需要把内层的 $...$ 剥离，只保留内容（_{x}）。


def _fix_nested_dollars(text: str) -> str:
    """修复嵌套 $...$：把 $_{x}$ → _{x}、$^{2}$ → ^{2} 等内层定界符剥离。
    
    仅处理**外层已有 $...$ 包裹**的情况（如 $\mathrm{...}_{x}...$ 内的 $_{x}$）。
    独立公式（整段就是 $...$）不动。
    """
    if not text or '$' not in text:
        return text
    
    # 策略：找到所有 $...$ 区间，检查是否有嵌套的 $...$
    # 如果有，剥离内层的所有 $ 定界符对
    result = []
    i = 0
    n = len(text)
    
    while i < n:
        if text[i] == '$':
            # 找到最后一个 $ 作为闭合定界符（贪婪匹配）
            j = text.rfind('$', i + 1)
            if j != -1 and j > i + 1:
                inner = text[i+1:j]
                # 检查内层是否有嵌套的 $...$
                if '$' in inner:
                    # 剥离内层的所有 $ 定界符对：$content$ → content
                    # 使用循环直到没有更多 $...$ 对
                    while '$' in inner:
                        inner = re.sub(r'\$([^$]*)\$', r'\1', inner)
                    result.append('$' + inner + '$')
                else:
                    result.append(text[i:j+1])
                i = j + 1
            else:
                result.append(text[i])
                i += 1
        else:
            result.append(text[i])
            i += 1
    
    return ''.join(result)

# 模型"拒绝/占位"式译文（批4 防御）：这类文本**不是译文**，绝不能落进 text_zh（否则会
# 渲染进 zh.md/en_zh.md 污染阅读）。实测来源：目标段落没出现在给模型的全文里 → 模型回
# 「（原文未提供该段内容，无法翻译。）」。根因已修（待译清单与共享上下文同源），此处兜底
# 任何形式的拒绝文本（含英文变体）。命中即按 rejected 丢弃（该段保留原文，渲染时回退英文）。
_REFUSAL_ZH_RE = re.compile(
    r"(原文未提供|未提供该段|无法翻译|无法完成翻译|无法进行翻译|不能翻译|"
    r"缺少原文|原文缺失|未给出原文|not provided in the (original|source)|"
    r"cannot translate|unable to translate|no source text)", re.IGNORECASE)


def is_refusal_translation(text: str) -> bool:
    """是否为"拒绝/占位"式译文（非译文）——**公开**：清理工具与流水线共用同一判据。"""
    t = (text or "").strip()
    if not t:
        return False
    return bool(_REFUSAL_ZH_RE.search(t))


def _is_refusal(text: str) -> bool:
    """内部别名（与 `is_refusal_translation` 同一判据）。"""
    return is_refusal_translation(text)


def _strip_control(text: str) -> str:
    return _CTRL_RE.sub("", text or "")


# 非法 JSON 字符串转义修复：模型常把 LaTeX（如 $_{800^{\circ}C}$）以**单反斜杠**写进 JSON 字符串，
# `\c` 是非法转义 → json.loads 失败。把"非合法 JSON 转义"的反斜杠补成双反斜杠（合法 → 还原为单反斜杠）。
# 合法 JSON 转义：\" \\ \/ \b \f \n \r \t \uXXXX；其余一律视为 LaTeX 残留而修复。
_INVALID_JSON_ESC_RE = re.compile(r'\\(?!(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))')


def _repair_json_escapes(s: str) -> str:
    return _INVALID_JSON_ESC_RE.sub(lambda m: "\\\\", s)


def _loads_lenient(s: str) -> dict | None:
    """json.loads，失败时尝试修复非法 LaTeX 转义再解析。返回 dict 或 None。"""
    try:
        d = json.loads(s)
        return d if isinstance(d, dict) else None
    except json.JSONDecodeError:
        pass
    try:
        d = json.loads(_repair_json_escapes(s))
        return d if isinstance(d, dict) else None
    except json.JSONDecodeError:
        return None


def _balanced_extract(text: str, start: int) -> dict | None:
    """从 text[start]=='{' 起做平衡括号提取，返回解析成功的 dict；否则 None。
    处理嵌套花括号/字符串内引号与转义（含 LaTeX 非法转义修复）。"""
    depth = in_str = esc = 0
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                data = _loads_lenient(text[start:i + 1])
                if data is not None:
                    return data
                return None
    return None


def _parse_json(text: str) -> dict:
    t = _strip_control(text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S)
    # 1) 直接解析（最干净路径，含 LaTeX 非法转义修复）
    data = _loads_lenient(t)
    if data is not None:
        return data
    # 2) 优先找含预期键（summary/translations）的 JSON 对象——容忍 reasoning 前缀/
    #    前后杂文本（智谱 glm-flash 等推理模型常带思维链/说明文字）
    for key in ("summary", "translations"):
        for m in re.finditer(r'\{"%s"' % key, t):
            obj = _balanced_extract(t, m.start())
            if obj is not None:
                return obj
    # 3) 通用：从任意 '{' 平衡提取
    for m in re.finditer(r"\{", t):
        obj = _balanced_extract(t, m.start())
        if obj is not None:
            return obj
    logger.warning("翻译输出 JSON 解析失败，原始片段: %r", t[:500])
    raise ValueError("翻译输出 JSON 解析失败")


def _parse_plain_translation(text: str, expected_ids: list[str]) -> dict:
    """纯文本翻译输出解析后备（部分模型不输出 JSON，而是按段落标记输出译文）。

    识别格式：
      [P001] 译文文本...
      [P002] 译文文本...
    或：
      P001: 译文文本...
      P002: 译文文本...

    返回与 _parse_json 相同的 dict 结构：{"translations": [{"para_id": ..., "zh": ...}]}。
    """
    t = _strip_control(text or "").strip()
    t = re.sub(r"^```(?:json|text)?\s*|\s*```$", "", t, flags=re.S)

    results = []
    # 匹配 [P001] 或 P001: 或 **P001** 等标记
    pattern = re.compile(
        r'(?:\[(' + '|'.join(re.escape(pid) for pid in expected_ids if pid) + r')\]'
        r'|(' + '|'.join(re.escape(pid) for pid in expected_ids if pid) + r')\s*[:：])',
        re.IGNORECASE
    )
    matches = list(pattern.finditer(t))
    # 2026-09-19 修复：原实现要求 ≥2 个匹配，但**单段补跑批次**只有 1 个段落标记，
    # 被误判"无结果"→ 截断补跑恒失败。改为 ≥1 即可（无匹配才返回空）。
    if not matches:
        return {"translations": []}

    for i, m in enumerate(matches):
        pid = m.group(1) or m.group(2)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(t)
        zh = t[start:end].strip()
        # 清理前导标点/空白
        zh = zh.lstrip("：: \n\t")
        if zh:
            results.append({"para_id": pid, "zh": zh})

    return {"translations": results}


def _translate_task(para_ids: list[str]) -> str:
    """译文任务后缀（参考上方全文，翻译指定段落，保留 [[MATHn]] 标签）。

    前缀（header + 全文块）由调用方拼好在此任务后缀之前；任务只描述「翻译动作」，
    不再出现「独立 summary 步骤」（D16：译文阶段不生成六维总结）。
    """
    ids = ", ".join(str(p) for p in para_ids if p)
    return (
        "## 翻译任务\n"
        "请将上方论文全文中的以下段落翻译为中文。译文里**用 [[MATHn]] 标签原样指代公式**，"
        "绝不展开、绝不改写 LaTeX；保留该段所有 [[MATHn]] 标签（顺序一致）。\n"
        "题注段按图/表题注译（Figure N.→图 N.）。\n"
        f"待译段落（para_id）：{ids}\n"
        "输出严格 JSON：\n"
        '{"translations": [{"para_id": "P001", "zh": "..."}]}\n'
        "若某公式疑似有误，在对应 zh 末尾加 [[NOTE]] 说明，不要改标签。"
    )


def _translate_task_compact(paras: list[dict], batch_indices: list[int],
                            prev_context: str = "") -> str:
    """紧凑模式翻译任务（自包含：段落文本内联，无共享前缀）。

    用于小上下文翻译专用模型（如 Hunyuan-MT-7B 32K）。段落文本直接嵌入 prompt，
    模型无需"看全文"——只看本批待译段落。输出格式同样要求 JSON，但解析失败时有纯文本后备。
    2026-09-19：增加 prev_context（前一批译文末尾）改善跨批术语/指代一致性。
    """
    parts = []
    for idx in batch_indices:
        p = paras[idx]
        pid = p.get("para_id") or ""
        text = (p.get("text_en") or "").strip()
        if pid and text:
            parts.append(f"[{pid}] {text}")
    paras_text = "\n\n".join(parts)
    ctx_line = ""
    if prev_context:
        ctx_line = f"前文参考（仅供理解上下文，不要翻译）：\n{prev_context}\n\n"
    return (
        "请将以下学术论文段落翻译为中文。\n"
        "规则：\n"
        "1. 保留 [[MATHn]] 公式标签原样，不展开不改写\n"
        "2. 图/表题注按「图 N.」「表 N.」格式翻译\n"
        "3. 术语与前文保持一致\n"
        "4. 每段译文以对应段落标记开头，格式：[P001] 译文内容\n\n"
        + ctx_line
        + "待翻译段落：\n\n"
        f"{paras_text}"
    )


def _apply_translations(data_out: dict, paras: list[dict],
                        para_id_to_idx: dict[str, int], math_list: list[str],
                        ) -> tuple[dict, int]:
    """LLM 翻译结果 → 逐段最终译文回填（reassemble + KNOWN_FIXES + 清洗）。

    整篇与分批共用**同一套**「译文 → paras[idx]['text_zh']」后处理，保证写回行为一致。
    返回 ({idx: 最终zh（已清洗，空串丢弃计 rejected）}, rejected)。

    para_id_to_idx：{para_id: paras 下标}；math_list：全局公式表（全文块 [[MATHn]] 序），
    由 paper_context_with_math 产出——与共享前缀中的全局编号一致。
    """
    out: dict = {}
    rejected = 0
    for it in (data_out or {}).get("translations", []) or []:
        pid = it.get("para_id")
        if not pid or not it.get("zh"):
            continue
        idx = para_id_to_idx.get(pid)
        if idx is None:
            continue
        result = reassemble(it["zh"], math_list)
        for pat, repl, _note in KNOWN_FIXES:
            result = re.sub(pat, lambda _mm, _r=repl: _r, result)  # lambda 防转义解析
        result = _strip_control(result)
        result = _strip_html_tags(result)  # 2026-09-19：清理 <sup> 等 HTML 标签
        result = _fix_nested_dollars(result)  # 2026-09-20：修复嵌套 $...$（$_{x}$ → _{x}）
        if not result.strip() or _is_refusal(result):
            rejected += 1
            continue
        out[idx] = result
    return out, rejected


def _do_batch(llm, shared: str, paras: list[dict], batch: list[int],
              para_id_to_idx: dict[str, int], math_list: list[str], context: str,
              calls: list[int], *, compact: bool = False,
              prev_context: str = "") -> tuple[dict, int]:
    """翻译单个批次：单次 LLM + **同 prompt 最多重试一次（不递归拆批）**，失败上抛。

    旧实现（上一轮）在 JSON 解析失败时把批切成两半递归重试（级联重复调用/token 浪费）。
    现 max_tokens 已增至 64000，正常批不会截断；同 prompt 重试一次仍失败则上抛，不再
    缩小批。返回的 zh 已 reassemble + KNOWN_FIXES + 清洗（空串丢弃计 rejected）。

    compact=True：紧凑模式（小上下文模型），段落内联、无共享前缀、JSON 失败→纯文本后备。
    2026-09-19：prev_context 传入前一批译文末尾，改善跨批连续性。
    """
    para_ids = [paras[i].get("para_id") for i in batch]
    if compact:
        prompt = _translate_task_compact(paras, batch, prev_context=prev_context)
    else:
        prompt = with_task(shared, _translate_task(para_ids))
    data_out: dict | None = None
    for attempt in (0, 1):
        try:
            raw = llm.complete(prompt, context=context)
            calls[0] += 1
            try:
                data_out = _parse_json(raw)
            except ValueError:
                if compact:
                    # 紧凑模式后备：部分模型（如 Hunyuan-MT）不输出 JSON 而输出纯文本标记
                    data_out = _parse_plain_translation(raw, para_ids)
                    if not data_out.get("translations"):
                        raise ValueError("纯文本解析也无结果")
                else:
                    raise
            break
        except ValueError:
            if attempt == 1:
                raise
            logger.warning("译文批 %d 段解析失败，重试一次", len(batch))
    out, rejected = _apply_translations(data_out, paras, para_id_to_idx, math_list)
    return out, rejected


def _make_batches(paras: list[dict], targets: list[int],
                  *, compact: bool = False) -> tuple[list[list[int]], int]:
    """按段数 + 字符数分批（现有 /targets 分批逻辑），返回 (batches, truncated)。

    compact=True：使用紧凑模式常量（更小的批，更多的批次数）。
    """
    max_body = COMPACT_MAX_BODY_CHARS if compact else MAX_BODY_CHARS
    max_paras = COMPACT_MAX_BATCH_PARAS if compact else MAX_BATCH_PARAS
    max_calls = COMPACT_MAX_CALLS if compact else MAX_CALLS
    batches: list[list[int]] = []
    cur: list[int] = []
    size = 0
    for idx in targets:
        t = paras[idx].get("text_en") or ""
        if cur and (len(cur) >= max_paras or size + len(t) > max_body):
            batches.append(cur)
            cur = [idx]
            size = len(t)
        else:
            cur.append(idx)
            size += len(t)
    if cur:
        batches.append(cur)
    truncated = 0
    if len(batches) > max_calls:
        truncated = len(batches) - max_calls
        batches = batches[:max_calls]
    # 紧凑模式跳过总字符上限检查（小模型分批多，总量由批数控制）
    if not compact:
        kept, acc = [], 0
        for batch in batches:
            chars = sum(len(paras[i].get("text_en") or "") for i in batch)
            if acc + chars > MAX_TOTAL_INPUT_CHARS:
                truncated += len(batches) - len(kept)
                break
            acc += chars
            kept.append(batch)
        batches = kept
    return batches, truncated


def _split_sentences(text: str, max_chars: int) -> list[str]:
    """把超长段落按句子边界切成 ≤max_chars 的块（尽量保持句子完整）。

    用于紧凑模式超大段落：模型输出上限 ~1900 token，单段英文 >COMPACT_UNIT_MAX 时整段必被
    截断，只能句子级切块逐块翻译再拼接。单句仍超限时硬切（极端罕见）。
    2026-09-19：末尾句不切分，归入下一块作为连续性上下文（翻译时跳过）。
    """
    parts = re.split(r'(?<=[。！？；.!?;])\s+|(?<=\n)', text)
    chunks: list[str] = []
    cur = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if len(cur) + len(p) + 1 <= max_chars:
            cur = (cur + " " + p).strip()
        else:
            if cur:
                chunks.append(cur)
            while len(p) > max_chars:          # 单句超上限 → 硬切兜底
                chunks.append(p[:max_chars])
                p = p[max_chars:]
            cur = p
    if cur:
        chunks.append(cur)
    return chunks or [text[:max_chars]]


def _chunks_task(chunk_ids: list[str], chunks: list[str],
                 prev_context: str = "") -> str:
    """句子块翻译任务（紧凑模式超大段落专用，自包含，输出 JSON 或纯文本标记）。

    2026-09-19：增加 prev_context（前一块末尾句）作为连续性参考，改善跨块指代/术语一致性。
    """
    body = "\n\n".join(f"[{cid}] {ct}" for cid, ct in zip(chunk_ids, chunks))
    ctx_line = ""
    if prev_context:
        ctx_line = (
            f"\n前文参考（仅供理解上下文，不要翻译）：\n{prev_context}\n\n"
        )
    return (
        "请将以下学术论文句子块翻译为中文（按标记一一对应，不要合并、不要遗漏）。\n"
        "规则：\n"
        "1. 保留 [[MATHn]] 公式标签原样，不展开不改写\n"
        "2. 术语与前文保持一致\n"
        "3. 输出严格 JSON：\n"
        '{"translations": [{"para_id": "标记", "zh": "译文"}, ...]}\n\n'
        + ctx_line
        + "待翻译句子块：\n\n"
        f"{body}"
    )


def _translate_oversized(llm, paras: list[dict], targets: list[int],
                         math_list: list[str], context: str,
                         calls: list[int]) -> tuple[int, int]:
    """紧凑模式：翻译超过 COMPACT_UNIT_MAX 的超长段落（句子切块 → 逐块组批 → 拼接）。

    返回 (translated, rejected)。块组批按输出预算（COMPACT_UNIT_MAX）打包，块标记用
    f"{para_id}__c{序号}"，解析后按序拼接回父段 text_zh。
    """
    translated = rejected = 0
    for idx in targets:
        text = (paras[idx].get("text_en") or "").strip()
        if len(text) <= COMPACT_UNIT_MAX:
            continue
        pid = paras[idx].get("para_id") or ""
        chunks = _split_sentences(text, COMPACT_UNIT_MAX)
        chunk_ids = [f"{pid}__c{ci}" for ci in range(len(chunks))]

        # 组批：块总字符 ≤ COMPACT_UNIT_MAX（输出预算内不截断）
        cb_batches: list[tuple[list[str], list[str]]] = []
        cur_ids: list[str] = []
        cur_chunks: list[str] = []
        size = 0
        for cid, ct in zip(chunk_ids, chunks):
            if cur_ids and size + len(ct) > COMPACT_UNIT_MAX:
                cb_batches.append((cur_ids, cur_chunks))
                cur_ids, cur_chunks, size = [], [], 0
            cur_ids.append(cid)
            cur_chunks.append(ct)
            size += len(ct)
        if cur_ids:
            cb_batches.append((cur_ids, cur_chunks))

        zh_parts: list[str] = []
        failed = False
        prev_zh_tail = ""  # 前一批译文末尾，作为下一批连续性上下文
        for cb_ids, cb_texts in cb_batches:
            prompt = _chunks_task(cb_ids, cb_texts, prev_context=prev_zh_tail)
            data_out: dict | None = None
            for attempt in (0, 1):
                try:
                    raw = llm.complete(prompt, context=context)
                    calls[0] += 1
                    try:
                        data_out = _parse_json(raw)
                    except ValueError:
                        data_out = _parse_plain_translation(raw, cb_ids)
                        if not data_out.get("translations"):
                            raise ValueError("句子块纯文本解析无结果")
                    break
                except ValueError:
                    if attempt == 1:
                        failed = True
                    else:
                        logger.warning("句子块批 %d 块解析失败，重试一次", len(cb_ids))
            if failed:
                break
            got = {t["para_id"]: t["zh"] for t in (data_out or {}).get("translations", []) or []}
            # 按批内块顺序拼接（缺块留空，保证顺序）
            batch_zh = "".join(got.get(cid, "") for cid in cb_ids)
            zh_parts.append(batch_zh)
            # 取本批译文末尾 ~200 字作为下一批连续性上下文
            prev_zh_tail = batch_zh[-200:] if len(batch_zh) > 200 else batch_zh
        if failed:
            rejected += 1
            continue
        full = reassemble("".join(zh_parts), math_list)
        for pat, repl, _n in KNOWN_FIXES:
            full = re.sub(pat, lambda _m, _r=repl: _r, full)
        full = _strip_control(full)
        full = _strip_html_tags(full)  # 2026-09-19：清理 <sup> 等 HTML 标签
        full = _fix_nested_dollars(full)  # 2026-09-20：修复嵌套 $...$（$_{x}$ → _{x}）
        if full.strip() and not _is_refusal(full):
            paras[idx]["text_zh"] = full
            translated += 1
        else:
            rejected += 1
    return translated, rejected


def _run_batches(llm, shared: str, paras: list[dict], targets: list[int],
                 para_id_to_idx: dict[str, int], math_list: list[str], context: str,
                 calls: list[int], *, compact: bool = False) -> tuple[tuple[int, int], int]:
    """分批翻译全部 target，返回 ((translated, rejected), truncated）。

    紧凑模式兜底：模型输出超限会只译出批次开头若干段（后面的被截断，纯文本解析照过不误），
    因此每批后比对"应译 vs 实译"，把未译段落按更小的单段批补跑（单段必不超输出容量）。
    """
    # 紧凑模式：超长段落（>COMPACT_UNIT_MAX）从普通批次剔除，走句子切块专用通道
    #（普通批次按 COMPACT_MAX_BODY_CHARS=COMPACT_UNIT_MAX 装不下它们，硬塞必截断）。
    if compact:
        normal = [i for i in targets
                  if len((paras[i].get("text_en") or "")) <= COMPACT_UNIT_MAX]
        oversized = [i for i in targets if i not in set(normal)]
    else:
        normal = targets
        oversized = []
    batches, truncated = _make_batches(paras, normal, compact=compact)
    translated = rejected = 0
    prev_zh_tail = ""  # 前一批译文末尾，作为下一批连续性上下文
    for batch in batches:
        tr_map, rej = _do_batch(llm, shared, paras, batch, para_id_to_idx,
                                math_list, context, calls, compact=compact,
                                prev_context=prev_zh_tail)
        rejected += rej
        batch_zh = ""
        for idx, zh in tr_map.items():
            paras[idx]["text_zh"] = zh
            translated += 1
            batch_zh += zh
        # 取本批译文末尾 ~200 字作为下一批连续性上下文
        prev_zh_tail = batch_zh[-200:] if len(batch_zh) > 200 else batch_zh
        # 截断补跑（仅紧凑模式）：本批应译未译的段落单独成批重译
        if compact:
            missing = [i for i in batch if i not in tr_map]
            if missing:
                logger.warning("紧凑批截断：%d/%d 段未译，按单段补跑",
                               len(missing), len(batch))
                for idx in missing:
                    try:
                        m_map, m_rej = _do_batch(llm, shared, paras, [idx],
                                                 para_id_to_idx, math_list, context,
                                                 calls, compact=compact)
                        rejected += m_rej
                        for mi, zh in m_map.items():
                            paras[mi]["text_zh"] = zh
                            translated += 1
                    except Exception as e:  # noqa: BLE001 - 单段补跑失败保留原文
                        logger.warning("单段补跑失败（%s）保留原文: %s",
                                       paras[idx].get("para_id"), e)
    # 紧凑模式：超长段落句子切块翻译
    if oversized:
        o_tr, o_rej = _translate_oversized(llm, paras, oversized, math_list,
                                           context, calls)
        translated += o_tr
        rejected += o_rej
    return (translated, rejected), truncated


def _try_whole(llm, shared: str, paras: list[dict], targets: list[int],
               para_id_to_idx: dict[str, int], math_list: list[str], context: str,
               calls: list[int], *, compact: bool = False) -> tuple[dict | None, int]:
    """整篇一次翻译：全部 target 一次标注。

    正常模式：共享全文前缀 + 翻译任务后缀（大上下文模型，提示词缓存命中）。
    紧凑模式：段落内联（小上下文模型），无共享前缀。

    返回 ({idx: zh} | None=需回退分批, rejected)。解析失败/超限 → 返回 None 交上层回退分批。
    """
    para_ids = [paras[i].get("para_id") for i in targets]
    if compact:
        prompt = _translate_task_compact(paras, targets)
        max_whole = COMPACT_MAX_WHOLE_CHARS
    else:
        prompt = with_task(shared, _translate_task(para_ids))
        max_whole = MAX_WHOLE_CHARS
    if len(prompt) > max_whole:
        logger.info("整篇一次正文超限（prompt %d > %d），回退分批", len(prompt), max_whole)
        return None, 0
    try:
        raw = llm.complete(prompt, context=context)
        calls[0] += 1
        try:
            data_out = _parse_json(raw)
        except ValueError:
            if compact:
                data_out = _parse_plain_translation(raw, para_ids)
                if not data_out.get("translations"):
                    raise ValueError("纯文本解析也无结果")
            else:
                raise
    except ValueError:
        logger.warning("整篇一次翻译解析失败，回退分批")
        return None, 0
    except Exception as e:  # noqa: BLE001
        logger.warning("整篇一次翻译异常(%s)，回退分批", e)
        return None, 0
    out, rejected = _apply_translations(data_out, paras, para_id_to_idx, math_list)
    return out, rejected


def run_translate(doc_path: str | Path, llm, *, context: str = "translate",
                  context_path: str | Path | None = None,
                  compact: bool = False) -> dict:
    """翻译 document.json：**整篇一次优先**，失败回退分批，写回 text_zh。

    - **正常模式**（compact=False）：共享全文前缀 + 翻译任务后缀（大上下文模型，提示词缓存命中）。
    - **紧凑模式**（compact=True）：段落内联、无共享前缀、更小分批（小上下文翻译专用模型，
      如 Hunyuan-MT-7B 32K）。JSON 解析失败时自动回退纯文本标记解析。

    compact 模式跳过共享前缀（专用模型无需与编译共享缓存），每批 ≤ COMPACT_MAX_BODY_CHARS。

    llm: paperkb.llm.LLMClient 实现。
    返回: {"translated", "rejected", "targets", "calls", "summary", "truncated"}
    """
    p = Path(doc_path)
    data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    paras = data.get("paragraphs") or []

    # 上下文/待译清单的来源（与编译同源）；写回目标始终是 `p`。
    ctx_p = Path(context_path) if context_path else p
    doc = read_document(str(ctx_p))
    block, math_list = paper_context_with_math(doc)
    shared = CTX_HEADER + block

    para_id_to_idx: dict[str, int] = {}
    for i, pp in enumerate(paras):
        pid = pp.get("para_id") or ""
        if pid:
            para_id_to_idx.setdefault(pid, i)

    allowed_ids = {p.para_id for p in context_paragraphs(doc) if getattr(p, "para_id", "")}
    targets = [i for i, pp in enumerate(paras)
               if (pp.get("para_id") or "") in allowed_ids
               and not pp.get("is_heading") and (pp.get("text_en") or "").strip()]
    total_chars = sum(len(paras[i].get("text_en") or "") for i in targets)

    calls_box = [0]
    translated = rejected = truncated = 0

    whole_limit = COMPACT_MAX_WHOLE_CHARS if compact else MAX_WHOLE_CHARS
    if total_chars > 0 and total_chars <= whole_limit:
        whole_map, whole_rej = _try_whole(llm, shared, paras, targets, para_id_to_idx,
                                          math_list, context, calls_box, compact=compact)
        if whole_map is not None:
            rejected += whole_rej
            for idx, zh in whole_map.items():
                paras[idx]["text_zh"] = zh
                translated += 1
        else:
            (translated, rejected), truncated = _run_batches(
                llm, shared, paras, targets, para_id_to_idx, math_list, context,
                calls_box, compact=compact)
    else:
        (translated, rejected), truncated = _run_batches(
            llm, shared, paras, targets, para_id_to_idx, math_list, context,
            calls_box, compact=compact)

    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"translated": translated, "rejected": rejected, "targets": len(targets),
            "calls": calls_box[0], "summary": {}, "truncated": truncated}
