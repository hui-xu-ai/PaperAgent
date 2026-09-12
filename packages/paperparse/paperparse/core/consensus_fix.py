#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
文件: paperparse/core/consensus_fix.py
功能: 领域词典校正层（P16 共识错误修复——双通道同错，diff 零报告，需领域知识兜底）

背景:
  双通道仲裁（百度 OCR vs MinerU）是"分歧检测器"：对化学式/单位/特殊符号两通道
  **同错**（如 BF₄⁻ → 双通道都输出 "BF−"，漏下标 4）时 diff 判 equal 零报告。
  本模块引入独立于两 OCR 的**第三信号：领域词典**（rules/builtin/domain.json，
  生成器 tools/build_domain_dict.py 用 pyvalem/pint/periodictable/PubChem 产出，
  运行时零外部依赖）。

设计（2026-08-26 实测验证）:
  - domain.json 只存规范式+元素计数+charge（非畸形正则）
  - 畸形判定 = 运行时归一化比对 + 缺失检测（Layer A 词典 + Layer B 结构校验合一）：
      候选 token 解析元素序列+电荷 → 词典命中（元素集合一致 & charge 一致）→
      缺数字下标 = 畸形 → 校正为词典规范形态
  - 安全红线（P12 传承）：
      · 裸元素名（无显式电荷）绝不改（"BF" 可能是血流，非 BF₄⁻）
      · 带显式电荷 + 词典唯一命中 + 缺下标 → conf 0.95 自动落地 + audit
      · 其余候选 → 不动（记录为 review 候选）
      · 专名保护（Nafion/PEDOT:PSS/PI…——PI 会被 pyvalem 解析成 P+I 假式）
  - 形态分层提取（实测：裸文本正则不可靠）：
      LaTeX 公式块（$..$，\mathrm{XX}_{n}^{±}） / HTML 标签（<sub>/<sup>） / 纯文本
  - 前置码点归一：U+2212(−)→-、全角→半角、Unicode 上下标→可解析形态

对外接口: apply_domain_fix / load_domain_dict / _canonical_form（测试用）
"""
from __future__ import annotations

import json
import re
from pathlib import Path

__all__ = ["apply_domain_fix", "load_domain_dict"]

# ---------------------------------------------------------------------------
# 常量与数据加载
# ---------------------------------------------------------------------------

# Unicode 下标/上标数字映射
_SUB_DIGITS = {c: str(i) for i, c in enumerate("₀₁₂₃₄₅₆₇₈₉")}
_SUP_DIGITS = {c: str(i) for i, c in enumerate("⁰¹²³⁴⁵⁶⁷⁸⁹")}
_SUP_CHARGE = {"⁻": -1, "⁺": 1, "²⁻": -2, "²⁺": 2, "³⁻": -3, "³⁺": 3}
_CHARGE_SUP = {-1: "⁻", 1: "⁺", -2: "²⁻", 2: "²⁺", -3: "³⁻", 3: "³⁺"}

# 码点归一（前置；OCR 常见 Unicode 变体 → ASCII 可解析形态）
_CP_NORM = [
    ("\u2212", "-"), ("\u2213", "-"), ("\u2013", "-"), ("\u2014", "-"),
    ("\u00a0", " "), ("\u2009", " "), ("\u200a", " "), ("\u200b", ""),
    ("\u00b7", "*"), ("\u2022", "*"),
    ("℃", "°C"), ("µ", "μ"),
]
for _s, _d in zip("０１２３４５６７８９", "0123456789"):
    _CP_NORM.append((_s, _d))

# 希腊字母（LaTeX 命令）→ ASCII（供 \Nu→N 类命令反查；不覆盖化学式内联希腊）
_GREEK_LATEX = {"\\alpha": "a", "\\beta": "b", "\\gamma": "g", "\\delta": "d",
                "\\epsilon": "e", "\\varepsilon": "e", "\\zeta": "z",
                "\\eta": "e", "\\theta": "t", "\\iota": "i", "\\kappa": "k",
                "\\lambda": "l", "\\mu": "u", "\\nu": "v", "\\xi": "x",
                "\\pi": "p", "\\rho": "r", "\\sigma": "s", "\\tau": "t",
                "\\upsilon": "y", "\\phi": "f", "\\varphi": "f", "\\chi": "c",
                "\\psi": "y", "\\omega": "w",
                "\\Nu": "N", "\\Pi": "P", "\\Sigma": "S", "\\Delta": "D",
                "\\Omega": "O", "\\Lambda": "L", "\\Gamma": "G", "\\Phi": "F"}

_DOMAIN_JSON = "domain.json"


def _domain_paths() -> list[Path]:
    """内置（包内）+ 外部 learned（学习闭环追加）路径"""
    from paperparse.config import asset_root
    paths = [Path(asset_root()) / "rules" / "builtin" / _DOMAIN_JSON]
    # 外部 learned 层（rule_library.default_rules_dir 同源逻辑）
    env = __import__("os").getenv("RULES_DIR", "").strip()
    ext = None
    if env:
        ext = Path(env) / "learned" / _DOMAIN_JSON
    else:
        cwd = Path.cwd() / "rules" / "learned" / _DOMAIN_JSON
        ext = cwd if cwd.exists() else None
        if ext is None:
            for parent in Path.cwd().parents:
                cand = parent / "rules" / "learned" / _DOMAIN_JSON
                if cand.exists():
                    ext = cand
                    break
    if ext and ext.exists():
        paths.append(ext)
    return paths


_dict_cache: dict | None = None


def load_domain_dict() -> dict:
    """加载领域词典（内置 + learned 合并；缓存）。条目合并策略：learned 覆盖同 formula"""
    global _dict_cache
    if _dict_cache is not None:
        return _dict_cache
    merged: dict = {"schema_version": 1, "elements": [], "chemistry": [],
                    "variable_formulas": [], "units": [], "protected_names": []}
    for p in _domain_paths():
        if not p.exists():
            continue
        data = json.loads(p.read_text(encoding="utf-8"))
        for k in ("elements", "chemistry", "variable_formulas", "units",
                  "protected_names"):
            merged.setdefault(k, []).extend(data.get(k, []))
    # 去重（learned 覆盖 builtin：同 formula 后出现者胜）
    by_formula: dict[str, dict] = {}
    for e in merged["chemistry"]:
        by_formula[e.get("formula", "")] = e
    merged["chemistry"] = list(by_formula.values())
    # 元素集合 + 索引
    merged["_elements_set"] = set(merged.get("elements", []))
    merged["_chem_index"] = _build_index(merged["chemistry"])
    merged["_units_index"] = _build_unit_index(merged.get("units", []))
    merged["_protected_re"] = _build_protected_re(merged.get("protected_names", []))
    _dict_cache = merged
    return merged


def _build_index(chem: list[dict]) -> dict:
    """索引：frozenset(元素) → 条目列表（供候选命中查找）"""
    idx: dict[frozenset, list[dict]] = {}
    for e in chem:
        if not e.get("enabled", True):
            continue
        key = frozenset((e.get("elements") or {}).keys())
        idx.setdefault(key, []).append(e)
    return idx


def _build_unit_index(units: list[dict]) -> dict[str, dict]:
    idx: dict[str, dict] = {}
    for u in units:
        if not u.get("enabled", True):
            continue
        for s in [u.get("symbol", ""), u.get("ascii", "")] + list(u.get("aliases", [])):
            if s:
                idx[s] = u
    return idx


def _build_protected_re(names: list) -> re.Pattern | None:
    """专名保护：词边界正则（P.P / P-LIG 等含标点，按字面+边界）"""
    if not names:
        return None
    names = sorted(set(names), key=len, reverse=True)
    esc = [re.escape(n) for n in names]
    return re.compile(r"(?<![A-Za-z0-9])(" + "|".join(esc) + r")(?![A-Za-z0-9])")


# ---------------------------------------------------------------------------
# 元素解析（运行时零依赖；元素表来自 domain.json）
# ---------------------------------------------------------------------------

def _norm_text(t: str) -> str:
    """前置码点归一：Unicode 变体 → ASCII 可解析形态"""
    for src, dst in _CP_NORM:
        t = t.replace(src, dst)
    return t


def _parse_elements(seq: str, elem_set: set) -> tuple[dict | None, str | None]:
    """元素序列 → 元素计数（贪婪最长优先；未知符号 → None + 首个未知符号）"""
    counts: dict[str, int] = {}
    i = 0
    n = len(seq)
    while i < n:
        two = seq[i:i + 2]
        one = seq[i]
        if len(two) == 2 and two in elem_set:
            sym = two
            i += 2
        elif one in elem_set:
            sym = one
            i += 1
        else:
            return None, one
        counts[sym] = counts.get(sym, 0) + 1
    return counts, None


def _canonical_form(token: str, elem_set: set) -> dict | None:
    """候选 token → 规范形态 {elements, charge, has_charge, subscript_ok}
    处理形态：纯文本（BF4- / BF₄⁻ / BF−）、HTML 剥标签后、LaTeX 剥命令后。
    token 需已归一化（- 电荷、数字下标可解析）。"""
    t = _norm_text(token)
    t = re.sub(r"</?sub>|</?sup>", "", t)
    t = re.sub(r"\\mathrm|\\text|\\mathit", "", t)
    t = t.replace("{", "").replace("}", "").replace("$", "").replace("\\", "")
    t = t.strip()
    if not t:
        return None
    # 提取电荷（尾部 ± 序列；含 Unicode 上标电荷/数字）
    # 注意：字符类内 - 必须转义/置尾，否则 [+-−] 会形成范围（+..− 含 'O' 等字母）
    charge = 0
    has_charge = False
    m = re.search(r"([+\-\u2212\u207b\u207a]|(?:²|³)?[⁻⁺])$", t)
    if m:
        chg_s = m.group(1)
        if chg_s in _SUP_CHARGE:
            charge = _SUP_CHARGE[chg_s]
        else:
            charge = -1 if chg_s in "-−" else 1
        has_charge = True
        t = t[:m.start()].rstrip()
    # 下标数字：Unicode 下标/普通数字 → 附着到前一个元素
    # 形态1: 元素后直接下标（BF4 / B F 4 / BF₄）
    # 先按"元素序列 + 数字下标"解析
    m = re.match(r"^([A-Za-z]+)(.*)$", t)
    if not m:
        return None
    seq, tail = m.group(1), m.group(2)
    elements, bad = _parse_elements(seq, elem_set)
    if elements is None:
        return None
    # tail 必须是纯数字（下标）或空；数字分配给**最后一个元素**（化学惯例：
    # 数字紧跟某元素后=该元素下标，如 BF4 → F:4、SO4 → O:4）
    if tail:
        digits = []
        for c in tail:
            if c in _SUB_DIGITS:
                digits.append(_SUB_DIGITS[c])
            elif c.isdigit():
                digits.append(c)
            else:
                return None
        if digits:
            elements[list(elements)[-1]] = int("".join(digits))
    return {"elements": elements, "charge": charge, "has_charge": has_charge}


def _latex_block_scan(block: str, elem_set: set) -> list[dict]:
    r"""LaTeX 公式块内扫描 \mathrm{XX}_{n}^{±} 候选 → 规范形态列表"""
    out = []
    pat = re.compile(
        r"\\mathrm\s*\{([A-Za-z]+)\}(?:_\{(\d+)\})?"
        r"(?:\^\{[0-9]*[-+]?\})?")
    for m in pat.finditer(block):
        seq = m.group(1)
        elements, bad = _parse_elements(seq, elem_set)
        if elements is None:
            continue
        sub = m.group(2)
        if sub:
            elements[list(elements)[-1]] = int(sub)
        charge = 0
        has_charge = False
        chg_m = re.search(r"\^\{(\d*)(-|\+)?\}$", m.group(0))
        if chg_m:
            num = int(chg_m.group(1) or "1")
            sign = chg_m.group(2) or "+"
            charge = -num if sign == "-" else num
            has_charge = True
        out.append({"elements": elements, "charge": charge, "has_charge": has_charge,
                    "start": m.start(), "end": m.end(), "latex": m.group(0)})
    return out


# ---------------------------------------------------------------------------
# 单位
# ---------------------------------------------------------------------------

_UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([A-Za-zμΩ°℃²³⁻¹⁰-]+)")


def _fix_units(text: str, units_index: dict) -> tuple[str, list[dict]]:
    """单位符号规范化：℃→°C 等（aliases → symbol）。仅在数值后触发。"""
    fixes = []
    changed = False

    def _rep(m: re.Match) -> str:
        nonlocal changed
        num, unit = m.group(1), m.group(2)
        entry = units_index.get(unit)
        if entry and entry["symbol"] != unit:
            fixed = num + entry["symbol"]
            if fixed != m.group(0):
                fixes.append({"rule": entry.get("id", "DOM-UNIT"),
                              "before": m.group(0), "after": fixed,
                              "confidence": entry.get("confidence", 0.95),
                              "context": "unit"})
                changed = True
                return fixed
        return m.group(0)

    return _UNIT_RE.sub(_rep, text), fixes


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def _record_or_apply(text: str, raw: str, fx: dict | None, *, context: str,
                     fixes: list[dict]) -> tuple[str, list[dict]]:
    """落地/记录分流：conf>=0.95 且带 replacement → 替换文本并记录；
    review 候选（charge 残缺）→ 只记录（不落地）；其余忽略。"""
    if fx is None:
        return text, fixes
    if fx.get("review"):
        fixes.append({**fx, "before": raw, "after": "", "context": context})
        return text, fixes
    if fx.get("confidence", 0) >= 0.95 and fx.get("replacement"):
        text = text.replace(raw, fx["replacement"], 1)
        fixes.append({**fx, "before": raw, "after": fx["replacement"],
                      "context": context})
    return text, fixes


def apply_domain_fix(text: str, *, kind: str = "body") -> tuple[str, list[dict]]:
    """领域词典校正：畸形化学式（带电荷缺下标）→ 规范形态；单位规范化。

    返回 (修复后文本, fixes)；fixes 每项:
      {rule, before, after, confidence, context}
    conf >= 0.95 → 自动落地；< 0.95 → 记录供复核（本函数只做记录标记）。
    """
    d = load_domain_dict()
    elem_set = d["_elements_set"]
    fixes: list[dict] = []
    if not text:
        return text, fixes

    # 0) 专名保护掩码（Nafion/PEDOT:PSS/PI…，防被当化学式）
    masks: list[tuple[str, str]] = []
    if d["_protected_re"]:
        def _mask(m: re.Match) -> str:
            tok = "\uE000%d\uE001" % len(masks)
            masks.append((tok, m.group(0)))
            return tok
        text = d["_protected_re"].sub(_mask, text)

    # 1) 单位规范化
    text, unit_fixes = _fix_units(text, d["_units_index"])
    fixes.extend(unit_fixes)

    # 2) LaTeX 公式块内候选
    latex_fixes: list[dict] = []

    def _scan_latex(m: re.Match) -> str:
        block = m.group(0)
        for cand in _latex_block_scan(block, elem_set):
            fx = _judge(cand, d, source="latex")
            block, _f = _record_or_apply(block, cand["latex"], fx,
                                         context="latex", fixes=latex_fixes)
        return block

    text = re.sub(r"\$\$[^$]*\$\$|\$[^$\n]*\$", _scan_latex, text)
    fixes.extend(latex_fixes)

    # 3) 纯文本候选（含 HTML 形态：先剥 sup/sub 标签记录）
    html_fixes: list[dict] = []
    # 提取 HTML 公式形态（BF<sub>4</sub><sup>−</sup> / BF<sup>−</sup>）：
    # 元素序列可跨 <sub>/<sup> 标签交替（"BF<sup>−</sup>" 的 F 后直接是标签）
    html_pat = re.compile(
        r"((?:[A-Z][a-z]?|<sub>\d+</sub>|<sup>[−⁻⁺+0-9²³]+</sup>)+)")
    for m in list(html_pat.finditer(text)):
        raw = m.group(0)
        if "<" not in raw:
            continue                            # 纯文本由 plain_pat 处理
        cand = _canonical_form(raw, elem_set)
        if not cand:
            continue
        fx = _judge(cand, d, source="html")
        text, _f = _record_or_apply(text, raw, fx, context="html",
                                    fixes=html_fixes)
    fixes.extend(html_fixes)

    # 纯文本：元素+可选数字下标+可选电荷（电荷可双字符：²⁻/³⁺）
    plain_pat = re.compile(
        r"(?<![A-Za-z0-9])([A-Z][a-z]?(?:[A-Z][a-z]?)*)"
        r"([₀-₉0-9]*)((?:²|³)?[−⁻⁺+]?)(?![A-Za-z0-9])")
    for m in list(plain_pat.finditer(text)):
        raw = m.group(0)
        cand = _canonical_form(raw, elem_set)
        if not cand:
            continue
        fx = _judge(cand, d, source="plain")
        text, _f = _record_or_apply(text, raw, fx, context="plain", fixes=fixes)

    # 4) 还原专名掩码
    for tok, orig in masks:
        text = text.replace(tok, orig)

    return text, fixes


def _judge(cand: dict, d: dict, *, source: str) -> dict | None:
    """畸形判定：词典命中 + 缺下标 → conf 分级
    - 元素集合不命中词典 → None（不动作）
    - 无显式电荷 → None（裸元素名绝不改）
    - 命中 & 带电荷 & charge 匹配 & 缺数字下标 → conf 0.95 自动
    - 命中 & 带电荷 & charge 不匹配（电荷残缺，如 SO− 实为 SO₄²⁻）→ conf 0.8
      复核候选（不落地，review=True 由调用方处理）
    - 命中 & 下标完整但形态不同 → None（渲染已 OK，不动）
    """
    elements = cand["elements"]
    key = frozenset(elements.keys())
    entries = d["_chem_index"].get(key)
    if not entries:
        return None
    if not cand["has_charge"]:
        return None
    matched = [e for e in entries if e.get("charge") == cand["charge"]]
    if not matched:
        # charge 不匹配但元素命中：可能电荷残缺（SO− vs SO₄²⁻）→ 复核候选
        return {"rule": entries[0].get("id", ""), "confidence": 0.8,
                "replacement": "", "missing": {}, "review": True,
                "note": "charge mismatch, candidate %s vs dict %s"
                        % (cand["charge"], entries[0].get("charge"))}
    entry = matched[0]
    # 缺下标判定：候选某元素计数 < 词典计数
    missing = {sym: (entry.get("elements") or {}).get(sym, 0) - n
               for sym, n in elements.items()
               if (entry.get("elements") or {}).get(sym, 0) > n}
    if not missing:
        return None
    # 生成规范校正形态（替换目标）：公式内 → 纯 LaTeX（块本身已有 $，避免 $$ 嵌套）
    if source == "latex":
        replacement = entry.get("latex", "")
    else:
        replacement = entry.get("unicode", "")
    return {"rule": entry.get("id", ""), "confidence": 0.95,
            "replacement": replacement, "missing": missing}


def _latex_from_entry(entry: dict) -> str:
    r"""条目 → LaTeX 规范形态（\mathrm{XX}_{n}^{±}）"""
    latex = entry.get("latex", "")
    if latex:
        return "$" + latex + "$"
    return entry.get("unicode", "")


def find_boundary_candidates(text: str, *, max_per_text: int = 8) -> list[dict]:
    """引擎判定**边界外**的带电荷化学候选（C 层 AI 共识扫描样本源）：
    元素解析成功 + 显式电荷，但**词典未命中**（_judge None）——确定性词典管不了，
    交给 AI 发现新共识错误（学习闭环输入）。输出 [{token, elements, charge, context}]。"""
    d = load_domain_dict()
    elem_set = d["_elements_set"]
    out: list[dict] = []
    seen: set = set()
    plain_pat = re.compile(
        r"(?<![A-Za-z0-9])([A-Z][a-z]?(?:[A-Z][a-z]?)*)"
        r"([₀-₉0-9]*)((?:²|³)?[−⁻⁺+]?)(?![A-Za-z0-9])")
    for m in plain_pat.finditer(text):
        raw = m.group(0)
        cand = _canonical_form(raw, elem_set)
        if not cand or not cand["has_charge"]:
            continue
        key = frozenset(cand["elements"].keys())
        if key in d["_chem_index"]:
            continue                        # 词典已覆盖该元素组合（含完整形态/缺下标
            # 判定）→ 确定性层管，不送 AI
        seen_key = (raw, tuple(sorted(cand["elements"].items())), cand["charge"])
        if seen_key in seen:
            continue
        seen.add(seen_key)
        ctx = text[max(0, m.start() - 40):m.end() + 40]
        out.append({"token": raw, "elements": cand["elements"],
                    "charge": cand["charge"], "context": ctx})
        if len(out) >= max_per_text:
            break
    return out


def reset_cache():
    """测试用：清空词典缓存"""
    global _dict_cache
    _dict_cache = None
