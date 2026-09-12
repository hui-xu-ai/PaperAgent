#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: paperparse/core/rule_mining.py
功能: P11-4 清洗规则学习：从双通道差异样本挖掘清洗规则候选
      text_conflict 样本 → 字符级差异 → 模式分类（spacing/dict/formula/other）→
      候选规则（rule_library schema 兼容）→ 语料验证（防误伤）→ 置信度
对外接口: mine_clean_rules / CandidateRule
版本: v1.0.0 (2026-08-22)
版本历史:
  v1.0.0 初始版本（adma 实测：空格差异 "1 of15"→"1 of 15"×8 是高置信 spacing 规则；
         词替换 "Eficient"→"Efficient" 是 dict 规则候选）
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from paperparse.core.dual_align import DualReport

from paperparse.middleware.schema import TextBlock

__all__ = ["mine_clean_rules", "CandidateRule", "MINING_RULE_PREFIX"]

MINING_RULE_PREFIX = "R-mining-"     # 挖掘规则 id 前缀（入库时由 add_rule 改写为 R-YYYYMMDD-NNN）

# 参与差异样本提取的噪声词（页眉/页脚/页码类差异不产规则）
_NOISE_HINTS = ("check for updates", "wiley", "elsevier", "©", "download", "sciencenews")


@dataclass
class CandidateRule:
    """[全局] 候选清洗规则（schema 兼容，待验证后入库）"""
    category: str = ""          # rule_library.RULE_CATEGORIES 之一
    pattern_type: str = ""      # regex / dict
    pattern: str = ""           # regex 或 JSON dict
    replacement: str = ""
    confidence: float = 0.5
    direction: str = "po->mineru"   # 清洗方向：脏侧 -> 参考侧
    clean_channel: str = "paddleocr"   # 干净侧通道（验证误伤只统计该侧）
    evidence: list[dict] = field(default_factory=list)   # {paper, page, before, after}
    scope: str = "global"
    description: str = ""

    def to_rule(self) -> dict:
        """[全局] 候选 → 规则 dict（**必须携带方向字段**——P12 安全红线：
        direction/clean_channel 缺失会让规则失去作用域，无法防止误伤正确侧）"""
        return {
            "rule_id": "",          # add_rule 生成
            "category": self.category,
            "pattern_type": self.pattern_type,
            "pattern": self.pattern,
            "replacement": self.replacement,
            "confidence": self.confidence,
            "scope": self.scope,
            "source": "mining",
            "direction": self.direction,          # 脏侧 -> 干净侧（如 po->mineru）
            "clean_channel": self.clean_channel,  # 干净侧通道（验证误伤只统计该侧）
            "evidence": self.evidence,
            "description": self.description,
        }


# ---------- 字符级差异 → 模式 ----------

def _char_diffs(dirty: str, clean: str) -> list[tuple[str, str, str]]:
    """[局部] 字符级差异摘要：[(op, dirty_snippet, clean_snippet)]"""
    sm = difflib.SequenceMatcher(None, dirty, clean)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        ds = dirty[i1:i2]
        cs = clean[j1:j2]
        if not ds and not cs:
            continue
        out.append((tag, ds, cs))
    return out


def _is_spacing_only(diffs) -> bool:
    """[局部] 差异是否纯空白（insert/delete/replace 内容全为空白）"""
    for _, ds, cs in diffs:
        if (ds or "").strip() or (cs or "").strip():
            return False
    return True


def _word_token_diffs(dirty: str, clean: str) -> list[tuple[str, str]]:
    """[局部] 提取"整词替换"模式：在**词序列**上做 diff（避免取到子串）"""
    dw = re.findall(r"[A-Za-z]+", dirty)
    cw = re.findall(r"[A-Za-z]+", clean)
    sm = difflib.SequenceMatcher(None, dw, cw)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "replace":
            continue
        ds = " ".join(dw[i1:i2])
        cs = " ".join(cw[j1:j2])
        if len(ds.split()) == 1 and len(cs.split()) == 1 and len(ds) >= 3 and len(cs) >= 3:
            out.append((ds, cs))
    return out


# ---------- 挖掘入口 ----------

def mine_clean_rules(report: "DualReport", mineru_blocks: list[TextBlock],
                     po_blocks: list[TextBlock], *,
                     min_evidence: int = 1,
                     paper: str = "",
                     verdicts: dict[int, str] | None = None) -> list[CandidateRule]:
    """[全局] 从双通道报告挖掘清洗规则候选

    策略：
      1. 只取 text_conflict + format_diff 样本（含完整文本，经 block_id 定位）；
      2. 方向自适应：
         - spacing（纯空白差异）→ regex 候选（上下文词对），置信度 0.85（证据≥3）/0.6；
         - dict（整词替换）→ 方向由 AI 仲裁裁决（verdicts: item 索引 → verdict）；
           无裁决/方向未知样本不产规则（仅记录）；
      3. 语料验证：对两侧全部文本应用候选，统计误伤（干净侧被改写次数）。
    """
    if not report.items:
        return []
    m_by_id = {b.block_id: b for b in mineru_blocks}
    p_by_id = {b.block_id: b for b in po_blocks}

    spacing_samples: list[dict] = []   # {"dirty", "clean", "page", "clean_channel"}
    dict_samples: list[dict] = []
    # 来源：text_conflict（内容/词差异）+ format_diff（表示差异/空格粘连）
    for idx, it in enumerate(report.items):
        if it.diff_type not in ("text_conflict", "format_diff"):
            continue
        mid = it.mineru.get("block_id")
        pid = it.paddleocr.get("block_id")
        m_text = m_by_id.get(mid).text if mid and m_by_id.get(mid) else (it.mineru.get("text") or "")
        p_text = p_by_id.get(pid).text if pid and p_by_id.get(pid) else (it.paddleocr.get("text") or "")
        if not m_text or not p_text:
            continue
        # 参考文献块不产清洗规则（格式噪声）；页脚/页码类保留——"1 of15" 页号格式
        # 正是 spacing 规则的有效来源（_NOISE_HINTS 已过滤期刊名/版权等）
        m_kind = m_by_id.get(mid).kind if mid and m_by_id.get(mid) else ""
        p_kind = p_by_id.get(pid).kind if pid and p_by_id.get(pid) else ""
        if m_kind == "reference" or p_kind == "reference":
            continue
        if any(h in (m_text + " " + p_text).lower() for h in _NOISE_HINTS):
            continue
        diffs = _char_diffs(m_text, p_text)
        if not diffs:
            continue
        if _is_spacing_only(diffs):
            # 空格多的一侧 = clean（记录方向供验证误伤统计）
            if m_text.count(" ") >= p_text.count(" ") + 1:
                spacing_samples.append({"dirty": p_text, "clean": m_text,
                                        "page": it.page, "clean_channel": "mineru"})
            elif p_text.count(" ") >= m_text.count(" ") + 1:
                spacing_samples.append({"dirty": m_text, "clean": p_text,
                                        "page": it.page, "clean_channel": "paddleocr"})
        else:
            # 词差异方向由 AI 仲裁裁决（verdicts: item_index → mineru|paddleocr|both|neither）：
            #   verdict=paddleocr → mineru 侧词脏（dirty=mineru 词, clean=paddleocr 词）
            #   verdict=mineru     → paddleocr 侧词脏
            #   both/neither/无裁决 → 方向未知（样本保留但不计入规则置信度）
            v = (verdicts or {}).get(idx, "unknown")
            wd = _word_token_diffs(m_text, p_text)
            wr = _word_token_diffs(p_text, m_text)
            if v == "paddleocr":        # mineru 脏
                for dw, cw in wd:
                    if dw and cw:
                        dict_samples.append({"dirty": dw, "clean": cw, "page": it.page,
                                             "clean_channel": "paddleocr", "directed": True})
            elif v == "mineru":          # paddleocr 脏
                for dw, cw in wr:
                    if dw and cw:
                        dict_samples.append({"dirty": dw, "clean": cw, "page": it.page,
                                             "clean_channel": "mineru", "directed": True})
            else:
                # 方向未知：记录两个方向的词对（供 review/人工），不进规则
                for dw, cw in wd + wr:
                    if dw and cw:
                        dict_samples.append({"dirty": dw, "clean": cw, "page": it.page,
                                             "clean_channel": "unknown", "directed": False})

    rules: list[CandidateRule] = []
    # ---- spacing 候选：抽取 (w1 w2) 对 → 正则 ----
    w_pairs: dict[tuple[str, str], list[dict]] = {}
    for s in spacing_samples:
        for w1, w2 in _context_word_pairs(s["dirty"], s["clean"]):
            w_pairs.setdefault((w1, w2), []).append(s)
    for (w1, w2), evs in w_pairs.items():
        if len(evs) < min_evidence:
            continue
        pattern = "%s(%s)" % (re.escape(w1), re.escape(w2))
        repl = "%s %s" % (w1, w2)
        # 统一 clean_channel：取多数样本的方向
        chan = max({e["clean_channel"] for e in evs}, key=lambda c: sum(
            1 for e in evs if e["clean_channel"] == c))
        rules.append(CandidateRule(
            category="spacing", pattern_type="regex",
            pattern=pattern, replacement=repl,
            confidence=0.85 if len(evs) >= 3 else 0.6,
            direction="auto", clean_channel=chan,
            evidence=[{"paper": paper, "page": e["page"],
                       "before": e["dirty"][:80], "after": e["clean"][:80]} for e in evs],
            description="双通道挖掘：%s(%s) 缺空格" % (w1, w2)))
    # ---- dict 候选：整词替换（AI verdict 定向；无向样本仅记录不产规则） ----
    w_replace: dict[tuple[str, str], list[dict]] = {}
    unknown_samples: list[dict] = []
    for s in dict_samples:
        if s.get("directed"):
            key = (s["dirty"], s["clean"])
            w_replace.setdefault(key, []).append(s)
        else:
            unknown_samples.append(s)
    for (dw, cw), evs in w_replace.items():
        if len(evs) < min_evidence:
            continue
        chan = max({e["clean_channel"] for e in evs}, key=lambda c: sum(
            1 for e in evs if e["clean_channel"] == c))
        rules.append(CandidateRule(
            category="spacing", pattern_type="dict",
            pattern=json_dict(dw, cw), replacement="",
            confidence=round(0.5 * min(1.0, len(evs) / 3.0), 2),
            direction="mineru->paddleocr" if chan == "paddleocr" else "paddleocr->mineru",
            clean_channel=chan,
            evidence=[{"paper": paper, "page": e["page"],
                       "before": e["dirty"], "after": e["clean"]} for e in evs],
            description="双通道挖掘+AI仲裁：%s → %s" % (dw, cw)))
    return rules


def mine_char_rules(conflicts: list[dict], arbitrations: list, *,
                    paper: str = "") -> list[CandidateRule]:
    """[全局] P14 字符级差异 → 规则候选（拼写/断词词对，AI verdict 定向）

    与 mine_clean_rules（P12 块级）互补：p14 M8 段落内字符级 diff 的确认项
    （Eficient→Efficient、ofconductivity→of conductivity 类）→ dict 规则候选。

    安全红线（P15）：
    - 只产 **≥3 字符纯字母词对**（单字母 insert 如 'f' 不产规则——词边界执行器
      对单字母误伤风险高）；
    - 方向 = AI verdict 定向（verdict=paddleocr → mineru 脏，clean=paddle；
      verdict=mineru → paddle 脏）；both/neither/unresolved 不产规则；
    - 置信度保守（0.5 × min(1, 证据/2)）：单篇 1-2 证据远低于 auto 门槛 0.9，
      需跨篇聚合（tools/aggregate_dual_rules.py）后才可能自动应用。
    返回 CandidateRule 列表（to_rule() 可落 mined_rules.json，与 P12 同格式）。
    """
    arb_by_id = {a.id: a for a in (arbitrations or [])}
    samples: dict[tuple[str, str], list[dict]] = {}
    for i, c in enumerate(conflicts):
        a = arb_by_id.get(i)
        if not a or a.verdict not in ("mineru", "paddleocr"):
            continue
        m = (c.get("mineru") or {}).get("text", "")
        p = (c.get("paddleocr") or {}).get("text", "")
        if a.verdict == "paddleocr":
            dirty, clean, chan = m, p, "paddleocr"      # mineru 侧词脏
        else:
            dirty, clean, chan = p, m, "mineru"         # paddleocr 侧词脏
        # 只产 ≥3 字符纯字母词对（拼写/断词类；单字母 insert/delete 跳过）
        if not (re.fullmatch(r"[A-Za-z]{3,}", dirty)
                and re.fullmatch(r"[A-Za-z]{3,}", clean)):
            continue
        if dirty == clean:
            continue
        samples.setdefault((dirty, clean), []).append(
            {"page": c.get("page", 0), "clean_channel": chan})
    rules: list[CandidateRule] = []
    for (dw, cw), evs in samples.items():
        chan = max({e["clean_channel"] for e in evs},
                   key=lambda ch: sum(1 for e in evs if e["clean_channel"] == ch))
        rules.append(CandidateRule(
            category="spacing", pattern_type="dict",
            pattern=json_dict(dw, cw), replacement="",
            confidence=round(0.5 * min(1.0, len(evs) / 2.0), 2),
            direction=("mineru->paddleocr" if chan == "paddleocr"
                       else "paddleocr->mineru"),
            clean_channel=chan,
            evidence=[{"paper": paper, "page": e["page"],
                       "before": dw, "after": cw} for e in evs],
            description="P14 字符级挖掘+AI仲裁：%s → %s" % (dw, cw)))
    return rules


def persist_rules(candidates: list[CandidateRule],
                  rules_dir: str | Path | None = None) -> list[dict]:
    """[全局] 候选规则 → rule_library.add_rule 入库（learned 层）

    - regex：直接入库（replacement 必填）；
    - dict（词级 OCR 纠错，引擎已支持）：replacement 填 pattern JSON（schema 兼容，
      引擎按 pattern 执行词边界替换）；
    - 幂等：同 pattern 已存在则跳过。
    - **P12 安全红线**：auto_apply 门控 = confidence≥0.9 AND 方向明确（direction 非 auto/
      unknown）AND clean_channel 非空——方向不明的规则即使 conf≥0.9 也绝不允许自动应用
      （只能作用通道清洗侧或经人工复核补方向）。
    """
    from paperparse.core.rule_library import add_rule, default_rules_dir
    d = Path(rules_dir) if rules_dir else default_rules_dir()
    added = []
    for cand in candidates:
        rule = cand.to_rule()
        if rule["pattern_type"] == "dict" and not rule.get("replacement"):
            rule["replacement"] = rule["pattern"]   # dict 规则 replacement=pattern（schema 兼容）
        directed = bool(cand.clean_channel) and cand.direction not in (None, "", "auto", "unknown")
        rule["auto_apply"] = bool(directed and cand.confidence >= 0.9)
        try:
            added.append(add_rule(d, rule, level="learned"))
        except ValueError as e:
            if "已存在" in str(e):
                continue
            raise
    return added


def json_dict(*pairs) -> str:
    """[局部] (dirty, clean)* → dict 规则 pattern JSON"""
    import json
    d = {}
    it = iter(pairs)
    for a, b in zip(it, it):
        d[a] = b
    return json.dumps(d, ensure_ascii=False)


def _context_word_pairs(dirty: str, clean: str) -> list[tuple[str, str]]:
    """[局部] 从空格差异对提取相邻词对 (w1, w2)：
    clean 中有 "w1 w2"、dirty 中对应位置是 "w1w2"（词被粘连）。
    用 lookahead 使相邻词对可重叠（"1 of 15" 同时产出 "1 of" 与 "of 15"）。"""
    pairs = []
    for m in re.finditer(r"\b([A-Za-z0-9]+)(?= ([A-Za-z0-9]+)\b)", clean):
        w1, w2 = m.group(1), m.group(2)
        if w1 + w2 in dirty:
            pairs.append((w1, w2))
    return pairs


def validate_candidates(rules: list[CandidateRule],
                        mineru_blocks: list[TextBlock],
                        po_blocks: list[TextBlock]) -> list[CandidateRule]:
    """[全局] 语料验证（方向感知）：规则应用到**干净侧**文本不应产生改写（防误伤）

    脏侧的命中是正确修复，不计误伤；只有干净侧被改写才算 false positive。
    """
    import json as _json
    texts_by_channel = {"mineru": [b.text for b in mineru_blocks],
                        "paddleocr": [b.text for b in po_blocks]}
    out = []
    for r in rules:
        clean_texts = texts_by_channel.get(r.clean_channel, [])
        fp = 0
        if r.pattern_type == "regex":
            try:
                pat = re.compile(r.pattern)
                for t in clean_texts:
                    fp += len(pat.findall(t))
            except re.error:
                fp = 999
        elif r.pattern_type == "dict":
            try:
                mapping = _json.loads(r.pattern)
                for t in clean_texts:
                    for k in mapping:
                        fp += t.count(k)
            except Exception:
                fp = 999
        if fp > len(r.evidence):
            continue        # 干净侧改写超过证据数 → 规则过宽，丢弃
        if fp:
            r.confidence = round(r.confidence * 0.6, 2)
        r.description = r.description + ("（干净侧误伤 %d 处）" % fp if fp else "（干净侧无误伤）")
        out.append(r)
    return out
