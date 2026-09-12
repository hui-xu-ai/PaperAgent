#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: skill/src/paperparse/core/para_align_report.py
功能: T-A 段落级对齐的"报告层"：对齐报告渲染（JSON/可读文本）+ 拼接算法缺陷清单。
      核心对齐算法在 para_align.py（parse_md_paras/align_docs）。
对外接口: render_json_report / render_text_report / defect_list
版本: v1.0.0 (2026-08-18)
版本历史:
  v1.0.0 初始版本（从 para_align.py 拆分，遵守单文件 ≤400 行规范）
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from paperparse.core.calibration_md import normalize
from paperparse.core.para_align import AlignReport, ParaDoc, strip_latex

__all__ = ["render_json_report", "render_text_report", "defect_list"]


def _tokens(norm: str) -> set[str]:
    """[局部] 词集合（Dice 用，与 para_align 同规则）"""
    toks = set(re.findall(r"[a-z0-9]+", norm))
    cjk = list(re.findall(r"[\u4e00-\u9fff]", norm))
    if cjk:
        toks.update(cjk)
        toks.update(a + b for a, b in zip(cjk, cjk[1:]))
    return toks


def _dice(a_norm: str, b_norm: str) -> float:
    """[局部] Dice 系数"""
    ta, tb = _tokens(a_norm), _tokens(b_norm)
    if not ta or not tb:
        return 0.0
    return 2.0 * len(ta & tb) / (len(ta) + len(tb))


def render_json_report(report: AlignReport, out_path: str | Path) -> Path:
    """[全局] 对齐报告 → JSON 文件（UTF-8）"""
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
                 encoding="utf-8")
    return p


def render_text_report(report: AlignReport, out_path: str | Path) -> Path:
    """[全局] 对齐报告 → 可读文本文件（含不匹配区域与差异明细）"""
    lines: list[str] = []
    st = report.stats
    lines.append("# 段落对齐报告")
    lines.append("")
    lines.append("- 自动版: %s （%d 段）" % (report.auto_path, st["auto_para_count"]))
    lines.append("- 校准版: %s （%d 段）" % (report.calib_path, st["calib_para_count"]))
    lines.append("- 匹配: %d 对（平均 Dice %.3f，低分对 %d）"
                 % (st["matched_count"], st["avg_dice"], st["low_dice_pairs"]))
    lines.append("- 不匹配区域: %d 处（shift=%d / extra=%d / missing=%d）"
                 % (st["region_count"], st["region_by_type"]["shift"],
                    st["region_by_type"]["extra"], st["region_by_type"]["missing"]))
    for note in report.notes:
        lines.append("- 备注: " + note)
    lines.append("")

    if report.regions:
        lines.append("## 不匹配区域")
        for r in report.regions:
            lines.append("")
            lines.append("### %s  auto=%s  calib=%s" % (
                r.region_type, r.auto_range, r.calib_range))
            for t in r.auto_texts:
                lines.append("  [自动] " + t)
            for t in r.calib_texts:
                lines.append("  [校准] " + t)

    diff_pairs = [x for x in report.matched if x.diffs]
    if diff_pairs:
        lines.append("")
        lines.append("## 匹配对内差异明细（%d 对）" % len(diff_pairs))
        for x in diff_pairs[:40]:
            lines.append("")
            lines.append("### auto#%d ↔ calib#%d  dice=%.3f%s"
                         % (x.auto_idx, x.calib_idx, x.dice,
                            "  [标题]" if x.both_heading else ""))
            for d in x.diffs[:6]:
                lines.append("  [%s] 自动句: %s" % (d.op, d.auto_sent or d.auto_snippet[:80]))
                lines.append("  [%s] 校准句: %s" % (d.op, d.calib_sent or d.calib_snippet[:80]))
    lines.append("")
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def defect_list(report: AlignReport, auto_doc: ParaDoc,
                calib_doc: ParaDoc) -> list[dict]:
    """[全局] 拼接算法缺陷清单（供 T-B 排查 / AI 学习调试经验）

    规则:
      - heading_split：自动版多个连续项拼起来 ≈ 校准版单个标题（标题+续行被拆散）
      - heading_fragment：自动版标题是校准版标题的片段（截断）
      - shift：不匹配区域同时含自动版与校准版段 → 词段错位
      - extra：仅自动版多段 → 重复/污染/分类误判嫌疑
      - missing：仅校准版有段 → 漏接/被过滤嫌疑
      - low_dice：匹配对 dice<0.8 → 内容差异
    参数:
        report: align_docs() 产物
        auto_doc / calib_doc: 对应 parse_md_paras() 产物
    返回:
        缺陷列表 [{"id","type","auto_idx","calib_idx","detail","suspect"}]
    """
    defects: list[dict] = []
    n = 0

    def add(dtype: str, auto_idx, calib_idx, detail: str, suspect: str) -> None:
        nonlocal n
        n += 1
        defects.append({"id": "D%03d" % n, "type": dtype, "auto_idx": auto_idx,
                        "calib_idx": calib_idx, "detail": detail, "suspect": suspect})

    # 1) 标题分割/截断：校准版单个标题 ≈ 自动版连续多个项（标题+续行被拆散）
    calib_heads = [(x.idx, x.text) for x in calib_doc.items if x.is_heading]
    auto_norms = [normalize(strip_latex(x.text)) for x in auto_doc.items]
    for ci, ctext in calib_heads:
        cn = normalize(strip_latex(ctext))
        if len(cn) < 8:
            continue
        # 起点：自动版某项 normalize 是校准标题的前缀/片段（且短于标题）
        starts = [k for k, an in enumerate(auto_norms)
                  if 4 <= len(an) < len(cn) and (cn.startswith(an) or an in cn)]
        best: tuple | None = None
        for k in starts:
            j = k
            while j + 1 < len(auto_norms):
                joined = " ".join(auto_norms[k: j + 2])
                if cn.startswith(joined) or _dice(joined, cn) > _dice(
                        " ".join(auto_norms[k: j + 1]), cn) * 1.05:
                    j += 1
                else:
                    break
            if best is None or (j - k) > (best[1] - best[0]):
                best = (k, j)
        if best:
            k, j = best
            if j > k and _dice(" ".join(auto_norms[k: j + 1]), cn) >= 0.8:
                part_ids = [auto_doc.items[x].idx for x in range(k, j + 1)]
                add("heading_split", part_ids, ci,
                    "校准标题「%s」被自动版拆成 %d 段：%s"
                    % (ctext[:60], j - k + 1, " | ".join(
                        auto_doc.items[x].text[:30] for x in range(k, j + 1))),
                    "stitch 标题续行合并（同字号标题续行未并入）/ block_classify 标题判定")
        elif starts:
            ai = auto_doc.items[starts[0]].idx
            add("heading_fragment", ai, ci,
                "自动版标题「%s」只是校准标题「%s」的片段（截断）"
                % (auto_doc.items[starts[0]].text[:60], ctext[:60]),
                "标题行提取截断 / 标题续行未合并")

    # 2) 不匹配区域 → shift / extra / missing
    for r in report.regions:
        if r.region_type == "shift":
            add("shift", list(r.auto_range), list(r.calib_range),
                "错位区域：自动版 %s ↔ 校准版 %s（前段：%s）"
                % (r.auto_range, r.calib_range,
                   (r.auto_texts[0][:50] if r.auto_texts else "")),
                "列检测/跨栏跨页阅读顺序/悬挂续接 逐环排查（T-B）")
        elif r.region_type == "extra":
            texts = " | ".join(t[:40] for t in r.auto_texts[:3])
            add("extra", list(r.auto_range), [],
                "自动版多出 %d 段：%s" % (len(r.auto_texts), texts),
                "重复拼接/污染行未剥离/分类误判（meta 行进正文）")
        else:
            texts = " | ".join(t[:40] for t in r.calib_texts[:3])
            add("missing", [], list(r.calib_range),
                "自动版缺少 %d 段：%s" % (len(r.calib_texts), texts),
                "段落被过滤（噪声剥离过严）/ 悬挂续接吞并")

    # 3) 匹配对内低分 → 内容差异
    for m in report.matched:
        if m.dice < 0.8 and not m.both_heading:
            a = auto_doc.items[m.auto_idx - 1]
            c = calib_doc.items[m.calib_idx - 1]
            add("low_dice", m.auto_idx, m.calib_idx,
                "dice=%.3f 差异 %d 处：自动「%s…」↔ 校准「%s…」"
                % (m.dice, len(m.diffs), a.text[:40], c.text[:40]),
                "缺词/词序/连字/OCR 差异（MinerU vs PyMuPDF）")
    return defects
