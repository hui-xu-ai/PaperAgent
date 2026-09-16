# -*- coding: utf-8 -*-
"""解析后审计（L2 文本层一致性 + L3 产物质量断言）—— ★2026-09-17

为什么（用户问题）："如果你的触发判据误判、导致没开启 OCR，造成后续识别错误，怎么办？"
答案不能只靠"把触发调准"——**必须有事后兜底层**，它不依赖任何事前判据，只看结果：

- **L2 `build_text_layer_audit`**：把 MinerU 产物与 **PDF 自带文本层**逐段对照，汇总
  · 输出里的高信号 token 在文本层对应页找不到（`unverified_tokens`，**全篇**而非只未配对段）；
  · 输出里的可疑形态：控制字符残留、`数字 空格 数字`（数字被拆散）、`数字 字母 数字`（疑似
    `o2 nm` 这类"可见错映射"）、`<sup>纯单词>`（drop-cap/小型大写被切碎）；
  · 文本层自身的坏字形（C0/PUA/U+FFFD）——该页字形映射不可信 → 产出**可疑清单 + 严重度 +
    建议动作**（`rescan_ocr` = 用 OCR 模式重解析本篇）。
- **L3 `assert_quality`**：对最终产物做廉价硬断言（控制符 0 / `$` 配平 / 图数与图注数一致 /
  公式自检不超阈值 / `<sup>` 碎片不超阈值），不达标 → 由 backend 转成任务 warning + 落盘。

两者都**只报告、不改文**（改文由既有护栏路径负责），可回退、可审计。
"""
from __future__ import annotations

import re

# 文本层坏字形（与 core.parse_params.text_layer_signals 同口径）
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_PUA_RE = re.compile(r"[\ue000-\uf8ff]")
_SUP_WORD_RE = re.compile(r"<sup>[A-Za-z]{2,}</sup>")
_HOLE_RE = re.compile(r"(?<=\d)[ \t]{1,3}(?=\d)")
_ALNUM_RE = re.compile(r"(?<=\d)[A-Za-z](?=\d)")

SEVERITY_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}


def _norm_ws(t: str) -> str:
    return re.sub(r"\s+", " ", t or "").strip()


def build_text_layer_audit(items: list, *, max_suspects: int = 20) -> dict:
    """[全局] L2：逐段对照产物与文本层 → 审计摘要。

    `items`：`[{"para_id": str, "page": int, "text": str, "page_raw": str}]`
    （`page_raw` 为该段所在页的**文本层原文**；空/不可用时该段只计入 `paras_no_pagetext`）。
    返回 `{severity, counts, suspects[], suggested_action}`；**纯函数、不改文**。
    """
    counts = {"paras": 0, "paras_no_pagetext": 0, "unverified": 0, "out_ctrl": 0,
              "out_hole": 0, "out_alnum": 0, "out_sup_word": 0, "page_ctrl": 0,
              "page_pua": 0, "page_fffd": 0}
    suspects: list = []
    for it in items or []:
        text = it.get("text") or ""
        page_raw = it.get("page_raw") or ""
        if not text.strip():
            continue
        if not page_raw.strip():
            counts["paras_no_pagetext"] += 1     # 该段所在页无可用文本层（映射失败/扫描件）
            continue
        counts["paras"] += 1
        # 文本层自身的坏字形
        p_ctrl = len(_CTRL_RE.findall(page_raw))
        p_pua = len(_PUA_RE.findall(page_raw))
        p_fffd = page_raw.count("\ufffd")
        counts["page_ctrl"] += p_ctrl
        counts["page_pua"] += p_pua
        counts["page_fffd"] += p_fffd
        # 输出侧形态
        o_ctrl = len(_CTRL_RE.findall(text))
        o_hole = len(_HOLE_RE.findall(text))
        o_alnum = len(_ALNUM_RE.findall(text))
        o_sup = len(_SUP_WORD_RE.findall(text))
        counts["out_ctrl"] += o_ctrl
        counts["out_hole"] += o_hole
        counts["out_alnum"] += o_alnum
        counts["out_sup_word"] += o_sup
        bad_tokens: list = []
        try:
            from paperparse.core.local_text import unverified_tokens
            bad_tokens = unverified_tokens(text, page_raw) or []
        except Exception:  # noqa: BLE001 - 审计失败不影响解析
            bad_tokens = []
        counts["unverified"] += len(bad_tokens)

        reasons = []
        if o_ctrl:
            reasons.append("产物含 %d 个控制字符（乱码）" % o_ctrl)
        if bad_tokens:
            reasons.append("产物有 %d 个高信号 token 在文本层找不到（如 %s）"
                           % (len(bad_tokens), ", ".join(
                               str(t.get("token")) for t in bad_tokens[:3])))
        if p_ctrl or p_pua or p_fffd:
            reasons.append("该页文本层含坏字形 ctrl=%d pua=%d fffd=%d（字形映射不可信）"
                           % (p_ctrl, p_pua, p_fffd))
        if o_sup:
            reasons.append("产物含 %d 处 `<sup>词</sup>` 碎片（drop-cap/小型大写）" % o_sup)
        if not reasons:
            continue
        sev = "high" if (o_ctrl or bad_tokens and (p_ctrl or p_pua)) else \
            ("medium" if (p_ctrl or p_pua or p_fffd or o_sup) else "low")
        suspects.append({"para_id": it.get("para_id"), "page": it.get("page"),
                         "severity": sev, "reasons": reasons[:3],
                         "samples": {"ctrl": o_ctrl, "holes": o_hole,
                                     "alnum": o_alnum, "sup_word": o_sup,
                                     "unverified": len(bad_tokens)}})
    suspects.sort(key=lambda s: -SEVERITY_ORDER.get(s["severity"], 0))
    worst = max((SEVERITY_ORDER.get(s["severity"], 0) for s in suspects), default=0)
    severity = {v: k for k, v in SEVERITY_ORDER.items()}[worst]
    return {
        "severity": severity,
        "counts": counts,
        "suspects": suspects[:max_suspects],
        "suspect_total": len(suspects),
        # 建议动作：high/medium 时建议"用 OCR 模式重解析本篇"（成本 = 页数 ×1）
        "suggested_action": "rescan_ocr" if worst >= SEVERITY_ORDER["medium"] else "none",
    }


def assert_quality(*, en_md: str, doc: dict | None = None, stats: dict | None = None,
                   max_sup_word: int = 2, formula_issue_ratio: float = 0.05) -> dict:
    """[全局] L3：对最终产物做廉价硬断言（不达标只报告）。

    断言：① 控制字符 0；② `$` 配平；③ `<sup>纯单词>` 碎片 ≤ 阈值（drop-cap 未修）；
    ④ 图与图注数一致（|markers-captions| ≤ 1，来自 qa_report.figures）；
    ⑤ 公式自检 issue 数 ≤ max(3, 段落数 × formula_issue_ratio)。
    """
    stats = stats or {}
    en = en_md or ""
    failed: list = []
    detail: dict = {}
    n_ctrl = len(_CTRL_RE.findall(en))
    detail["control_chars"] = n_ctrl
    if n_ctrl:
        failed.append("control_chars: en.md 含 %d 个控制字符" % n_ctrl)
    n_dollar = en.count("$")
    detail["dollar_odd"] = bool(n_dollar % 2)
    if n_dollar % 2:
        failed.append("dollar_balance: `$` 出现 %d 次（未配平）" % n_dollar)
    n_sup = len(_SUP_WORD_RE.findall(en))
    detail["sup_word"] = n_sup
    if n_sup > max_sup_word:
        failed.append("sup_word: `<sup>词</sup>` 碎片 %d 处（>%d）" % (n_sup, max_sup_word))
    figs = stats.get("figures") or {}
    if isinstance(figs, dict) and figs:
        diff = abs(int(figs.get("image_markers") or 0) - int(figs.get("captions") or 0))
        detail["figure_marker_caption_diff"] = diff
        # `match=False` 即失败——差 1 张正是的"并排双图漏提"事故形态（markers 5 / captions 6）
        if figs.get("match") is False or diff > 1:
            failed.append("figure_match: 图标记 %s 与图注 %s 不一致"
                          % (figs.get("image_markers"), figs.get("captions")))
    # `doc` 可能是 ArticleDocument（dataclass）或已加载的 dict ⇒ 两种都接
    if isinstance(doc, dict):
        paras = doc.get("paragraphs")
    else:
        paras = getattr(doc, "paragraphs", None)
    n_paras = len(paras or [])
    # `formula_self_check` 在 p14 里是**列表**（issues），在 qa_report 里是 dict ⇒ 两种都接
    fsc = stats.get("formula_self_check")
    n_issues = len((fsc.get("issues") if isinstance(fsc, dict) else fsc) or [])
    detail["formula_issues"] = n_issues
    detail["paragraphs"] = n_paras
    limit = max(3, int(n_paras * formula_issue_ratio))
    if n_paras and n_issues > limit:
        failed.append("formula_self_check: %d 处 > 阈值 %d" % (n_issues, limit))
    return {"passed": not failed, "failed": failed, "detail": detail}
