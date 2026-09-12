#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: skill/src/paperparse/core/rule_engine.py
功能: 规则匹配引擎（P-ENHANCE R04 / S6.5）：按 6 类别固定顺序应用 rules/ 规则库到
      document 段落文本，置信度门控（auto_apply 自动 / 低置信进待确认清单），
      audit 记录每个修复。替代 anomaly_detect.apply_known_fixes（KNOWN_FIXES 硬编码）。

执行器顺序：latex → formula → variable → spacing → metadata → layout
对外接口: apply_rules / apply_spacing_local / RuleReport
版本: v0.1.0 (2026-08-22)
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from paperparse.core.rule_library import LEVELS, load_rules

__all__ = ["CATEGORY_ORDER", "apply_rules", "RuleReport"]

CATEGORY_ORDER = ("latex", "formula", "variable", "spacing", "metadata", "layout")
# level 优先级（应用顺序：user 覆盖优先）
_LEVEL_PRIORITY = {lv: i for i, lv in enumerate(LEVELS)}   # user=2 > builtin=1 > learned=0

# 词中粘连候选（spacing 本地对照用；非捕获组——findall 返回整词）
_MID_STOP = ("and", "the", "for", "with", "in", "on", "to", "by", "at", "from",
             "into", "onto", "upon", "through", "between", "within", "without")
_MID_STOP_RE = re.compile(r"[a-z]{2}(?:%s)[a-z]{2,}" % "|".join(_MID_STOP))
_TRIM = 120


def find_mid_joins(text: str) -> set[str]:
    """[局部] 词中粘连候选：先提取完整词（[a-z]{6,}），再查词内嵌 stopword
    （位置 2..len-sw-2）。正则从左优先会截出 "deandconductor" 这类错误前缀，
    先取整词可保证候选语义正确（如 electrodeandconductor）。"""
    out: set[str] = set()
    for w in re.findall(r"[a-z]{6,}", (text or "").lower()):
        for sw in _MID_STOP:
            idx = w.find(sw)
            if 2 <= idx <= len(w) - len(sw) - 2:
                out.add(w)
                break
    return out


class RuleReport:
    """[全局] 规则引擎报告（applied=自动应用 / pending=待 AI 审查）"""

    def __init__(self):
        self.applied: list[dict] = []
        self.pending: list[dict] = []

    def add(self, entry: dict, auto: bool):
        (self.applied if auto else self.pending).append(entry)

    def to_dict(self) -> dict:
        by_cat: dict[str, dict] = {}
        for lst, key in ((self.applied, "applied"), (self.pending, "pending")):
            for e in lst:
                c = e.get("category", "?")
                by_cat.setdefault(c, {key: 0, "pending": 0})
                by_cat[c][key] = by_cat[c].get(key, 0) + 1
        return {"fixed": len(self.applied), "pending_count": len(self.pending),
                "applied": self.applied, "pending": self.pending,
                "stats": by_cat}


def _entry(rule: dict, para_id: str, before: str, after: str, page: Any) -> dict:
    return {
        "rule_id": rule.get("rule_id", ""),
        "category": rule.get("category", ""),
        "level": rule.get("level", ""),
        "para_id": para_id,
        "page": page,
        "before": before[:_TRIM], "after": after[:_TRIM],
        "confidence": rule.get("confidence", 0.0),
        "source": rule.get("source", ""),
    }


def _ordered_rules(rules: list[dict]) -> list[dict]:
    """[局部] 类别内排序：level 优先级（user>builtin>learned）→ confidence 降序"""
    return sorted(rules,
                  key=lambda r: (_LEVEL_PRIORITY.get(r.get("level"), -1),
                                 -float(r.get("confidence", 0.0))))


def _apply_dict_rules(doc: Any, rules: list[dict], report: RuleReport,
                      include_heading: bool) -> int:
    """[局部] dict 类规则批量应用（P11 双通道挖掘的词级 OCR 纠错规则）

    pattern = JSON 对象 {脏词: 正确词}（如 {"diferent": "different"}）；
    替换为**词边界**匹配（防 "differentiation" 被 "diferent" 子串误伤）：
      整词全小写匹配 → 替换为正确词；首字母大写/全大写变体 → 保留大小写形态。
    """
    import json as _json
    fixed = 0
    for rule in _ordered_rules(rules):
        if rule.get("pattern_type") != "dict" or not rule.get("enabled", True):
            continue
        try:
            mapping = _json.loads(rule["pattern"])
        except Exception:
            continue
        if not isinstance(mapping, dict):
            continue
        for p in doc.paragraphs:
            if p.is_heading and not include_heading:
                continue
            t = p.text_en or ""
            new = t
            for dirty, clean in mapping.items():
                if not dirty or not clean:
                    continue
                # 词边界 + 大小写变体（Title/ALL CAPS）
                for variant, repl in _case_variants(dirty, clean):
                    pat = re.compile(r"(?<![A-Za-z])%s(?![A-Za-z])" % re.escape(variant))
                    new2, n = pat.subn(repl, new)
                    if n:
                        new = new2
            if new != t:
                auto = bool(rule.get("auto_apply", False))
                if auto:
                    p.text_en = new
                    report.add(_entry(rule, p.para_id, t, new,
                                      (p.coords or {}).get("pages")), auto=True)
                    fixed += 1
                else:
                    report.add(_entry(rule, p.para_id, t, "（待确认）",
                                      (p.coords or {}).get("pages")), auto=False)
    return fixed


def _case_variants(dirty: str, clean: str) -> list[tuple[str, str]]:
    """[局部] 大小写变体：(脏词形态, 对应替换形态)"""
    out = [(dirty, clean)]
    if dirty and dirty[0].isalpha():
        out.append((dirty[0].upper() + dirty[1:], clean[0].upper() + clean[1:]))
    if dirty.islower():
        out.append((dirty.upper(), clean.upper()))
    return out


def _apply_regex_rules(doc: Any, rules: list[dict], report: RuleReport,
                       include_heading: bool) -> int:
    """[局部] regex 类规则批量应用（自动/待确认门控）

    replacement 语义：pattern 含捕获组 → m.expand（支持 \\1 引用，如 spacing 拆词）；
    无捕获组 → 字面量替换（KNOWN_FIXES 迁移规则的反斜杠安全）。
    """
    fixed = 0
    for rule in _ordered_rules(rules):
        if rule.get("pattern_type") != "regex" or not rule.get("enabled", True):
            continue
        try:
            pat = re.compile(rule["pattern"])
        except re.error:
            continue
        repl = rule.get("replacement", "")
        exclude = set(w.lower() for w in (rule.get("exclude") or []))
        expand = pat.groups > 0

        def _repl(m: re.Match) -> str:
            if exclude and m.group(0).lower() in exclude:
                return m.group(0)          # 排除词（如 offspring）→ 不替换
            return m.expand(repl) if expand else repl

        for p in doc.paragraphs:
            if p.is_heading and not include_heading:
                continue
            t = p.text_en or ""
            new = pat.sub(_repl, t)
            if new != t:
                auto = bool(rule.get("auto_apply", False))
                if auto:
                    p.text_en = new
                    report.add(_entry(rule, p.para_id, t, new,
                                      (p.coords or {}).get("pages")), auto=True)
                    fixed += 1
                else:
                    # 低置信：不落笔，只记待确认清单（R05 AI 审查）
                    report.add(_entry(rule, p.para_id, t, "（待确认）",
                                      (p.coords or {}).get("pages")), auto=False)
    return fixed


def apply_spacing_local(doc: Any, pdf_path: str | Path, report: RuleReport) -> int:
    """[全局] 词中粘连候选检测（Layer 3 本地对照作证据，**不自动修复**）：
    "XofY" 型候选 → pending 条目（本地页文本有空格形式则附 evidence_hint）。

    词中粘连自动修复误伤风险高（maintain/other 等合法词含 stopword 子串），
    一律进待确认清单，由 R05 AI 审查 + 用户确认后转规则。
    """
    try:
        import pymupdf
    except ImportError:
        return 0
    try:
        pdf = pymupdf.open(str(pdf_path))
    except Exception:
        pdf = None                     # 本地不可用 → 仅检测（无本地证据）
    detected = 0
    try:
        for p in doc.paragraphs:
            if p.is_heading or not (p.text_en or ""):
                continue
            t = p.text_en
            candidates = sorted(find_mid_joins(t))
            if not candidates:
                continue
            pages = (p.coords or {}).get("pages") or []
            local = ""
            if pdf is not None:
                parts = []
                for pg in pages:
                    try:
                        parts.append(pdf[int(pg) - 1].get_text("text") or "")
                    except Exception:
                        pass
                local = " ".join(parts).lower()
            for w in candidates:
                hint = "本地无空格形式" if not local else (
                    "本地含空格形式" if any(
                        "%s %s " % (w[:w.find(sw)], sw)
                        for sw in _MID_STOP
                        if (idx := w.find(sw)) >= 2 and idx <= len(w) - len(sw) - 2)
                    else "本地无空格形式")
                report.add({"rule_id": "SPACING-mid-join", "category": "spacing",
                            "para_id": p.para_id, "page": pages[:3] or None,
                            "before": w, "after": "（待确认：拆词?）",
                            "confidence": 0.0, "source": "detect",
                            "evidence_hint": hint, "kind": "mid_join"},
                           auto=False)
                detected += 1
    finally:
        if pdf is not None:
            pdf.close()
    return detected


def _apply_metadata(doc: Any, rules: list[dict], report: RuleReport) -> int:
    """[局部] metadata 执行器：frontmatter 清洗 + metadata 类规则"""
    fixed = 0
    # ① 作者清洗："and X" 残留 → X；条目去尾标点
    if doc.metadata and doc.metadata.authors:
        cleaned = []
        changed = False
        for a in doc.metadata.authors:
            a = a.strip(" *")
            if a.lower().startswith("and "):
                a = a[4:].strip()
                changed = True
            if a:
                cleaned.append(a)
        if changed:
            doc.metadata.authors = cleaned
            report.add({"rule_id": "META-authors-and", "category": "metadata",
                        "para_id": "frontmatter", "page": 1,
                        "before": "authors 含 'and' 条目", "after": "已清理",
                        "confidence": 1.0, "source": "builtin"}, auto=True)
            fixed += 1
    # ② keywords 截断清理（R06：本地提取的 "laser-" 类以 - 结尾短项删除）
    if doc.metadata and doc.metadata.keywords:
        kept = [k for k in doc.metadata.keywords
                if not (str(k).endswith("-") and len(str(k)) < 12)]
        if len(kept) != len(doc.metadata.keywords):
            doc.metadata.keywords = kept
            report.add({"rule_id": "META-keywords-trunc", "category": "metadata",
                        "para_id": "frontmatter", "page": 1,
                        "before": "keywords 含截断项", "after": "已清理",
                        "confidence": 1.0, "source": "builtin"}, auto=True)
            fixed += 1
    # ③ metadata 类规则（regex 应用于段落文本——如机构/日期行清理）
    fixed += _apply_regex_rules(doc, rules, report, include_heading=True)
    return fixed


def _apply_latex(doc: Any, rules: list[dict], report: RuleReport,
                 pdf_path: str | Path) -> int:
    """[局部] latex 执行器：语法检测（异常→pending）+ latex 规则"""
    fixed = _apply_regex_rules(doc, rules, report, include_heading=False)
    # 语法检测（无法自动修复 → 待 AI 审查）
    try:
        from paperparse.core.latex_check import check_document_latex
        for err in check_document_latex(doc):
            report.add({"rule_id": "LATEX-syntax", "category": "latex",
                        "para_id": err.get("para_id"), "page": None,
                        "before": err.get("near", "")[:100],
                        "after": "（待 AI 审查）",
                        "confidence": 0.0, "source": "detect",
                        "kind": err.get("kind"), "detail": err.get("detail")},
                       auto=False)
    except Exception:
        pass
    return fixed


def clean_blocks_channeled(blocks: list, rules_dir: str | Path | None = None,
                           channel: str = "paddleocr") -> tuple[list, list]:
    """[全局] ★ P12 安全红线：挖掘规则（source=mining）**通道作用域**清洗。

    挖掘规则（双通道差异学习而来）只允许作用其**脏侧**通道的文本块，绝不直接作用
    最终文档（apply_rules 在双通道模式下 exclude_source={"mining"}）——防止规则
    把正确的识别结果改错。作用域规则：

      channel=="paddleocr"（辅助通道，可丢弃/低风险）：
          应用 clean_channel ∈ (None, "mineru") 的全部启用挖掘规则
          （direction 未知/缺失的保守规则也只作用 paddleocr 侧，永不触碰 mineru）；
      channel=="mineru"（主通道，高价值）：
          仅应用 clean_channel=="paddleocr"（脏侧=mineru）且 auto_apply=True 的规则
          （AI 定向 + conf≥0.9 + 方向感知验证）；其余保持原样进 review 待人工确认。

    返回: (changed_blocks, audit)
        audit 每项 {rule_id, block_id, page, before, after, channel}
    """
    from paperparse.core.rule_library import load_all_rules
    all_rules = load_all_rules(external=rules_dir)
    mining = [r for r in all_rules
              if (r.get("source") or "") == "mining" and r.get("enabled", True)]
    if not mining:
        return [], []
    applicable = []
    for r in mining:
        cc = r.get("clean_channel")
        if channel == "paddleocr":
            # 脏侧=paddleocr：clean_channel==mineru（明确）或 None（未知，保守）
            if cc in (None, "", "mineru"):
                applicable.append(r)
        else:  # mineru 主通道：仅 AI 定向 + auto（conf≥0.9 已验证）
            if cc == "paddleocr" and r.get("auto_apply"):
                applicable.append(r)
    if not applicable:
        return [], []
    import json as _json
    changed: list = []
    audit: list[dict] = []
    for b in blocks:
        if not (b.text or "").strip():
            continue
        before = b.text
        text = before
        for r in applicable:
            try:
                if r.get("pattern_type") == "regex":
                    text, n = re.subn(r["pattern"], r.get("replacement", ""), text)
                    if n:
                        audit.append({"rule_id": r.get("rule_id"), "block_id": b.block_id,
                                      "page": b.page, "before": before, "after": text,
                                      "channel": channel, "kind": "regex"})
                elif r.get("pattern_type") == "dict":
                    mapping = _json.loads(r.get("pattern") or "{}")
                    for k, v in mapping.items():
                        pat = re.compile(r"(?<![A-Za-z])" + re.escape(k) + r"(?![A-Za-z])")
                        text, n = pat.subn(v, text)
                        if n:
                            audit.append({"rule_id": r.get("rule_id"),
                                          "block_id": b.block_id, "page": b.page,
                                          "before": before, "after": text,
                                          "channel": channel, "kind": "dict"})
            except (re.error, ValueError, TypeError):
                continue  # 单条规则异常不影响其余
        if text != before:
            b.text = text
            changed.append(b)
    return changed, audit


def apply_rules(doc: Any, rules_dir: str | Path | None = None,
                pdf_path: str | Path | None = None,
                only_categories: tuple[str, ...] | None = None,
                exclude_source: set[str] | None = None) -> RuleReport:
    """[全局] 规则引擎主入口：按类别顺序应用规则库 + 本地对照 + 元数据清洗

    参数:
        doc: ArticleDocument（原地修改 paragraphs 与 metadata）
        rules_dir: 外部规则根（**始终合并内嵌 builtin**——R09 修复：应用后端传
                   RULES_DIR 时内置规则不得丢失；传 None → 默认外部根）
        pdf_path: 源 PDF（spacing 本地对照用；None 则跳过本地对照）
        only_categories: 限定类别（测试用）
        exclude_source: **P12 安全红线**：排除指定 source 的规则（双通道模式下
                        exclude={"mining"}——挖掘清洗规则只允许作用脏侧通道块
                        （clean_blocks_channeled），绝不直接作用最终文档）
    返回:
        RuleReport（applied/pending/stats）
    """
    from paperparse.core.rule_library import load_all_rules
    all_rules = load_all_rules(external=rules_dir)   # 内嵌 builtin + 外部 learned/user
    if exclude_source:
        all_rules = [r for r in all_rules
                     if (r.get("source") or "") not in exclude_source]
    report = RuleReport()
    cats = only_categories or CATEGORY_ORDER

    if "latex" in cats:
        _apply_latex(doc, [r for r in all_rules if r.get("category") == "latex"],
                     report, pdf_path)
    if "formula" in cats:
        _apply_regex_rules(doc, [r for r in all_rules if r.get("category") == "formula"],
                           report, include_heading=True)
    if "variable" in cats:
        _apply_regex_rules(doc, [r for r in all_rules if r.get("category") == "variable"],
                           report, include_heading=True)
    if "spacing" in cats:
        _apply_regex_rules(doc, [r for r in all_rules if r.get("category") == "spacing"],
                           report, include_heading=True)
        # P11 双通道挖掘的词级 OCR 纠错规则（dict 型，如 {"diferent": "different"}）
        _apply_dict_rules(doc, [r for r in all_rules if r.get("category") == "spacing"],
                          report, include_heading=True)
        # mid-join 候选检测（词中粘连）默认关闭——无词典自动判误伤风险高（maintain/
        # responsive 等合法词），由 R05 AI 审查按需启用（apply_spacing_local 保留）
    if "metadata" in cats:
        _apply_metadata(doc, [r for r in all_rules if r.get("category") == "metadata"],
                        report)
    if "layout" in cats:
        _apply_regex_rules(doc, [r for r in all_rules if r.get("category") == "layout"],
                           report, include_heading=True)
    return report


def save_report(report: RuleReport, out_path: str | Path) -> Path:
    """[全局] 报告落盘（intermediate/rule_report.json）"""
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=1),
                 encoding="utf-8")
    return p
