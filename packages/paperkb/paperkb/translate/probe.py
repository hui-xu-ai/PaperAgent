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
6. **只给建议**：本模块只返回数字与理由，**不写任何设置**——是否采纳由用户在界面上决定；
7. **墙钟上限 = 生产客户端的读超时（90s）**（2026-09-23 用户报"卡在第 5 档"与"多次重试超时"后
   两次收紧）：生产请求是**非流式**的，`requests` 的 read timeout 就等于"整批必须在 N 秒内返回"。
   探测必须用**同一个数**，否则会把"生产必然超时的批次"推荐给用户（实测代价：13852 字符的批
   3 次超时 = 270s 白等 + 3 次输入白烧，然后才回落主模型；同一模型拿 3777 字符的小批立即成功
   ⇒ 是时间不够，不是能力不够）。客户端自己的读超时也被识别为"该档超时"（同一结论、同一文案）；
   整个探测另有总预算（`TOTAL_BUDGET_SEC`），用尽则余下档位标"未测"。
8. **效率评价**（2026-09-23 用户："增加一个翻译效率评价，监测到模型翻译效率很低很慢后，则认为
   这就是极限"）：每档记 `sec` / `sec_per_1k`（按输入字符归一）。**译完了但每千字符慢于
   `SLOW_SEC_PER_1K` ⇒ 同样视为极限**（阶梯停在该档、不计入最高通过档），因为真实翻译的等待
   时间不可接受；并把节奏换算成"一篇论文约需多少分钟"随结果一起给用户。

## 与真实翻译路径的关系

探测请求用**紧凑模式的提示词形状**（段落内联、无共享全文前缀，见 `_translate_task_compact`）：
要测的是"这个模型一次能产出多少译文"，与输入前缀无关；不带 40 万字符的全文前缀既省 token 又
省时间，输出侧的结论对主模型路径同样成立（两条路径的批次上限是同一个旋钮、同一套分批算法）。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from ..context import context_paragraphs
from ..doc import read_document
from ..layout import doc_path
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

#: ── 单档**墙钟上限** = 生产客户端的**读超时**（2026-09-23 用户实测"多次重试超时"后定稿）────
#: 为什么必须与生产**同口径**：生产请求是非流式的，`requests` 的 read timeout 就等于"整个响应
#: 体必须在 N 秒内到达"。旧版探测用 300s 额度（`PROBE_TIMEOUT_SEC`），只证明"输出能回全"，却把
#: "生产 90s 内根本回不来的批次"判为**通过** ⇒ 推荐值直接踩在生产超时上：实测用户那篇 14500 字符
#: 档，13852 字符的批连续 3 次 `Read timed out (read timeout=90)`（270s 白等 + 3 次输入白烧）才
#: 回落主模型；同一个模型拿 3777 字符的小批立刻成功 ⇒ 是**时间不够**，不是模型不会译。
#: 90 = `backend/app/services/llm_service.TRANSLATE_TIMEOUT_SEC`（专用翻译模型客户端），
#: 两者由 `backend/tests/test_translate_timeout_contract.py` 守卫同步。
TIER_TIMEOUT_SEC = 90.0

#: 客户端自身超时之上再留的余量：正常应由**客户端读超时**先报错（我们把那类异常判为超时档，
#: 理由更明确），墙钟只兜底"持续吐 token、永不结束"（那种情况 read timeout 不触发）。
TIER_WALL_GRACE_SEC = 20.0

#: 整个探测的总预算：超了就用**已测到的档位**给建议，余下档位如实标记"未测"（不无限等）。
#: 5 档 × 90s + 余量：正常 2-3 档就结束，慢模型也不会把界面挂住。
TOTAL_BUDGET_SEC = 500.0

#: ── 效率评价（2026-09-23 用户："增加一个翻译效率评价，监测到模型翻译效率很低很慢后，则认为
#: 这就是极限"）────────────────────────────────────────────────────────────────
#: 判据：**每千正文字符的墙钟耗时**（`sec_per_1k`）。按输入归一的理由——批次上限这个旋钮就是
#: "一批装多少源文字符"，用户真正要知道的是"这个规模的批要等多久"，与译文长度无关地可比。
#: 阈值校准（实测 glm-4.5-air 各档 s/千字符）：3000→13.3、6000→12.7、12000→5.9、24000→4.7
#: —— 小档偏慢是固定开销（思考链前言）摊薄得少，**可用档的节奏在 5-13 s/千字符**。取 25 ≈ 2×
#: 实测最慢的可用节奏：只抓"塌方式变慢"（推理链随批次变大而失控、逐字往外爬），不误伤小档。
SLOW_SEC_PER_1K = 25.0

#: 效率评价里估算"整篇要等多久"用的参考正文长度（字符）——4 万 ≈ 一篇普通研究论文的正文量。
PAPER_REF_CHARS = 40000

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
    for base, is_kb in ((getattr(roots, "kb_dir", None), True),
                        (getattr(roots, "library_dir", None), False)):
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
            f = doc_path(d, kb=is_kb)
            try:
                cands.append((f.stat().st_size, d))
            except OSError:
                continue
        for _size, d in sorted(cands, key=lambda x: -x[0]):
            if total >= need_chars or len(sources) >= max_docs:
                break
            f = doc_path(d, kb=is_kb)
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


def _tier_budget(chars: int = 0) -> float:
    """该档的墙钟上限（秒）——**与生产客户端读超时同值**，不随档位大小变。

    为什么不按字符数放大（旧实现 `max(90, chars/1000×6)`）：生产是**固定 90s** 的超时，探测若给
    大档更多额度，就会推荐出生产必然超时的批次。`chars` 参数仅为兼容旧调用点保留。
    """
    return TIER_TIMEOUT_SEC


def _looks_like_timeout(err: BaseException) -> bool:
    """异常是否为"读超时"类（requests.Timeout / httpx.ReadTimeout / 文本含 timeout）。

    paperkb 不绑定具体 HTTP 库，故按类名与消息判定；判中即按"该档超时 = 极限"处理，而不是
    笼统的"调用报错、稍后重试"——两者给用户的结论完全不同。
    """
    if "timeout" in type(err).__name__.lower():
        return True
    msg = str(err).lower()
    return "timed out" in msg or "timeout" in msg or "超时" in msg


def _call_with_deadline(fn: Callable[[], object], timeout: float):
    """在**守护线程**里执行 `fn`；超时返回 `(None, True)`，否则 `(返回值, False)`。

    为什么不用 `ThreadPoolExecutor`：它的工作线程**非守护**，被放弃的挂起调用会在进程退出时
    被 join 住（`concurrent.futures` 注册了 atexit 钩子）⇒ 关停/测试时卡死。守护线程不会。
    被放弃的那次调用仍在后台跑完（SDK 无法取消），但**探测不再等它**——这是"交互式操作必须
    有结论"与"后台浪费一次调用"之间的取舍，已在返回文本里向用户说明。
    """
    box: dict = {}

    def _run() -> None:
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001 - 异常带回主线程判定，别吞
            box["error"] = e

    t = threading.Thread(target=_run, name="probe-tier", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return None, True
    if "error" in box:
        raise box["error"]
    return box.get("value"), False


def _efficiency_report(tested: list[dict], slow_sec_per_1k: float) -> dict:
    """把各档的节奏汇总成一句人话（用户 2026-09-23："增加一个翻译效率评价"）。

    取**通过档里最大的一档**的节奏做代表：小档有固定开销（思考链前言、连接握手），按每千字符
    摊下来偏慢，用它估整篇会高估；最大通过档最接近真实批次的形状。
    """
    ok_rows = [r for r in tested if r.get("ok")]
    rep: dict = {"sec_per_1k": None, "tier": None, "sec": None, "out_cps": None,
                 "paper_minutes": None, "slow_sec_per_1k": slow_sec_per_1k, "note": ""}
    if ok_rows:
        pace = ok_rows[-1]
        rep.update({"sec_per_1k": pace["sec_per_1k"], "tier": pace["tier"],
                    "sec": pace["sec"], "out_cps": pace["out_cps"]})
        rep["paper_minutes"] = round(PAPER_REF_CHARS / 1000.0 * pace["sec_per_1k"] / 60.0, 1)
        rep["note"] = ("翻译效率：通过档里最大一档（%d 字符）用 %ss ⇒ 每千正文字符 %s 秒，"
                       "一篇约 %d 千字符的论文单跑约需 %s 分钟（按此节奏累计，不含重试）。"
                       "判据：单批必须在 %gs 内译完（= 生产客户端的读超时），译不完即判极限；"
                       "另外每千字符慢于 %.0f 秒也判效率过低。"
                       % (pace["tier"], pace["sec"], pace["sec_per_1k"],
                          PAPER_REF_CHARS // 1000, rep["paper_minutes"],
                          TIER_TIMEOUT_SEC, slow_sec_per_1k))
    else:
        rep["note"] = ("翻译效率：本次没有通过的档位，给不出节奏。判据：单批必须在 %gs 内译完"
                       "（= 生产客户端的读超时）；另外每千字符慢于 %.0f 秒也判效率过低。"
                       % (TIER_TIMEOUT_SEC, slow_sec_per_1k))
    return rep


def _probe_tier(llm, group: list[str], tier: int, *, timeout: float | None = None,
                slow_sec_per_1k: float | None = None) -> dict:
    """单档探测：把 group 作为**一个批次**发过去，双判据判定是否被截断。

    `timeout`：本档墙钟上限（秒）；None → `TIER_TIMEOUT_SEC`（= 生产客户端读超时）。超时按
    该档**失败**处理（理由设为"单档超时"），上层"首败即停"随即给出结论。
    `slow_sec_per_1k`：效率阈值（每千正文字符秒数）；译完但慢于此值 ⇒ 该档标 `slow`，
    上层同样视为"到头了"（用户 2026-09-23：效率过低就是极限）。
    """
    batch = [{"para_id": "P%03d" % (i + 1), "text_en": t} for i, t in enumerate(group)]
    ids = [p["para_id"] for p in batch]
    actual = sum(len(t) for t in group)
    row: dict = {"tier": tier, "chars": actual, "paras": len(batch), "ok": False,
                 "finish_reason": None, "got": 0, "missed": len(batch),
                 "out_chars": 0, "reason": "",
                 # 效率（2026-09-23 新增）：秒 / 每千正文字符 / 输出吞吐
                 "sec": 0.0, "sec_per_1k": 0.0, "out_cps": 0.0}
    prompt = _translate_task_compact(batch, list(range(len(batch))))
    # 取本次调用的 finish_reason：DeepSeekAI 会把它挂到实例上（getattr 兜底 = 该实现不提供，
    # 则退化为只看"应译段是否回全"——判据变弱但不会误判为失败）。
    if hasattr(llm, "last_finish_reason"):
        try:
            llm.last_finish_reason = None
        except Exception:  # noqa: BLE001 - 只读属性/只读实现：忽略
            pass
    budget = float(timeout) if timeout else _tier_budget(actual)
    slow = SLOW_SEC_PER_1K if slow_sec_per_1k is None else float(slow_sec_per_1k)

    def per_1k(sec: float) -> float:
        """墙钟秒 → 每千正文字符秒（效率判据的归一化口径）。"""
        return round(sec / (actual / 1000.0), 1) if actual else 0.0

    def _timeout_row(why: str) -> dict:
        """本档判超时（= 实用极限）——墙钟截断与客户端读超时**同一结论、同一文案**。"""
        same_as_prod = abs(budget - TIER_TIMEOUT_SEC) < 0.001
        row["error"] = True
        row["timed_out"] = True
        row["reason"] = ("单档超时：%s（本档 %d 字符；单档上限 %gs%s）——故视为该模型在这个批次"
                         "规模上的实用极限"
                         % (why, actual, budget,
                            "，= 生产客户端的读超时，真实翻译会照样报 read timeout 并把重试"
                            "次数耗光" if same_as_prod else ""))
        logger.warning("翻译上限探测：档位 %d 超时（%s）", tier, why)
        return row

    t0 = time.monotonic()
    try:
        raw, timed_out = _call_with_deadline(
            lambda: llm.complete(prompt, context="translate") or "", budget)
    except Exception as e:  # noqa: BLE001 - 调用失败按该档失败处理（保守：不给出更高推荐）
        row["sec"] = round(time.monotonic() - t0, 1)
        row["sec_per_1k"] = per_1k(row["sec"])
        if _looks_like_timeout(e):
            # **客户端自己的读超时先到**（生产也是这条路）：与"墙钟截断"合并同一结论，
            # 否则会被笼统当成"调用报错、稍后重试"——那恰好是用户被误导的那条提示。
            return _timeout_row("模型在 %gs 内没回完：%s"
                               % (budget, str(e).replace("\n", " ")[:120]))
        row["reason"] = "调用失败：%s" % str(e).replace("\n", " ")[:200]
        row["error"] = True
        return row
    row["sec"] = round(time.monotonic() - t0, 1)
    row["sec_per_1k"] = per_1k(row["sec"])
    if timed_out:
        return _timeout_row("%gs 内未译完且仍在吐字" % row["sec"])
    finish = getattr(llm, "last_finish_reason", None)
    row["finish_reason"] = finish
    row["out_chars"] = len(raw)
    row["out_cps"] = round(len(raw) / row["sec"], 1) if row["sec"] > 0 else 0.0
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
    if row["ok"] and row["sec_per_1k"] > slow:
        # 译完了但节奏塌了：不判"截断"，判"不实用"——一批要等多久，整篇等待时间不可接受
        row["slow"] = True
        row["reason"] = ("效率过低：译完了，但节奏 %s s/千字符（阈值 %.0f）——本档 %d 字符就要等"
                         " %gs，真实翻译的等待时间不可接受，故视为该模型的实用极限"
                         % (row["sec_per_1k"], slow, actual, row["sec"]))
    return row


def probe_safe_batch_chars(llm, paras: list[str], *, ladder: tuple[int, ...] = LADDER,
                           safety_factor: float = SAFETY_FACTOR,
                           max_tokens: int | None = None,
                           model: str = "", provider_name: str = "",
                           sources: list[dict] | None = None,
                           progress_cb: ProgressCb | None = None,
                           tier_timeout: float | None = None,
                           total_budget: float | None = None,
                           slow_sec_per_1k: float | None = None) -> dict:
    """阶梯探测出该模型的安全批次上限（返回结果，**不写任何设置**）。

    tier_timeout / total_budget：单档墙钟上限与总预算（秒）；None → 用模块默认
    （`_tier_budget(档位字符数)` / `TOTAL_BUDGET_SEC`）。**测试可传小值**，不必等真超时。
    slow_sec_per_1k：效率阈值（每千正文字符秒数）；None → `SLOW_SEC_PER_1K`。译完但慢于此值
    的档位判为"效率过低 = 实用极限"（不再往更高档试），推荐值仍取**更快的那一档**。

    llm: 满足 `paperkb.llm.LLMClient` 的客户端（`complete(prompt, context)`）；**必须是探测专用
    实例**——探测会读写 `llm.last_finish_reason`，与线上翻译共用实例会产生竞态。
    paras: 真实英文正文段（`collect_english_text` 产出），整段使用。
    progress_cb: `(current, total, phase)`，供前端显示进度。

    返回 {"tested": [档位行], "highest_pass", "recommended", "safety_factor", "ok",
          "stop_reason", "hint", "efficiency", "corpus_note", "max_tokens", "model",
          "provider_name", "probed_at", "text_source": {...}}。
    """
    total = len(ladder)
    tested: list[dict] = []
    stop_reason = "passed_all"
    highest = 0
    slow = SLOW_SEC_PER_1K if slow_sec_per_1k is None else float(slow_sec_per_1k)
    budget_total = TOTAL_BUDGET_SEC if total_budget is None else float(total_budget)
    paras = _pick_corpus_paras(paras, max(ladder))
    avail = sum(len(t) for t in paras)
    t_start = time.monotonic()
    for i, tier in enumerate(ladder, 1):
        if time.monotonic() - t_start > budget_total:
            # 总预算用尽：余下档位**逐档**标"未测"（界面要看到整条阶梯的处置），
            # 用已测到的结果给建议（不让界面无限等）
            for rest in ladder[i - 1:]:
                tested.append({"tier": rest, "chars": 0, "paras": 0, "ok": False,
                               "finish_reason": None, "got": 0, "missed": 0, "out_chars": 0,
                               "sec": 0.0, "sec_per_1k": 0.0, "out_cps": 0.0,
                               "reason": "总时长预算用尽（%gs），该档未测"
                                         % (time.monotonic() - t_start),
                               "skipped": True})
            stop_reason = "time_budget"
            logger.warning("翻译上限探测：总预算用尽（%gs），停止于第 %d 档",
                           time.monotonic() - t_start, i)
            break
        if avail < tier:
            # 库内正文拼不满这一档 ⇒ 无法测，阶梯到此为止（如实报告已测到哪）
            tested.append({"tier": tier, "chars": 0, "paras": 0, "ok": False,
                           "finish_reason": None, "got": 0, "missed": 0, "out_chars": 0,
                           "sec": 0.0, "sec_per_1k": 0.0, "out_cps": 0.0,
                           "reason": "库内可译英文正文不足（现有 %d 字符）" % avail,
                           "skipped": True})
            stop_reason = "insufficient_text"
            break
        group = _take(paras, tier)
        actual = sum(len(t) for t in group)
        budget = float(tier_timeout) if tier_timeout else _tier_budget(actual)
        if progress_cb:
            try:
                progress_cb(i, total, "正在测 %d 字符档（%d 段，单档上限 %gs）…"
                            % (tier, len(group), budget))
            except Exception:  # noqa: BLE001 - 进度回调异常绝不影响探测
                logger.debug("探测进度回调异常（忽略）", exc_info=True)
        logger.info("翻译上限探测：档位 %d 字符（实际 %d 字符 / %d 段，单档上限 %gs）",
                    tier, actual, len(group), budget)
        row = _probe_tier(llm, group, tier, timeout=budget, slow_sec_per_1k=slow)
        tested.append(row)
        if row["ok"] and not row.get("slow"):
            highest = max(highest, row["chars"])
            continue
        if row.get("timed_out"):
            stop_reason = "timed_out"
        elif row.get("slow"):
            stop_reason = "inefficient"
        else:
            stop_reason = "error" if row.get("error") else "failed"
        logger.warning("翻译上限探测：档位 %d 失败（%s）—— %s", tier, stop_reason, row["reason"])
        break

    recommended = _floor100(highest * safety_factor) if highest else 0
    efficiency = _efficiency_report(tested, slow)
    hint = ""
    if not recommended:
        if stop_reason == "insufficient_text":
            hint = ("库里可译的英文正文不足（现有 %d 字符），测不出安全值："
                    "请先解析/导入至少一篇含正文的文献再测。" % avail)
        elif stop_reason == "timed_out":
            hint = ("最低档 %d 字符在 %gs 内都没译完——这正是生产客户端的读超时，所以这个模型在这个"
                    "规模上连一次批次都产不出来（真实翻译会 90s 超时 ×重试次数 再回落主模型）。"
                    "不是「批次上限」的问题：优先换输出更快的翻译模型（思考型模型会把推理链算进"
                    "输出，慢一个量级）。" % (ladder[0], TIER_TIMEOUT_SEC))
        elif stop_reason == "inefficient":
            hint = ("最低档 %d 字符虽然译完了，但效率过低（%s s/千字符 > 阈值 %.0f）⇒ 这个批次规模"
                    "上模型已不实用（等待时间不可接受），故判为极限、不给推荐值：请换输出更快的"
                    "翻译模型。" % (ladder[0], tested[0]["sec_per_1k"], slow))
        elif stop_reason == "time_budget":
            hint = ("探测总时长预算（%gs）在首档就用尽，一档都没测出：本机与模型的组合极慢，"
                    "请稍后重试；若持续如此，换输出更快的翻译模型。" % budget_total)
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
    elif stop_reason == "time_budget":
        hint = ("推荐值 = 最高通过档 %d × 安全系数 %g。本次探测总时长到了预算上限（%gs），"
                "更高档位没测——想确认更高档请稍后再测一次（或直接手动填更大值，风险自担）。"
                % (highest, safety_factor, budget_total))
    elif stop_reason in ("inefficient", "timed_out"):
        hint = ("推荐值 = 最高通过档 %d × 安全系数 %g（不是测试极限）——这也正是**能在这个模型的"
                "读超时（%gs）内译完**的规模：更高档%s。想用更大批次就必须提高该模型的请求超时"
                "（代码常量 TRANSLATE_TIMEOUT_SEC），否则每批都会 90s 超时、耗掉重试次数再回落"
                "主模型。"
                % (highest, safety_factor, TIER_TIMEOUT_SEC,
                   "译完了但效率过低（见下方效率评价）" if stop_reason == "inefficient"
                   else "在超时前回不来"))
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
        "efficiency": efficiency,
        "corpus_note": corpus_note,
        "max_tokens": max_tokens,
        "model": model,
        "provider_name": provider_name,
        "probed_at": datetime.now().isoformat(timespec="seconds"),
        "text_source": {"docs": len(sources or []), "chars": sum(len(t) for t in paras),
                        "sources": sources or []},
    }
