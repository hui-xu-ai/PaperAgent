#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: paperparse/core/para_verify.py
功能: P14-M7 双通道整段验证：
      - 分别拼接两通道（mineru / paddleocr）的离散片段 → 完整段落；
      - 对同一段落（本地骨架定位）计算两通道**单词序列重叠率**
        （只计单词，剔除 LaTeX/数字/参考文献编号——用户规则）；
      - 用户细化：**不能只看单词集合，必须看片段序列**——两通道可能"单词
        一样但中间错位"（一段被拆开/片段乱序），集合重叠率会假高 → 用
        有序单词序列比较（LCS 长度比例 + 位移检测）；
      - 用途：**仅输出验证信号**（供用户检查/改算法），不自动改文本。
对外接口: verify_paragraphs / ParaVerify
版本: v1.0.0 (2026-08-24)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from paperparse.core.md_align import norm_text, _tokens, _dice

__all__ = ["verify_paragraphs", "ParaVerify", "verify_pair"]

# 阈值分级（仅提示，不自动改）
OVERLAP_OK = 0.9          # 高重叠：拼接验证通过
OVERLAP_REVIEW = 0.7      # 中重叠：复核清单
# LCS 错位检测：序列相似（单词集合重叠高）但 LCS 比例低 → 片段乱序/错位
LCS_SHIFT_RATIO = 0.85    # lcs_ratio < 集合重叠 * 该系数 → 疑似错位
WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")


@dataclass
class ParaVerify:
    """[全局] 一个段落的双通道验证结果"""
    para_id: str = ""             # 本地骨架段 id（定位用）
    mineru_words: list = field(default_factory=list)
    paddle_words: list = field(default_factory=list)
    overlap_set: float = 0.0      # 单词集合重叠（Jaccard 变体）
    overlap_seq: float = 0.0      # 单词序列重叠（LCS / 长序列）
    lcs_ratio: float = 0.0
    shifted: bool = False         # 疑似错位/乱序（集合高、序列低）
    verdict: str = "ok"           # ok / review / suspicious
    reason: str = ""

    def to_dict(self) -> dict:
        return {"para_id": self.para_id,
                "overlap_set": round(self.overlap_set, 3),
                "overlap_seq": round(self.overlap_seq, 3),
                "lcs_ratio": round(self.lcs_ratio, 3),
                "shifted": self.shifted, "verdict": self.verdict,
                "reason": self.reason}


def _word_seq(text: str) -> list[str]:
    """[局部] 单词序列（只计字母词，剔 LaTeX/数字/参考文献编号）"""
    t = norm_text(text)
    return WORD_RE.findall(t)


def _lcs_ratio(a: list[str], b: list[str]) -> float:
    """[局部] 最长公共子序列比例（相对较长序列）——词序一致度"""
    if not a or not b:
        return 0.0
    n, m = len(a), len(b)
    # 空间优化 DP（只保留两行）
    prev = [0] * (m + 1)
    for i in range(n):
        cur = [0] * (m + 1)
        for j in range(m):
            cur[j + 1] = prev[j] + 1 if a[i] == b[j] else max(prev[j + 1], cur[j])
        prev = cur
    return prev[m] / max(n, m)


def verify_pair(mineru_text: str, paddle_text: str, para_id: str = "") -> ParaVerify:
    """[全局] 单段验证：两通道段落文本 → 单词序列重叠 + 错位检测

    Q5 防线2（2026-08-26）：**段首对齐守卫**——P/M 前 8 个散文词的有序一致度
    < 0.25 → verdict=misaligned（段落配对错位/误拼接：如 cej RP017 把
    "In terms..." 块配到 "In addition..." 段）。调用方对 misaligned **跳过
    char_conflicts（不送 AI 仲裁）**，省 token。"""
    ma = _word_seq(mineru_text)
    pa = _word_seq(paddle_text)
    pv = ParaVerify(para_id=para_id, mineru_words=ma, paddle_words=pa)
    if not ma or not pa:
        pv.verdict = "suspicious"
        pv.reason = "空单词序列（段可能为空/全公式）"
        return pv
    # Q5 防线2：段首对齐守卫（集合重叠计算之前拦截——错位段不必算 LCS）
    if len(ma) >= 3 and len(pa) >= 3:
        from difflib import SequenceMatcher
        head_ratio = SequenceMatcher(None, ma[:8], pa[:8]).ratio()
        if head_ratio < 0.25:
            pv.verdict = "misaligned"
            pv.reason = "段首对齐失败（P/M 开头不同——段落配对错位/误拼接）"
            return pv
    # 集合重叠：较短序列单词在较长序列中的覆盖比例（用户语义"重叠比例"；
    # 全等/子集 → 接近 1.0；Dice 会被重复词稀释）
    sa, sb = set(ma), set(pa)
    shorter, longer = (sa, sb) if len(ma) <= len(pa) else (sb, sa)
    pv.overlap_set = len(shorter & longer) / len(shorter) if shorter else 0.0
    # 序列重叠（LCS 比例）
    pv.lcs_ratio = _lcs_ratio(ma, pa)
    pv.overlap_seq = pv.lcs_ratio
    # 错位检测：集合重叠高（单词几乎全在）但序列一致度低 → 片段乱序/错位
    if pv.overlap_set >= OVERLAP_REVIEW and pv.lcs_ratio < pv.overlap_set * LCS_SHIFT_RATIO:
        pv.shifted = True
    if pv.overlap_set >= OVERLAP_OK and pv.lcs_ratio >= OVERLAP_OK:
        pv.verdict = "ok"
    elif pv.shifted:
        pv.verdict = "suspicious"
        pv.reason = "单词集合重叠高但序列一致度低（片段可能错位/乱序）"
    elif pv.overlap_set >= OVERLAP_REVIEW:
        pv.verdict = "review"
        pv.reason = "单词重叠中等（部分文字识别差异）"
    else:
        pv.verdict = "suspicious"
        pv.reason = "单词重叠低（拼接疑点：断段/错拼/乱序）"
    return pv


def verify_paragraphs(local_paras: list, mineru_by_id: dict, paddle_by_id: dict,
                      pair_map: dict) -> dict:
    """[全局] 批量验证：本地骨架段落 → 两通道文本 → 验证信号清单

    参数:
        local_paras: 本地骨架 body 段（含 start_line/end_line）
        mineru_by_id: mineru 块 id → TextBlock（本地上文行 id → 块映射）
        paddle_by_id: paddleocr 块 id → TextBlock
        pair_map: 本地段 → (mineru 块 ids, paddle 块 ids)
    返回:
        {"items": [ParaVerify.to_dict()], "stats": {...}}
    """
    items = []
    stats = {"ok": 0, "review": 0, "suspicious": 0, "shifted": 0}
    for lp in local_paras:
        key = lp.para_id
        if key not in pair_map:
            continue
        m_ids, p_ids = pair_map[key]
        m_text = " ".join(mineru_by_id[i].text for i in m_ids if i in mineru_by_id)
        p_text = " ".join(paddle_by_id[i].text for i in p_ids if i in paddle_by_id)
        pv = verify_pair(m_text, p_text, para_id=key)
        items.append(pv.to_dict())
        stats[pv.verdict] = stats.get(pv.verdict, 0) + 1
        if pv.shifted:
            stats["shifted"] += 1
    return {"items": items, "stats": stats}
