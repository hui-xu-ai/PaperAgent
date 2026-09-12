#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
文件: paperparse/core/consensus_scan.py
功能: C 层 AI 共识扫描——发现**词典外**的共识错误候选（学习闭环样本源）

背景:
  确定性词典层（consensus_fix）能修"词典命中"的畸形（BF−→BF₄⁻），但词典外的
  新化合物（双通道同错、diff 零报告）它管不了。本模块把引擎判定**边界外**的
  带电荷化学 token（find_boundary_candidates）批量送 AI，AI 给出规范化学式建议
  → review.json（GUI 待确认）→ 用户确认后提升为 learned 词典条目（下次解析自动应用）。

设计:
  - 复用 dual_ai_review.OpenAICompatProvider（环境链 DEEPSEEK_* → SILICONFLOW_*）
  - AI 建议用**元素表校验**（domain.json 118 元素，运行时零 pyvalem 依赖）：
    建议式解析合法（元素+电荷）才接受，非法/幻觉丢弃
  - 一篇一批 + 自动分块重试（参照 arbitrate）

对外接口: ai_scan / build_scan_review_items
"""
from __future__ import annotations

import json
import re

from paperparse.core.consensus_fix import load_domain_dict
from paperparse.core.dual_ai_review import OpenAICompatProvider

__all__ = ["ai_scan", "build_scan_review_items"]

_SYSTEM = """你是科学论文 OCR 校正助手。输入是论文中被 OCR 识别成"残缺化学式"的 token：
元素序列合法且带电荷，但可能缺下标数字、缺/错电荷、元素误写（希腊字母污染等）。
对每个 token，判断它最可能是什么化合物/离子的完整规范式。

输出要求（严格 JSON 数组，无其他文字）：
[{"id":0,"suggestion":"BF4-","unicode":"BF₄⁻","reason":"四氟硼酸根，缺下标4","confidence":0.9}]

规则：
- suggestion 用可解析化学式（电荷写法：数字在符号前，如 BF4- / SO4-2 / NH4+ / Fe3+）
- unicode 用 Unicode 上下标形态（BF₄⁻ / SO₄²⁻ / NH₄⁺）
- 只改明显的 OCR 残缺；不确定或属正常词（如 "Co-P" 是上下文）→ 输出
  {"id":N,"suggestion":"","reason":"keep/不确定","confidence":0}
- confidence 0-1：越确定越高"""


def _validate_suggestion(suggestion: str) -> dict | None:
    """校验 AI 建议式：优先 pyvalem（精确处理 pyvalem 风格电荷 SO4-2/NH4+），
    不可用/拒绝时降级元素表解析（_canonical_form）。非法/幻觉 → None。"""
    if not suggestion:
        return None
    try:
        from pyvalem.formula import Formula
        fo = Formula(suggestion)
        stoich = {k: (v or 1) for k, v in dict(fo.atom_stoich).items()}
        if not stoich:
            return None
        return {"elements": stoich, "charge": int(fo.charge)}
    except Exception:  # noqa: BLE001 - pyvalem 拒绝/未装 → 降级
        pass
    d = load_domain_dict()
    from paperparse.core.consensus_fix import _canonical_form
    cand = _canonical_form(suggestion, d["_elements_set"])
    if not cand or not cand["has_charge"]:
        return None
    return {"elements": cand["elements"], "charge": cand["charge"]}


def _parse_json_list(text: str) -> list[dict]:
    """宽容解析 AI 输出：剥 ```json 围栏 / 取首个 [ ... ] 块 / JSON 解码"""
    text = re.sub(r"```(?:json)?", "", text).strip()
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def ai_scan(candidates: list[dict], *, provider: OpenAICompatProvider | None = None,
            paper: str = "", batch_size: int = 50) -> list[dict]:
    """批量送 AI 共识扫描 → [{token, suggestion, unicode, reason, confidence, valid}]
    valid=False = AI 不可用/建议非法/解析失败（调用方决定是否展示）。"""
    if not candidates:
        return []
    provider = provider or OpenAICompatProvider()
    if not provider.available():
        return [{"token": c["token"], "suggestion": "", "unicode": "",
                 "reason": "AI 不可用（未配置 DEEPSEEK_API_KEY / SILICONFLOW_API_KEY）",
                 "confidence": 0.0, "valid": False} for c in candidates]

    out: list[dict] = []
    for start in range(0, len(candidates), batch_size):
        batch = candidates[start:start + batch_size]
        lines = []
        for i, c in enumerate(batch):
            lines.append('<item id=%d> token: %s | 元素: %s | 电荷: %s | 上下文: %s'
                         % (start + i, c.get("token", ""), c.get("elements", {}),
                            c.get("charge", ""), c.get("context", "")[:120]))
        parsed: dict = {}
        err = ""
        for attempt in range(2):
            try:
                resp = provider.complete([
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": "论文：%s\n%s" % (paper or "-", "\n".join(lines))},
                ], max_tokens=4096)
                items = _parse_json_list(resp)
                parsed = {v.get("id"): v for v in items if isinstance(v, dict)}
                if len(parsed) >= len(batch):
                    break
                err = "建议数不足（%d/%d）" % (len(parsed), len(batch))
            except Exception as e:  # noqa: BLE001 - 网络/限流 → 重试
                err = str(e)[:80]
        for i, c in enumerate(batch):
            v = parsed.get(start + i)
            if not v:
                out.append({"token": c["token"], "suggestion": "", "unicode": "",
                            "reason": err or "AI 未输出", "confidence": 0.0,
                            "valid": False})
                continue
            sug = str(v.get("suggestion", "") or "").strip()
            chk = _validate_suggestion(sug)
            if not chk:
                out.append({"token": c["token"], "suggestion": "", "unicode": "",
                            "reason": "建议非法: %s" % sug[:40], "confidence": 0.0,
                            "valid": False})
                continue
            out.append({
                "token": c["token"], "suggestion": sug,
                "unicode": str(v.get("unicode", "") or ""),
                "reason": str(v.get("reason", "") or "")[:80],
                "confidence": float(v.get("confidence", 0) or 0),
                "valid": True, "elements": chk["elements"], "charge": chk["charge"]})
    return out


def build_scan_review_items(paragraphs, scans: list[dict],
                            by_token: dict[str, list[dict]],
                            page_map: dict[str, int] | None = None) -> list[dict]:
    """扫描建议 → review.json items（GUI 复核；ai.verdict=paddleocr conf<0.9 待确认）。
    by_token: {para_id: fixes(边界候选)} 由调用方预建（token → 段落定位）。
    page_map: para_id → 页码（PDF 页图定位；缺省 1）。"""
    page_map = page_map or {}
    items: list[dict] = []
    for s in scans:
        if not s.get("valid") or not s.get("suggestion"):
            continue
        para_id = None
        for pid, cands in by_token.items():
            if any(c["token"] == s["token"] for c in cands):
                para_id = pid
                break
        r = next((x for x in paragraphs if x.para_id == para_id), None)
        if r is None:
            continue
        mid = ("md%d" % (r.md_idx or [1])[0]) if r.md_idx else ("para-" + r.para_id)
        sugg_text = r.text.replace(s["token"], s.get("unicode") or s["suggestion"], 1)
        items.append({
            "report_idx": 0,
            "page": page_map.get(para_id, 1),
            "mineru": {"block_id": mid, "kind": r.kind, "text": r.text},
            "paddleocr": {"block_id": "paddle-" + r.para_id, "kind": r.kind,
                          "text": sugg_text},
            "ai": {"verdict": "paddleocr",
                   "reason": ("AI 共识扫描: %s (conf %s)" % (s["reason"], s["confidence"]))[:80],
                   "confidence": min(s["confidence"], 0.85), "applied": False},
            "user_choice": "", "auto_resolved": "",
            "evidence": {"para_id": r.para_id, "md_idx": r.md_idx or [],
                         "kind": "domain_ai", "token": s["token"],
                         "suggestion": s["suggestion"],
                         "unicode": s.get("unicode", "")},
        })
    return items
