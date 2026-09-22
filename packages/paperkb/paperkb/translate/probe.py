# -*- coding: utf-8 -*-
"""翻译批次「安全上限」探测（2026-09-22 用户要求：点一下自动测出该模型能吃的批次上限）。

## 为什么必须实测

「翻译批次上限（每批正文字符数）」本质是**输出侧容量**问题：把 N 字符英文发过去，模型要吐
N 字符量级的译文；超过它的输出天花板（服务端硬顶 / 请求 max_tokens / 长输出注意力衰减）就会
截断成"只译出前几段"。这个天花板**因模型、供应商、参数而异，官方文档不给**——实测最准：

- 2026-09-19 混元 MT-7B via 硅基流动：每批输出 ~1900 token 就断（max_tokens 参数改不动它）；
- 2026-09-21 glm-5.3-flash：请求 max_tokens=64000，服务端硬顶 ~11K token。

## 设计（用户明确要求，勿改）

1. **阶梯探测**：3000 → 6000 → 12000 → 24000 → 48000 字符，逐档**单次真译**（不是估算）；
2. **首败即停**：截断随批次单调（更大批不可能反而通过）⇒ 省时间省钱（通常只跑 2-3 次）；
3. **双判据**（两条都查，任一命中即判该档失败）：
   - `finish_reason == "length"`——供应商自报输出触顶；
   - **应译段没回全**——思考型模型常把 finish_reason 报成 stop，实际只译了一半
     （2026-09-21 glm 实测），只看 finish_reason 会漏判；
4. **安全系数 0.6**：推荐值 = 最高通过档 × 0.6，**绝不直接填测试极限**——极限值一次成功不代表
   次次成功（温度、文本密度、并发都会浮动），留 40% 余量才耐用；
5. **真实语料**：用知识库/解析库里**真实的英文正文段**（整段取用，绝不切句），不用造题；
6. **只给建议**：本模块只返回数字与理由，**不写任何设置**——是否采纳由用户在界面上决定。

## 与真实翻译路径的关系

探测请求用**紧凑模式的提示词形状**（段落内联、无共享全文前缀，见 `_translate_task_compact`）：
要测的是"这个模型一次能产出多少译文"，与输入前缀无关；不带 40 万字符的全文前缀既省 token 又
省时间，输出侧的结论对主模型路径同样成立（两条路径的批次上限是同一个旋钮、同一套分批算法）。
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Callable

from ..context import context_paragraphs
from ..doc import read_document
from .pipeline import (_parse_translation_output, _translate_task_compact,
                       is_refusal_translation)

logger = logging.getLogger(__name__)

#: 阶梯档位（正文字符数）。首档 3000 低于紧凑默认（14000）⇒ 首档就失败极具诊断价值
#: （说明瓶颈不是批次上限，而是请求侧 max_tokens 预算太小或模型/服务异常）。
LADDER: tuple[int, ...] = (3000, 6000, 12000, 24000, 48000)

#: 安全系数：推荐值 = 最高通过档 × 此系数（0.6 = 留 40% 余量，用户要求"不按测试极限填"）。
SAFETY_FACTOR = 0.6

#: 语料收集上限：最多打开多少个真实文档（按 document.json 体积从大到小取，少开文件多拿正文）。
MAX_SOURCE_DOCS = 30

#: 单段过短（页码、孤立符号等）不作为探测语料——它们会把 JSON 结构开销占比推高，失真。
MIN_PARA_CHARS = 40

#: 优选语料段长：优先用 ≥ 此长度的"实段"，让一批 N 字符的**段数**接近真实分批的形状
#: （真实 compact 批次同时受"12 段/批"限制；用碎段凑 4.8 万字符要 100+ 段，比真实形状苛刻
#: 得多，JSON 结构开销也被放大）。取 400 ≈ 实测语料中位段长，长段不够时自动退回全部段。
PREFER_PARA_CHARS = 400

ProgressCb = Callable[[int, int, str], None]


def collect_english_text(roots, need_chars: int, *, max_docs: int = MAX_SOURCE_DOCS,
                         min_para_chars: int = MIN_PARA_CHARS) -> dict:
    """从真实库里凑够 `need_chars` 字符的英文正文段（整段，按 kb → library 顺序）。

    取段口径与真实翻译**同源**：`read_document` + `context_paragraphs`（= 尾部杂项截断 +
    跳 References 类章节），所以拿到的一定是"能被翻译的段落"，不含参考文献。

    返回 {"paras": [str], "chars": int, "docs": int, "sources": [{dir, paras}]}。
    chars < need_chars ⇒ 库内可用正文不足（调用方据此缩短阶梯并如实报告）。
    """
    paras: list[str] = []
    sources: list[dict] = []
    total = 0
    for base in (getattr(roots, "kb_dir", None), getattr(roots, "library_dir", None)):
        if total >= need_chars or len(sources) >= max_docs:
            break
        if not base:
            continue
        base_p = Path(base)
        if not base_p.exists():
            continue
        # 大文件优先：一次探测只需 4.8 万字符，通常 1-3 篇就够，少开文件少读盘
        cands: list[tuple[int, Path]] = []
        for d in base_p.iterdir():
            if not d.is_dir() or d.name.startswith((".", "_")):
                continue          # 跳过 _trash/.cache 等非论文目录
            f = d / "document.json"
            try:
                cands.append((f.stat().st_size, d))
            except OSError:
                continue
        for _size, d in sorted(cands, key=lambda x: -x[0]):
            if total >= need_chars or len(sources) >= max_docs:
                break
            f = d / "document.json"
            try:
                doc = read_document(f)
                texts = [(p.text_en or "").strip() for p in context_paragraphs(doc)
                         if not p.is_heading and len((p.text_en or "").strip()) >= min_para_chars]
            except Exception as e:  # noqa: BLE001 - 单个文档坏不影响整体取证
                logger.warning("探测语料：读取失败已跳过 %s: %s", f, e)
                continue
            if not texts:
                continue
            sources.append({"dir": d.name, "paras": len(texts)})
            for t in texts:
                if total >= need_chars:
                    break
                paras.append(t)
                total += len(t)
    return {"paras": paras, "chars": total, "docs": len(sources), "sources": sources}


def _pick_corpus_paras(paras: list[str], need: int,
                       prefer: int = PREFER_PARA_CHARS) -> list[str]:
    """挑出用于探测的语料段：**优先长段**（按长度取够 need），再按原文顺序排回。

    为什么要挑：真实 compact 分批同时受"≤max_body 字符"和"≤12 段"两条约束。若拿碎段（中位
    几百字符）去凑 4.8 万字符，一批要 80+ 段，既不是真实形状、JSON 结构开销也会被放大。用长段
    凑出同样的字符量，段数接近真实批次 ⇒ 测出的"能扛多少字符"才对得上实际。
    长段不够（整库都是碎段）⇒ 全部段一起上，此时结论偏保守（可以接受）。
    """
    if not paras:
        return []
    picked_idx = [i for i, t in enumerate(paras) if len(t) >= prefer]
    if sum(len(paras[i]) for i in picked_idx) < need:
        picked_idx = list(range(len(paras)))
    picked_idx.sort(key=lambda i: -len(paras[i]))
    chosen: list[int] = []
    total = 0
    # 多留 15% 余量：顶档要求"装到 ≥ limit"，末段可能把总数推高，留点冗余更稳
    for i in picked_idx:
        if total >= need * 1.15:
            break
        chosen.append(i)
        total += len(paras[i])
    chosen.sort()          # 排回原文顺序，保持上下文连贯
    return [paras[i] for i in chosen]


def _take(paras: list[str], limit: int) -> list[str]:
    """按原文顺序装段（**只在段边界切**，绝不切段中），装到累计 ≥ limit 即停。

    不"截断"段落、也不为了凑数把装不下的段丢掉：装到够 limit 就停，所以一批的实际字符数落在
    [limit, limit + 最长段) —— 略超一点是**保守**方向（真实分批是"再加一段就超 limit"，也允许
    单段自身超 limit 时独占一批）。首段自身超 limit ⇒ 只装它一个，与真实路径一致。
    """
    out: list[str] = []
    acc = 0
    for t in paras:
        if out and acc >= limit:
            break
        out.append(t)
        acc += len(t)
    return out


def _floor100(n: float) -> int:
    """向下取整到百位（7200 这种数比 7183 好读、好填）。"""
    return int(n // 100) * 100


def _probe_tier(llm, group: list[str], tier: int) -> dict:
    """单档探测：把 group 作为**一个批次**发过去，双判据判定是否被截断。"""
    batch = [{"para_id": "P%03d" % (i + 1), "text_en": t} for i, t in enumerate(group)]
    ids = [p["para_id"] for p in batch]
    actual = sum(len(t) for t in group)
    row: dict = {"tier": tier, "chars": actual, "paras": len(batch), "ok": False,
                 "finish_reason": None, "got": 0, "missed": len(batch),
                 "out_chars": 0, "reason": ""}
    prompt = _translate_task_compact(batch, list(range(len(batch))))
    # 取本次调用的 finish_reason：DeepSeekAI 会把它挂到实例上（getattr 兜底 = 该实现不提供，
    # 则退化为只看"应译段是否回全"——判据变弱但不会误判为失败）。
    if hasattr(llm, "last_finish_reason"):
        try:
            llm.last_finish_reason = None
        except Exception:  # noqa: BLE001 - 只读属性/只读实现：忽略
            pass
    try:
        raw = llm.complete(prompt, context="translate") or ""
    except Exception as e:  # noqa: BLE001 - 调用失败按该档失败处理（保守：不给出更高推荐）
        row["reason"] = "调用失败：%s" % str(e).replace("\n", " ")[:200]
        row["error"] = True
        return row
    finish = getattr(llm, "last_finish_reason", None)
    row["finish_reason"] = finish
    row["out_chars"] = len(raw)
    data = _parse_translation_output(raw, ids)
    got = {str(it.get("para_id")) for it in (data.get("translations") or [])
           if it.get("zh") and not is_refusal_translation(it.get("zh"))}
    missed = [i for i in ids if i not in got]
    row["got"] = len(got)
    row["missed"] = len(missed)
    truncated = (finish == "length")
    row["ok"] = (not truncated) and (not missed)
    if truncated and missed:
        row["reason"] = ("输出触顶（finish_reason=length）：%d/%d 段未译出"
                         % (len(missed), len(batch)))
    elif truncated:
        row["reason"] = "供应商自报输出触顶（finish_reason=length）"
    elif missed:
        row["reason"] = ("应译 %d 段只回了 %d 段（输出被截断，供应商未自报）"
                         % (len(batch), len(got)))
    return row


def probe_safe_batch_chars(llm, paras: list[str], *, ladder: tuple[int, ...] = LADDER,
                           safety_factor: float = SAFETY_FACTOR,
                           max_tokens: int | None = None,
                           model: str = "", provider_name: str = "",
                           sources: list[dict] | None = None,
                           progress_cb: ProgressCb | None = None) -> dict:
    """阶梯探测出该模型的安全批次上限（返回结果，**不写任何设置**）。

    llm: 满足 `paperkb.llm.LLMClient` 的客户端（`complete(prompt, context)`）；**必须是探测专用
    实例**——探测会读写 `llm.last_finish_reason`，与线上翻译共用实例会产生竞态。
    paras: 真实英文正文段（`collect_english_text` 产出），整段使用。
    progress_cb: `(current, total, phase)`，供前端显示进度。

    返回 {"tested": [档位行], "highest_pass", "recommended", "safety_factor", "ok",
          "stop_reason", "hint", "corpus_note", "max_tokens", "model", "provider_name",
          "probed_at", "text_source": {...}}。
    """
    total = len(ladder)
    tested: list[dict] = []
    stop_reason = "passed_all"
    highest = 0
    paras = _pick_corpus_paras(paras, max(ladder))
    avail = sum(len(t) for t in paras)
    for i, tier in enumerate(ladder, 1):
        if avail < tier:
            # 库内正文拼不满这一档 ⇒ 无法测，阶梯到此为止（如实报告已测到哪）
            tested.append({"tier": tier, "chars": 0, "paras": 0, "ok": False,
                           "finish_reason": None, "got": 0, "missed": 0, "out_chars": 0,
                           "reason": "库内可译英文正文不足（现有 %d 字符）" % avail,
                           "skipped": True})
            stop_reason = "insufficient_text"
            break
        group = _take(paras, tier)
        if progress_cb:
            try:
                progress_cb(i, total, "正在测 %d 字符档（%d 段）…" % (tier, len(group)))
            except Exception:  # noqa: BLE001 - 进度回调异常绝不影响探测
                logger.debug("探测进度回调异常（忽略）", exc_info=True)
        logger.info("翻译上限探测：档位 %d 字符（实际 %d 字符 / %d 段）",
                    tier, sum(len(t) for t in group), len(group))
        row = _probe_tier(llm, group, tier)
        tested.append(row)
        if row["ok"]:
            highest = max(highest, row["chars"])
            continue
        stop_reason = "error" if row.get("error") else "failed"
        logger.warning("翻译上限探测：档位 %d 失败 —— %s", tier, row["reason"])
        break

    recommended = _floor100(highest * safety_factor) if highest else 0
    hint = ""
    if not recommended:
        if stop_reason == "insufficient_text":
            hint = ("库里可译的英文正文不足（现有 %d 字符），测不出安全值："
                    "请先解析/导入至少一篇含正文的文献再测。" % avail)
        elif stop_reason == "error":
            hint = ("探测调用报错（多为超时/限流），未测出安全值；稍后重试。若反复失败，"
                    "换一个翻译专用模型，或把「翻译批次上限」调大一档"
                    "（程序会同步放大输出预算 max_tokens，当前 %s）。" % (max_tokens or "未设置"))
        else:
            hint = ("最低档 %d 字符就失败 ⇒ 瓶颈不是「批次上限」本身，而是**请求的输出预算**"
                    "max_tokens（当前 %s）或该模型的输出能力（如思考型模型把推理链也计入输出）。"
                    "程序会把预算随「翻译批次上限」自动放大（预算 = max(该模型已配值, 上限 × 0.4)），"
                    "可先把上限调大一档再重测；仍失败就换输出更强的翻译模型。"
                    % (ladder[0], max_tokens or "未设置"))
    elif stop_reason == "passed_all":
        hint = ("已测档位全部通过（最高实际 %d 字符）⇒ 该模型输出余量充足；上面是按 %g 安全系数"
                "（留 %d%% 余量）给的推荐值，%d 字符已超过单篇普通批次的实际大小，基本等于"
                "不设约束。" % (highest, safety_factor, round((1 - safety_factor) * 100), highest))
    elif stop_reason == "insufficient_text":
        hint = ("推荐值 = 最高通过档 %d × 安全系数 %g（**不是测试极限**）。受库内正文量限制，"
                "%d 字符以上的档位没能测；想测更高档请多导入几篇正文较长的文献。"
                % (highest, safety_factor, tested[-1]["tier"]))
    else:
        hint = ("推荐值 = 最高通过档 %d × 安全系数 %g（不是测试极限）。想更激进可手动填更大值，"
                "但截断风险自担；改完看日志有无「批截断」。" % (highest, safety_factor))

    # 语料粒度提示（**事实陈述，不是建议**）：真实分批除字符上限外还有"每批段数上限"
    # （紧凑 12 段 / 主模型 24 段）。库里段落越碎，单批能装到的字符越少 ⇒ 上限调得比这更高
    # 也不会让单批变大。给出这个数，用户才知道"上限到底该设在什么量级才有意义"。
    corpus_note = ""
    if paras:
        lens = sorted(len(t) for t in paras)
        median = lens[len(lens) // 2]
        cap = median * 12
        corpus_note = ("本次语料 %d 段 / 中位段长 %d 字符。真实分批还有「每批 ≤12 段」这条约束"
                       "（紧凑模式），按你这批语料的粒度，单批最多约 %d 字符 —— 上限设得比它更高"
                       "不会让单批更大（想更大批就得让段落本身更长）。"
                       % (len(paras), median, cap))

    return {
        "tested": tested,
        "highest_pass": highest,
        "recommended": recommended,
        "safety_factor": safety_factor,
        "ladder": list(ladder),
        "ok": bool(recommended),
        "stop_reason": stop_reason,
        "hint": hint,
        "corpus_note": corpus_note,
        "max_tokens": max_tokens,
        "model": model,
        "provider_name": provider_name,
        "probed_at": datetime.now().isoformat(timespec="seconds"),
        "text_source": {"docs": len(sources or []), "chars": sum(len(t) for t in paras),
                        "sources": sources or []},
    }
