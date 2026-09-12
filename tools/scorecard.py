#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scorecard：语料评分卡（P-ENHANCE R01）—— 6 类别指标，验收与防回归基准。

类别：
  绝对量（无需 gold）：latex_syntax / garbled / spacing（启发式）
  差异量（需 gold）：  formula / layout / metadata

用法:
    .venv\\Scripts\\python.exe tools/scorecard.py --candidate corpus/baseline/<名>/paper.md [--gold corpus/gold/<名>.md] [--doc corpus/baseline/<名>/intermediate/document.json] [--report corpus/reports/<名>.json]
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ---------------- 常量与正则 ----------------

_MATH_RE = re.compile(r"\$\$(.+?)\$\$|\$([^$\n]+)\$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_IMG_LINE_RE = re.compile(r"^!\[[^\]]*\]\([^)]*\)\s*$")
_FRONT_RE = re.compile(r"^---\s*$")

# 空格缺失启发式（HEURISTIC，R04 规则引擎将细化）：
#   仅保留高精度强信号：词首 "of"+长词粘连（ofconductivity / ofmultimodal 等，MinerU 典型错误）。
#   词中粘连（XofY / XandY）误报过高（responsive/actuators 等合法词内含 stopword），
#   交由 R04 规则引擎 + 本地交叉验证（pymupdf 有空格对照）处理，不作为 scorecard 指标。
_LEAD_OF_RE = re.compile(r"^of[a-z]{7,}$")
_OF_EXCLUDE = frozenset("""
often offset office official officer officials offering offerings offered
offer offers offspring offsprings
""".split())

_FM_KEYS = ("title", "authors", "doi", "year", "keywords")


def strip_frontmatter(text: str) -> tuple[str, dict]:
    """[全局] 剥离 YAML frontmatter（--- 包围），返回 (正文, frontmatter 键值)"""
    lines = text.splitlines()
    fm: dict = {}
    if len(lines) >= 2 and _FRONT_RE.match(lines[0].strip()):
        end = None
        for i in range(1, len(lines)):
            if _FRONT_RE.match(lines[i].strip()):
                end = i
                break
        if end:
            for ln in lines[1:end]:
                if ":" in ln:
                    k, _, v = ln.partition(":")
                    k = k.strip().lower()
                    v = v.strip().strip("\"'")
                    if k in _FM_KEYS and v:
                        fm[k] = v
            return "\n".join(lines[end + 1:]), fm
    return text, fm


def _clean(text: str) -> str:
    """[局部] 指标用归一化：连字/空白"""
    return re.sub(r"\s+", " ", text).strip()


# ---------------- 绝对量指标 ----------------

def check_latex_syntax(text: str) -> list[dict]:
    """[局部] LaTeX 语法检测（优先引擎 latex_check；失败回退自带 $ 配对粗检）"""
    body, _ = strip_frontmatter(text)
    body = "\n".join(ln for ln in body.splitlines() if not _IMG_LINE_RE.match(ln.strip()))
    if "$" not in body and "\\" not in body:
        return []
    try:
        from paperparse.core.latex_check import check_latex_syntax as _engine_check
        return _engine_check(body)
    except Exception:
        pass
    # 回退：$ 配对粗检
    errs: list[dict] = []
    for ln in body.splitlines():
        if not ln.strip():
            continue
        dollars = [c for c in ln if c == "$"]
        if len(dollars) % 2 == 1:
            errs.append({"kind": "unclosed_math", "detail": "美元符未配对",
                         "near": ln.strip()[:120]})
    return errs[:50]


def count_garbled(text: str) -> int:
    """[局部] 正文 U+FFFD 替换字符计数"""
    body, _ = strip_frontmatter(text)
    return body.count("\ufffd")


def detect_spacing(text: str) -> list[str]:
    """[局部] 空格缺失启发式命中词列表：词首 "of"+长词粘连（高精度，可人工复核）"""
    body, _ = strip_frontmatter(text)
    hits = [w for w in re.findall(r"[A-Za-z]{7,}", body)
            if _LEAD_OF_RE.match(w.lower()) and w.lower() not in _OF_EXCLUDE]
    return hits


# ---------------- 差异量指标 ----------------

def math_segments(text: str) -> Counter:
    """[局部] 归一化公式段计数（$..$/$$..$$）"""
    body, _ = strip_frontmatter(text)
    segs = Counter()
    for m in _MATH_RE.finditer(body):
        raw = m.group(1) or m.group(2) or ""
        segs[re.sub(r"\s+", "", raw)] += 1
    return segs


def heading_seq(text: str) -> list[str]:
    """[局部] 标题序列（正文 `#` 行文本）"""
    body, _ = strip_frontmatter(text)
    out = []
    for ln in body.splitlines():
        m = _HEADING_RE.match(ln.strip())
        if m:
            out.append(_clean(m.group(2)))
    return out


def _norm_title(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


# ---------------- R10 结构指标 ----------------

# 出版信息残留（Elsevier 页眉/首页头部/时间线）
_PUB_NOISE_RE = [
    re.compile(r"contents lists available at", re.IGNORECASE),
    re.compile(r"^journal homepage\s*[:：]", re.IGNORECASE),
    re.compile(r"^available online\b", re.IGNORECASE),
    re.compile(r"^\d{2,4}\s*\(\d{4}\)\s+\d{3,6}$"),        # 卷 (年) 页码
]
_FRONT_HEADING_KEYS = ("abstract", "article info", "keywords")
_NUM_HEADING_RE2 = re.compile(r"^\d")


def detect_structure(md_text: str) -> dict:
    """[全局] 结构指标（R10 防回归）：
    - images: md 中图片引用行数（渲染图数）
    - abstract_position_ok: Abstract 类标题在首个编号标题（1. Introduction）前
    - publisher_noise: 出版信息残留行数
    - suspicious_headings: 小写开头的 heading（疑似正文句误判标题）
    """
    body, _ = strip_frontmatter(md_text)
    lines = body.splitlines()
    images = sum(1 for ln in lines if _IMG_LINE_RE.match(ln.strip()))
    heads = []
    for ln in lines:
        m = _HEADING_RE.match(ln.strip())
        if m:
            heads.append(_clean(m.group(2)))
    # ABSTRACT 位置：front heading 中最先出现者 vs 首个编号标题
    first_num = next((i for i, h in enumerate(heads) if _NUM_HEADING_RE2.match(h)), None)
    first_front = next(
        (i for i, h in enumerate(heads)
         if h.lower().startswith(_FRONT_HEADING_KEYS)
         or _norm_title(h)[:8] in ("abstract", "keyword", "article")), None)
    abstract_ok = (first_front is None) or (first_num is None) or (first_front < first_num)
    # 出版信息残留（正文非 heading 行）
    noise = 0
    for ln in lines:
        t = ln.strip()
        if not t or t.startswith("#") or t.startswith("!"):
            continue
        if any(p.search(t) for p in _PUB_NOISE_RE):
            noise += 1
    # 疑似误判标题：小写开头 heading（正文句）
    suspicious = [h for h in heads
                  if re.match(r"^[a-z]", h) and not _NUM_HEADING_RE2.match(h)]
    return {"images": images, "abstract_position_ok": abstract_ok,
            "publisher_noise": noise, "suspicious_headings": suspicious[:10]}


def compare_metadata(gold: dict, cand: dict) -> dict:
    """[局部] 元数据对比：title（归一化）/doi（相等）"""
    out = {"gold": gold, "cand": cand}
    for key in ("title", "doi", "authors"):
        g = gold.get(key, "")
        c = cand.get(key, "")
        if key == "title":
            out["%s_match" % key] = bool(g and c and _norm_title(g) == _norm_title(c))
        elif key == "authors":
            out["%s_match" % key] = bool(g and c and _norm_title(g) == _norm_title(c))
        else:
            out["%s_match" % key] = bool(g and c and g.strip().lower() == c.strip().lower())
    out["title_gold"], out["title_cand"] = gold.get("title", ""), cand.get("title", "")
    out["doi_gold"], out["doi_cand"] = gold.get("doi", ""), cand.get("doi", "")
    return out


# ---------------- 主评分 ----------------

def score(gold_md: str | None, cand_md: str,
          cand_doc: dict | None = None) -> dict:
    """[全局] 6 类别评分

    参数:
        gold_md: 黄金标准 md 文本（None=只出绝对量）
        cand_md: 候选 md 文本（引擎输出 paper.md）
        cand_doc: 候选 document.json（dict，可选；LaTeX 语法用段落级更准）
    返回:
        {"categories": {...}, "score": {...}}
    """
    # 绝对量
    latex_errs = check_latex_syntax(cand_md)
    if cand_doc and latex_errs:
        try:
            from paperparse.core.latex_check import check_document_latex
            from paperparse.middleware.schema import ArticleDocument
            doc = ArticleDocument.model_validate(cand_doc)
            latex_errs = check_document_latex(doc) or latex_errs
        except Exception:
            pass
    garbled = count_garbled(cand_md)
    spacing = detect_spacing(cand_md)

    # 差异量（无 gold 时短路为"未比较"）
    if gold_md:
        formula_gold = math_segments(gold_md)
        formula_cand = math_segments(cand_md)
        fm_mismatch = sum((formula_gold - formula_cand).values()) + \
            sum((formula_cand - formula_gold).values())
        gold_head = heading_seq(gold_md)
        cand_head = heading_seq(cand_md)
        sm = difflib.SequenceMatcher(None, gold_head, cand_head)
        layout_diff = sum(max(i2 - i1, j2 - j1)
                          for op, i1, i2, j1, j2 in sm.get_opcodes()
                          if op in ("replace", "insert", "delete"))
    else:
        formula_gold, formula_cand = Counter(), math_segments(cand_md)
        fm_mismatch, gold_head, cand_head, layout_diff = 0, [], heading_seq(cand_md), 0

    _, gold_fm = strip_frontmatter(gold_md) if gold_md else (None, {})
    _, cand_fm = strip_frontmatter(cand_md)
    meta = compare_metadata(gold_fm, cand_fm) if gold_md else {
        "cand": cand_fm, "title_match": None, "doi_match": None,
        "authors_match": None}

    # R10 结构指标（无 gold 绝对量）：图片数 / ABSTRACT 位置 / 出版信息干扰 / 疑似误判标题
    struct = detect_structure(cand_md)

    categories = {
        "latex_syntax": {"errors": len(latex_errs), "details": latex_errs[:20]},
        "garbled": {"u_fffd": garbled},
        "spacing": {"hits": len(spacing), "words": spacing[:50],
                    "heuristic": True},
        "formula": {"gold_count": len(formula_gold), "cand_count": len(formula_cand),
                    "mismatch": fm_mismatch},
        "layout": {"gold_headings": len(gold_head), "cand_headings": len(cand_head),
                   "diff_ops": layout_diff,
                   "gold_seq": gold_head, "cand_seq": cand_head},
        "metadata": meta,
        "structure": struct,
    }
    abs_err = (len(latex_errs) + garbled + len(spacing)
               + int(not struct["abstract_position_ok"]) + struct["publisher_noise"]
               + len(struct["suspicious_headings"]))
    diff_err = fm_mismatch + layout_diff + (0 if meta.get("title_match") is None
                                            else int(not meta["title_match"]))
    return {"categories": categories,
            "score": {"abs_errors": abs_err, "diff_errors": diff_err}}


def score_files(gold_path: str | Path | None, cand_path: str | Path,
                doc_path: str | Path | None = None) -> dict:
    """[全局] 文件级评分"""
    cand = Path(cand_path).read_text(encoding="utf-8", errors="replace")
    gold = Path(gold_path).read_text(encoding="utf-8", errors="replace") if gold_path else None
    doc = None
    if doc_path and Path(doc_path).exists():
        try:
            doc = json.loads(Path(doc_path).read_text(encoding="utf-8"))
        except Exception:
            doc = None
    return score(gold, cand, cand_doc=doc)


def main() -> int:
    ap = argparse.ArgumentParser(description="scorecard：6 类别语料评分")
    ap.add_argument("--candidate", required=True, help="候选 md（引擎输出 paper.md）")
    ap.add_argument("--gold", help="黄金标准 md（可选；缺省只出绝对量）")
    ap.add_argument("--doc", help="候选 document.json（可选，语法检测更准）")
    ap.add_argument("--report", help="报告落盘路径（默认打印）")
    args = ap.parse_args()

    result = score_files(args.gold, args.candidate, args.doc)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.report:
        p = Path(args.report)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
