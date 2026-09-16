# -*- coding: utf-8 -*-
"""PDF 自带文本层通道（**第三信号**，2026-09-16 新增，0 API 成本）。

为什么需要它（P12 退役教训 + 本轮实测）：
- 双通道（MinerU + PaddleOCR）本质是**分歧检测器**，不是**正确性检测器**：两侧同时错时
  差异为空 ⇒ 零报告（实证：`BF₄⁻` 下标丢失、`\\Nu_{2}`（氮气被识别成希腊字母 Nu）、
  U+FFFD 乱码）。这类"共识错误"再多 AI 仲裁也看不到——它没有 conflict 项可送。
- 而**电子版 PDF 自带文本层**是第三条独立证据：本机、免费、字符级。实测（adma 篇，15 页）：
  文本层归一化 55,422 字符 vs `en.md` 43,294（比 1.28），全文 3-gram Dice 0.809，
  逐页 978–6285 字符（无空页）。扫描件该层接近 0 ⇒ **必须按页门控**。
- 用法限定为三类（都不改文本、只把"看不到的问题"变可见）：
  1. `vote()`           平票裁决：规则/AI 判不了时投第三票（供复核展示，不自动落地）；
  2. `consensus_suspect()` 共识错误检测：两通道一致但与文本层显著不符；
  3. `lost_content()`   丢内容检测：段落字符数相对文本层异常偏少。
注意：文本层自身有连字/断行/公式乱序问题 ⇒ **只作提示与平票，不作权威**；
比对必须走 `normalize()`（MinerU 的文本是 LaTeX 包裹的，字面比对必然全不符）。
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

__all__ = ["MIN_PAGE_CHARS", "normalize", "normalize_spaced", "page_texts",
           "usable_pages", "dice", "similar", "vote", "vote_conflict",
           "consensus_suspect", "lost_content", "salient_tokens",
           "unverified_tokens"]

# 页门控：归一化字符数低于此值的页视为"无可用文本层"（扫描件/纯图页）
MIN_PAGE_CHARS = 200

_LIG = str.maketrans({"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl",
                      "\ufb03": "ffi", "\ufb04": "ffl"})
_MATH_SPAN = re.compile(r"\$\$[\s\S]+?\$\$|\$[^$\n]*?\$")
_LATEX_CMD = re.compile(r"\\[a-zA-Z]+\s*")
_KEEP = re.compile(r"[^0-9a-z]+")


def normalize(text: str) -> str:
    """比对用归一化：去 LaTeX 包裹与命令、展开连字、去软连字符、只留 `[0-9a-z]`。

    只留字母数字是**有意的**：第三信号判"字符内容"（谁的字对），格式（`$…$`、`\\mathrm`、
    上下标排版）交给 MinerU 的 LaTeX 负责；这样 MinerU 的 `$\\mathrm{Co}$` 与文本层的
    `Co` 才能对上。
    """
    t = (text or "").translate(_LIG)
    t = t.replace("\u00ad", "").replace("\ufffd", "")      # 软连字符 / 乱码替身
    t = _MATH_SPAN.sub(lambda m: m.group(0).strip("$"), t)  # 去 $ 定界，保留内容
    t = _LATEX_CMD.sub("", t)
    t = t.replace("{", "").replace("}", "").replace("\\", "")
    t = unicodedata.normalize("NFKD", t)
    return _KEEP.sub("", t.lower())


def normalize_spaced(text: str) -> str:
    """**保留单词边界**的归一化（折叠空白为单个空格，其余同上）。

    为什么需要第二套：实测冲突的绝大多数是"空格/断词"类（adma 95 个动作里 80 个是插空格
    或插一个字母）。`normalize()` 会把空格抹掉 ⇒ 两侧字符串完全相同 ⇒ 第三票永远判
    "both"（不可区分）。判这类差异必须保留空格：`ofconductivity` vs `of conductivity`
    在 PDF 文本层里只有一个能**逐字**命中。
    """
    t = (text or "").translate(_LIG)
    t = t.replace("\u00ad", "").replace("\ufffd", "")
    t = _MATH_SPAN.sub(lambda m: m.group(0).strip("$"), t)
    t = _LATEX_CMD.sub("", t)
    t = t.replace("{", "").replace("}", "").replace("\\", "")
    t = unicodedata.normalize("NFKD", t).lower()
    t = re.sub(r"[^0-9a-z]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    # LaTeX 常把数字拆成单字（`1 5 0 ^ { \circ }` 表示 150）⇒ 相邻数字之间的空格是**伪空格**，
    # 必须去掉，否则"MinerU 的 150"与"文本层的 150"在含空格模式下永远对不上（实测踩到）。
    return re.sub(r"(?<=\d)\s+(?=\d)", "", t)


def page_texts(pdf_path: str | Path, *, raw: bool = False) -> dict[int, str]:
    """页号（1-based）→ 文本层内容（失败返回空 dict，调用方按"不可用"处理）。

    `raw=False`（默认）返回**归一化**文本（比对用）；`raw=True` 返回 PDF 原文
    —— AI 综合建议需要"给人/给模型看的可读原文"，归一化后的文本会丢空格与标点。
    """
    out: dict[int, str] = {}
    try:
        import pymupdf

        doc = pymupdf.open(str(pdf_path))
        try:
            for i in range(doc.page_count):
                t = doc[i].get_text("text")
                out[i + 1] = t if raw else normalize(t)
        finally:
            doc.close()
    except Exception:  # noqa: BLE001 - 文本层取不到 = 该信号不可用，不影响解析
        return {}
    return out


def usable_pages(pages: dict[int, str], min_chars: int = MIN_PAGE_CHARS) -> set[int]:
    """可用页集合（文本层足够厚的页）。全篇都不可用 → 空集 = 关闭第三信号。"""
    return {p for p, t in (pages or {}).items() if len(t or "") >= min_chars}


def dice(a: str, b: str, n: int = 3) -> float:
    """字符 n-gram Dice 相似度（0~1）。空串返回 0。"""
    if not a or not b:
        return 0.0
    ga = {a[i:i + n] for i in range(max(0, len(a) - n + 1))}
    gb = {b[i:i + n] for i in range(max(0, len(b) - n + 1))}
    if not ga or not gb:
        return 0.0
    return 2 * len(ga & gb) / (len(ga) + len(gb))


def _hit(text: str, local: str, *, min_len: int, threshold: float) -> bool:
    """该侧内容是否被文本层支持：短片段用子串命中，长片段用 Dice。"""
    if not text or not local:
        return False
    if len(text) < min_len:
        return False
    if text in local:
        return True
    return dice(text, local) >= threshold


def _span_probes(ctx: str, core: str, off: int | None, *, spaced: bool, min_len: int = 6,
                 pads: tuple[int, ...] = (12, 8, 5, 30, 60)) -> list[str]:
    """生成**始终跨过冲突点**的窗口探针（多个宽度），归一化后返回。

    ⚠️ 不能用"左右半窗"：半窗会把冲突点排除在外（实测 `of conductivity` vs
    `ofconductivity` 的右半窗 `conductivity was` 在文本层里两边都命中 ⇒ 判成 both）。
    这里所有窗口都以冲突点为中心、宽度递减，既保留区分度又容忍窗口内的局部噪声
    （公式排版/换行连字符）。
    """
    ctx = ctx or ""
    if not ctx:
        return []
    if off is None:
        off = ctx.find(core) if core else 0
        if off < 0:
            off = 0
    off = max(0, min(off, len(ctx)))
    clen = len(core or "")
    out: list[str] = []
    for pad in pads:
        w = ctx[max(0, off - pad): min(len(ctx), off + clen + pad)]
        n = normalize_spaced(w) if spaced else normalize(w)
        if len(n) >= min_len and n not in out:
            out.append(n)
    return out


def vote(mineru_text: str, paddle_text: str, local_text: str, *,
         min_len: int = 12, threshold: float = 0.92) -> dict:
    """第三票：文本层支持哪一侧。

    **只有"可判定的那一侧"参与**：归一化长度 < `min_len` 的片段不投票（实测绝大多数冲突
    是单字符/空格，如 `Eficient→Efficient` 的 insert 'f'）⇒ 返回 `unknown`，行为与旧版
    完全一致；否则会产出大量无意义的 "neither"，把统计口径搞乱（2026-09-16 实测踩到）。

    返回 {"verdict": mineru|paddleocr|both|neither|unknown, "m_hit","p_hit",
          "m_dice","p_dice","judged","reason"}；`unknown` = 无法判定、不改任何行为。
    """
    nm, npl = normalize(mineru_text), normalize(paddle_text)
    # 两侧"去掉空白后完全相同" ⇒ 差异只在空格/断词 ⇒ 必须用保留空格的归一化才判得出
    _spaced = bool(nm) and nm == npl
    nl = normalize_spaced(local_text) if _spaced else normalize(local_text)
    if len(nl) < MIN_PAGE_CHARS:
        return {"verdict": "unknown", "m_hit": False, "p_hit": False, "m_dice": 0.0,
                "p_dice": 0.0, "judged": [], "spaced": _spaced,
                "reason": "该页无可用文本层"}
    probes_m = _span_probes(mineru_text, "", None, spaced=_spaced, min_len=min_len)
    probes_p = _span_probes(paddle_text, "", None, spaced=_spaced, min_len=min_len)
    if not probes_m and not probes_p:
        return {"verdict": "unknown", "m_hit": False, "p_hit": False, "m_dice": 0.0,
                "p_dice": 0.0, "judged": [], "spaced": _spaced,
                "reason": "两侧片段都过短(<%d 字符)，文本层无法判定" % min_len}
    m_hit = any(p in nl for p in probes_m)
    p_hit = any(p in nl for p in probes_p)
    judged = (["mineru"] if probes_m else []) + (["paddleocr"] if probes_p else [])
    if m_hit and not p_hit:
        verdict = "mineru"
    elif p_hit and not m_hit:
        verdict = "paddleocr"
    elif m_hit and p_hit:
        verdict = "both"
    elif len(judged) == 2:
        verdict = "neither"          # 两侧都够长、都不在文本层里 → 真·都不符
    else:
        verdict = "unknown"          # 只有一侧可判且不命中 → 证据不足，不下结论
    return {"verdict": verdict, "m_hit": m_hit, "p_hit": p_hit,
            "m_dice": round(dice(nm, nl), 3) if nm else 0.0,
            "p_dice": round(dice(npl, nl), 3) if npl else 0.0,
            "judged": judged, "spaced": _spaced,
            "reason": "文本层逐字命中：M%s P%s（%s）"
                      % ("✓" if m_hit else "✗", "✓" if p_hit else "✗",
                         "含空格比对" if _spaced else "去空白比对")}


def consensus_suspect(mineru_text: str, paddle_text: str, local_text: str, *,
                      agree: float = 0.9, unsupported: float = 0.55,
                      min_len: int = 20) -> dict | None:
    """共识错误检测（**段落级 Dice 版**）：两通道互相一致但与文本层显著不符。

    ⚠️ 2026-09-16 实测结论：**段落级版本在真实论文上误报严重**，已不在 P14 链路启用
    （adma/snb 两篇 flagged 的段落全是"参考文献条目/声明段"，其 M 段与 md→本地行组映射
    并非一一对应 ⇒ 比值/Dice 无意义）。保留本函数仅供离线研究与单测；
    链路上改用 **token 级** `unverified_tokens()`（见下）——只判"这个公式/数值在对应页的
    文本层里到底有没有"，不依赖段落对齐，误报面小得多。
    """
    nm, npl = normalize(mineru_text), normalize(paddle_text)
    nl = normalize(local_text)
    if min(len(nm), len(npl), len(nl)) < min_len:
        return None
    mp = dice(nm, npl)
    ml = dice(nm, nl)
    if mp < agree:              # 两通道本来就不一致 → 归 M8 仲裁管
        return None
    if ml >= unsupported:       # 文本层支持 MinerU ⇒ 不是共识错误
        return None
    return {"kind": "consensus_suspect",
            "dice_m_p": round(mp, 3), "dice_m_local": round(ml, 3),
            "reason": "两通道一致（%.2f）但与 PDF 文本层不符（%.2f）→ 疑似两通道同错"
                      % (mp, ml)}


def lost_content(mineru_text: str, local_text: str, *, ratio: float = 0.7,
                 min_len: int = 40) -> dict | None:
    """丢内容检测（**段落级长度比版**）：MinerU 段归一化长度相对文本层异常偏少。

    ⚠️ 同 `consensus_suspect`：段落级在真实论文上误报严重（参考文献条目、声明段与
    md→本地行组映射不一一对应），**不在 P14 链路启用**；链路改用 token 级检查。
    """
    nm, nl = normalize(mineru_text), normalize(local_text)
    if len(nm) < min_len or len(nl) < min_len:
        return None
    r = len(nm) / max(1, len(nl))
    if r >= ratio:
        return None
    return {"kind": "lost_content", "ratio": round(r, 3),
            "reason": "段落字符数为 PDF 文本层的 %.0f%%（可能丢内容）" % (r * 100)}


def similar(a: str, b: str) -> float:
    """归一化字符串的序列相似度（供需要更严格比较的调用方使用）。"""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb, autojunk=False).ratio()


def _tight_window(ctx: str, core: str, pad: int = 12, off: int | None = None) -> str:
    """从 ±60 上下文窗口里裁出**冲突点附近**的小窗（±pad 字符）。

    判"空格/断词"类冲突必须用小窗：±60 窗里常混着公式/换行连字符等噪声 ⇒ 整窗在文本层里
    逐字命中不了；小窗只含冲突点，命中率与区分度都高得多。

    `off` = 冲突核心在窗口内的偏移。**必须传**：insert 型冲突的 core 是空串
    （`Eficient→Efficient` 的 'f'），`ctx.find("")` 恒为 0 ⇒ 小窗会裁到窗口开头（错位置）。
    `char_conflicts` 的窗口是 `text[i1-60:i2+60]` ⇒ 偏移 = `min(i1, 60)`。
    """
    ctx = ctx or ""
    if not ctx:
        return ""
    if off is None:
        off = ctx.find(core) if core else 0
        if off < 0:
            return ctx
    off = max(0, min(off, len(ctx)))
    return ctx[max(0, off - pad): off + len(core or "") + pad]


def vote_conflict(c: dict, page_text: str, *, min_len: int = 6,
                  pad: int = 12) -> dict:
    """**冲突项的第三票**——输入 `char_conflicts` 的 conflict dict。

    冲突 dict 需要 `mineru.text` / `paddleocr.text`（差异核心）+ `evidence.m_ctx`/`p_ctx`
    （±60 上下文）与 `i1`/`j1`（核心在窗口内的偏移）。判据：**哪一侧"跨过冲突点"的窗口
    在 PDF 文本层里逐字出现**（窗口宽度多档递减，容忍局部噪声）。

    归一化模式自动选：两侧去掉空白后相同（=空格/断词类冲突，实测占 80%+）→ 用
    `normalize_spaced()`（保留单词边界，否则两侧一模一样判不出）；否则用 `normalize()`
    （容忍公式/排版差异）。

    两段式：A 小窗（±12/8/5）→ B 宽窗（±30/60）。两段都只有一侧命中 → 该侧胜出
    （`stage` 标注）；都命中或都不中 → 不裁决（`decisive=False`）。**任何情况下都不改文本**。
    """
    core_m = (c.get("mineru") or {}).get("text", "")
    core_p = (c.get("paddleocr") or {}).get("text", "")
    ev = c.get("evidence") or {}
    ctx_m = ev.get("m_ctx") or core_m
    ctx_p = ev.get("p_ctx") or core_p
    off_m = min(int(ev.get("i1") or 0), 60)
    off_p = min(int(ev.get("j1") or 0), 60)

    nm, npl = normalize(core_m), normalize(core_p)
    spaced = bool(nm) and nm == npl                     # 差异只在空白/断词
    nl = normalize_spaced(page_text) if spaced else normalize(page_text)
    if len(nl) < MIN_PAGE_CHARS:
        return {"verdict": "unknown", "m_hit": False, "p_hit": False, "m_dice": 0.0,
                "p_dice": 0.0, "judged": [], "spaced": spaced, "stage": "none",
                "decisive": False, "reason": "该页无可用文本层"}

    def _judge(pads: tuple[int, ...]) -> tuple[bool, bool, list[str], list[str]]:
        pm = _span_probes(ctx_m, core_m, off_m, spaced=spaced, min_len=min_len, pads=pads)
        pp = _span_probes(ctx_p, core_p, off_p, spaced=spaced, min_len=min_len, pads=pads)
        return (any(x in nl for x in pm), any(x in nl for x in pp), pm, pp)

    for stage, pads in (("tight", (pad, 8, 5)), ("wide", (30, 60))):
        m_hit, p_hit, pm, pp = _judge(pads)
        if not pm and not pp:
            continue
        if m_hit != p_hit:                              # 只有一侧命中 → 有区分度，可裁决
            return {"verdict": "mineru" if m_hit else "paddleocr", "m_hit": m_hit,
                    "p_hit": p_hit,
                    "m_dice": round(dice(nm, nl), 3), "p_dice": round(dice(npl, nl), 3),
                    "judged": ["mineru", "paddleocr"], "spaced": spaced, "stage": stage,
                    "decisive": True,
                    "reason": "文本层逐字命中：M%s P%s（%s窗/%s）"
                              % ("✓" if m_hit else "✗", "✓" if p_hit else "✗",
                                 "小" if stage == "tight" else "宽",
                                 "含空格比对" if spaced else "去空白比对")}
        if stage == "wide":                             # 宽窗仍无区分 → 不下结论
            return {"verdict": "both" if (m_hit and p_hit) else "neither",
                    "m_hit": m_hit, "p_hit": p_hit,
                    "m_dice": round(dice(nm, nl), 3), "p_dice": round(dice(npl, nl), 3),
                    "judged": ["mineru", "paddleocr"], "spaced": spaced, "stage": stage,
                    "decisive": False,
                    "reason": "文本层未能区分（两侧窗口都%s命中）"
                              % ("能" if (m_hit and p_hit) else "未")}
    return {"verdict": "unknown", "m_hit": False, "p_hit": False, "m_dice": 0.0,
            "p_dice": 0.0, "judged": [], "spaced": spaced, "stage": "none",
            "decisive": False, "reason": "两侧片段都过短，文本层无法判定"}


# ---------------------------------------------------------------- token 级共识检查
# 段落级 Dice 版在真实论文上误报严重（见上面两个函数的说明）⇒ 链路改用**token 级**：
# 只问"这个公式/数值在它所在页的 PDF 文本层里到底有没有"。不依赖段落对齐 ⇒ 误报面小。

_UNIT = (r"eV|keV|MeV|µm|μm|nm|mm|cm|dm|m|mg|g|kg|mA|A|mV|V|Hz|kHz|MHz|Pa|kPa|MPa|GPa|"
         r"°C|K|s|ms|min|h|%|wt|vol|mol|M|mM|nM|rpm|dpi|F|Ω|S|W|J|N|T|mT")
_TOKEN_RES = (
    # ① 数值 + 单位（480 µm / 9.80 A m−2 kg−1 / 153.0 mF cm−2 …）
    re.compile(r"\d+(?:\.\d+)?\s*(?:" + _UNIT + r")\b"),
    # ② LaTeX 里的化学式/带下标公式（BF_4^-, CoO_x, H_2O, \Nu_{2}, CO_2）
    re.compile(r"\$[^$]{0,80}?\$"),
)


def salient_tokens(text: str) -> list[str]:
    """从段落文本里抽取**可核对的高信号 token**（公式/带单位数值），归一化后去重。

    只取"含数字"的 token：纯词/纯符号差异交给 char_conflicts，这里专治 P12 的天生盲区
    （两通道同错的公式/下标/单位，如 `BF₄⁻` 的下标 4 丢失、`\\Nu_{2}`、`480 µm`）。
    """
    out: list[str] = []
    raw = text or ""
    cands: list[str] = []
    for m in _TOKEN_RES[0].finditer(raw):
        cands.append(m.group(0))
    for m in _TOKEN_RES[1].finditer(raw):
        inner = m.group(0).strip("$")
        if re.search(r"\d", inner):
            cands.append(inner)
    for c in cands:
        n = normalize(c)
        if len(n) < 3 or not re.search(r"\d", n):
            continue                      # 太短/无数字 → 不判（证据不足）
        if n not in out:
            out.append(n)
    return out


def unverified_tokens(mineru_text: str, page_text: str) -> list[dict]:
    """该段的高信号 token 里，**在对应页文本层中找不到**的那些（疑似两通道同错/丢内容）。

    语义（保守）：文本层是"电子版 PDF 的真值近似"，找不到 ⇒ 人工看一眼值得；
    **绝不据此改文本**（只进质量提示）。返回 [{token, source}]。
    """
    nl = normalize(page_text)
    if len(nl) < MIN_PAGE_CHARS:
        return []
    bad: list[dict] = []
    for tok in salient_tokens(mineru_text):
        if tok in nl or dice(tok, nl) >= 0.98:
            continue
        bad.append({"token": tok, "source": "third_signal"})
    return bad
