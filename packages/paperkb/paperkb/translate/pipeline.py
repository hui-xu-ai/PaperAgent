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


def _balanced_extract(text: str, start: int) -> dict | None:
    """从 text[start]=='{' 起做平衡括号提取，返回解析成功的 dict；否则 None。
    处理嵌套花括号/字符串内引号与转义。"""
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
                try:
                    data = json.loads(text[start:i + 1])
                    return data if isinstance(data, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


def _parse_json(text: str) -> dict:
    t = _strip_control(text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S)
    # 1) 直接解析（最干净路径）
    try:
        data = json.loads(t)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
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
        if not result.strip() or _is_refusal(result):
            rejected += 1
            continue
        out[idx] = result
    return out, rejected


def _do_batch(llm, shared: str, paras: list[dict], batch: list[int],
              para_id_to_idx: dict[str, int], math_list: list[str], context: str,
              calls: list[int]) -> tuple[dict, int]:
    """翻译单个批次：单次 LLM + **同 prompt 最多重试一次（不递归拆批）**，失败上抛。

    旧实现（上一轮）在 JSON 解析失败时把批切成两半递归重试（级联重复调用/token 浪费）。
    现 max_tokens 已增至 64000，正常批不会截断；同 prompt 重试一次仍失败则上抛，不再
    缩小批。返回的 zh 已 reassemble + KNOWN_FIXES + 清洗（空串丢弃计 rejected）。
    """
    para_ids = [paras[i].get("para_id") for i in batch]
    prompt = with_task(shared, _translate_task(para_ids))
    data_out: dict | None = None
    for attempt in (0, 1):
        try:
            raw = llm.complete(prompt, context=context)
            calls[0] += 1
            data_out = _parse_json(raw)
            break
        except ValueError:
            if attempt == 1:
                raise
            logger.warning("译文批 %d 段 JSON 解析失败，重试一次", len(batch))
    out, rejected = _apply_translations(data_out, paras, para_id_to_idx, math_list)
    return out, rejected


def _make_batches(paras: list[dict], targets: list[int]) -> tuple[list[list[int]], int]:
    """按段数 + 字符数分批（现有 /targets 分批逻辑），返回 (batches, truncated)。"""
    batches: list[list[int]] = []
    cur: list[int] = []
    size = 0
    for idx in targets:
        t = paras[idx].get("text_en") or ""
        if cur and (len(cur) >= MAX_BATCH_PARAS or size + len(t) > MAX_BODY_CHARS):
            batches.append(cur)
            cur = [idx]
            size = len(t)
        else:
            cur.append(idx)
            size += len(t)
    if cur:
        batches.append(cur)
    truncated = 0
    if len(batches) > MAX_CALLS:
        truncated = len(batches) - MAX_CALLS
        batches = batches[:MAX_CALLS]
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


def _run_batches(llm, shared: str, paras: list[dict], targets: list[int],
                 para_id_to_idx: dict[str, int], math_list: list[str], context: str,
                 calls: list[int]) -> tuple[tuple[int, int], int]:
    """分批翻译全部 target，返回 ((translated, rejected), truncated)。"""
    batches, truncated = _make_batches(paras, targets)
    translated = rejected = 0
    for batch in batches:
        tr_map, rej = _do_batch(llm, shared, paras, batch, para_id_to_idx,
                                math_list, context, calls)
        rejected += rej
        for idx, zh in tr_map.items():
            paras[idx]["text_zh"] = zh
            translated += 1
    return (translated, rejected), truncated


def _try_whole(llm, shared: str, paras: list[dict], targets: list[int],
               para_id_to_idx: dict[str, int], math_list: list[str], context: str,
               calls: list[int]) -> tuple[dict | None, int]:
    """整篇一次翻译：全部 target 一次标注（共享全文前缀含全部段落 + 全局 [[MATHn]]）组成
    单个 prompt，单次 llm.complete + _parse_json + 逐段回填。

    返回 ({idx: zh} | None=需回退分批, rejected)。解析失败（被输出截断等）→ 返回 None
    交给上层回退分批。整篇 prompt 过大（>MAX_WHOLE_CHARS）同样返回 None（防 TokenGuard 红线）。
    """
    para_ids = [paras[i].get("para_id") for i in targets]
    prompt = with_task(shared, _translate_task(para_ids))
    # 预检：整篇 prompt 超过 MAX_WHOLE_CHARS 会触发 TokenGuard 单次输入红线
    # （guard 上限与 MAX_WHOLE_CHARS 同值）→ 不尝试整篇，交给上层回退分批。
    if len(prompt) > MAX_WHOLE_CHARS:
        logger.info("整篇一次正文超限（prompt %d > %d），回退分批", len(prompt), MAX_WHOLE_CHARS)
        return None, 0
    try:
        raw = llm.complete(prompt, context=context)
        calls[0] += 1
        data_out = _parse_json(raw)
    except ValueError:
        logger.warning("整篇一次翻译 JSON 解析失败，回退分批")
        return None, 0
    except Exception as e:  # noqa: BLE001 - 超时/网络/限流等 LLM 异常也回退分批（不再整篇失败）
        logger.warning("整篇一次翻译异常(%s)，回退分批", e)
        return None, 0
    out, rejected = _apply_translations(data_out, paras, para_id_to_idx, math_list)
    return out, rejected


def run_translate(doc_path: str | Path, llm, *, context: str = "translate",
                  context_path: str | Path | None = None) -> dict:
    """翻译 document.json：**整篇一次优先**（共享全文前缀入上下文，单请求、串行、看全文再译），
    失败（超时/解析/超限）回退分批，写回 text_zh。

    - **共享全文前缀**：请求前缀 = header + 论文全文块（paper_context，text_en 干净正文，
      跳 References，公式 [[MATHn]] 全局占位）。与编译共享同一前缀 → 提示词缓存命中。
    - **上下文来源与编译同源（批4）**：`context_path` 指定"构造全文前缀与待译清单"的那份
      document.json（生产由 `api.translate_paper` 传 `shared_doc_json(key)` = kb 快照优先）；
      **译文仍写回 `doc_path`**（翻译真相源在 library）。不传则退化为同一份（旧行为）。
      ⚠️ 为什么必须同源：编译读 kb 快照、翻译曾读 library 正本，两份一旦有差异（复核写回只重写
      library）`shared_ctx` 就分叉 ⇒ **前缀缓存无法共享**（整篇一次请求 ≈17k token 前缀每次全价）。
    - **不依赖 text_zh**：全文块只读 text_en；未翻译也能编译。
    - 翻译输入/回填**按 para_id**（与共享前缀一致），reassemble 用全局公式表逐字节回填。

    （O批-8反馈：用户要"AI 知道全文后再翻译"，且要避免多请求并发触发限流（智谱不支持并发）。
    整篇一次 = 单请求、串行、无并发；配合 timeout=600s 覆盖长译文生成，超时/异常回退分批不整篇失败。
    分批为兜底，每批大（MAX_BATCH_PARAS/MAX_BODY_CHARS 已放大）、串行，批间共享相同前缀命中缓存。）

    llm: paperkb.llm.LLMClient 实现。
    返回: {"translated", "rejected", "targets", "calls", "summary", "truncated"}
    （six-dim 六维总结不在翻译阶段生成，D16：交由 L1 编译 _note.md。）
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

    # 待译目标 = **与共享上下文同一判据**（批4 修正）：只有"模型看得到原文"的段落才要求翻译。
    # 旧实现用自建的 `_reference_cut` 只切 section 名，与 `context_paragraphs` 口径不一致 ⇒
    # 被尾部截断/误标 section 的段落（参考文献条目、致谢、SI…）进了待译清单却无原文可译，
    # 模型只能回「（原文未提供该段内容，无法翻译。）」并写进 text_zh（实测 66 处污染）。
    allowed_ids = {p.para_id for p in context_paragraphs(doc) if getattr(p, "para_id", "")}
    targets = [i for i, pp in enumerate(paras)
               if (pp.get("para_id") or "") in allowed_ids
               and not pp.get("is_heading") and (pp.get("text_en") or "").strip()]
    total_chars = sum(len(paras[i].get("text_en") or "") for i in targets)

    calls_box = [0]
    translated = rejected = truncated = 0

    if total_chars > 0 and total_chars <= MAX_WHOLE_CHARS:
        # 整篇一次优先：全文入同一 prompt（看全文再译），单请求串行，避免并发限流。
        whole_map, whole_rej = _try_whole(llm, shared, paras, targets, para_id_to_idx,
                                          math_list, context, calls_box)
        if whole_map is not None:
            rejected += whole_rej
            for idx, zh in whole_map.items():
                paras[idx]["text_zh"] = zh
                translated += 1
        else:
            # 整篇失败（超时/解析/超限）→ 回退分批（串行兜底，仍能翻译，只是不整篇）。
            (translated, rejected), truncated = _run_batches(
                llm, shared, paras, targets, para_id_to_idx, math_list, context, calls_box)
    else:
        # 超大文档 / 无 target → 直接分批。
        (translated, rejected), truncated = _run_batches(
            llm, shared, paras, targets, para_id_to_idx, math_list, context, calls_box)

    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"translated": translated, "rejected": rejected, "targets": len(targets),
            "calls": calls_box[0], "summary": {}, "truncated": truncated}
