#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/build_domain_dict.py — 生成领域词典 rules/builtin/domain.json
（化学式/单位/专名；规范式+元素计数+charge，非畸形正则——畸形判定在运行时引擎）

用法: python tools/build_domain_dict.py [--pubchem] [--out <path>] [--seed <json>]
输入: work/scratch/dict-research/seed_base.json（人工基础清单：常见离子/化合物/单位）
流程:
  1. pyvalem 解析校验离子/化合物 → 规范式（atom_stoich/charge/latex/html 现成）
  2. periodictable 遍历 → 118 元素符号表（生成期固化，运行时零依赖）
  3. pint 校验单位清单 → Unicode 规范符号 + aliases
  4. --pubchem: PubChem PUG REST 名称→分子式 交叉验证（限速 0.3s；Hill 序需
     归一为 pyvalem 可解析形态，只做元素计数比对，不直接采用输出）
  5. 输出 domain.json（schema_version=1）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SEED = ROOT / "work" / "scratch" / "dict-research" / "seed_base.json"
DEFAULT_OUT = ROOT / "packages" / "paperparse" / "rules" / "builtin" / "domain.json"

SUB_DIGITS = "₀₁₂₃₄₅₆₇₈₉"
# 键 = str(int(charge))：1 → ⁺（int 无符号），-1 → ⁻
SUP_CHARGE = {"-1": "⁻", "-2": "²⁻", "-3": "³⁻", "1": "⁺", "2": "²⁺", "3": "³⁺"}


def norm_charge(formula: str) -> str:
    """括号电荷归一：SO4(2-) → SO4-2；PO4(3-) → PO4-3（pyvalem 不支持括号电荷；
    电荷符号必须在数字前：-2/+3，否则 "SO42-" 的下标歧义）"""
    return re.sub(r"\((\d)([+-])\)$", r"\2\1", formula)


def to_unicode(elements, charge) -> str:
    """元素计数 + charge → Unicode 上下标形态（BF4- → BF₄⁻）"""
    parts = []
    for sym, n in elements.items():
        parts.append(sym)
        if n and n > 1:
            parts.append("".join(SUB_DIGITS[int(d)] for d in str(n)))
    if charge:
        parts.append(SUP_CHARGE.get(str(charge), ""))
    return "".join(parts)


def parse_formula(formula: str):
    """pyvalem 解析 → (atom_stoich, charge, latex, html) 或 None（含错误信息）"""
    from pyvalem.formula import Formula
    f = norm_charge(formula)
    try:
        fo = Formula(f)
        return dict(fo.atom_stoich), int(fo.charge), fo.latex, fo.html
    except Exception as e:  # noqa: BLE001
        return None, f"pyvalem: {type(e).__name__}: {e}"


_VAR_SUFFIX = re.compile(r"^(.+?)([xyznm])$")


def classify(formula: str) -> dict:
    """分类 seed 条目 → 化学式条目 / 变量下标条目 / 专名保护 / 无效
    返回 {"kind": ..., "formula": ..., "elements": {...}, "charge": int,
          "latex": str, "unicode": str, "variable": str|None, "error": str|None}"""
    # 专名/缩写（非化学式）：含 : 、聚合物名、或 pyvalem 拒绝的常见缩写
    if ":" in formula or re.search(r"[a-z]{3,}", formula) or re.search(r"[^A-Za-z0-9+()\-]", formula):
        return {"kind": "protected", "formula": formula}
    res = parse_formula(formula)
    if res[0] is not None:
        stoich, charge, latex, html = res
        stoich = {k: (v or 1) for k, v in stoich.items()}
        # pyvalem 原生支持变量下标（CoOx → \mathrm{Co}\mathrm{O}_{x}）→ variable 类
        mvar = re.search(r"_\{([xyznm])\}", latex)
        if mvar:
            var = mvar.group(1)
            return {"kind": "variable", "formula": formula, "elements": stoich,
                    "charge": charge, "latex": latex, "html": html,
                    "unicode": to_unicode(stoich, charge) + var, "variable": var}
        return {"kind": "chemistry", "formula": formula, "elements": stoich,
                "charge": charge, "latex": latex, "html": html,
                "unicode": to_unicode(stoich, charge), "variable": None}
    # 变量下标（CoOx / CoO_x）：剥尾部变量再解析（pyvalem 老版本兜底）
    m = _VAR_SUFFIX.match(formula)
    if m and m.group(2) in "xyznm":
        res2 = parse_formula(m.group(1))
        if res2[0] is not None:
            stoich, charge, latex, html = res2
            var = m.group(2)
            latex_fixed = latex[:-1] + "_{%s}" % var + latex[-1:] if latex.endswith("}") else latex
            return {"kind": "variable", "formula": formula, "elements": stoich,
                    "charge": charge, "latex": latex_fixed, "html": html,
                    "unicode": to_unicode(stoich, charge) + var, "variable": var}
    # pyvalem 拒绝 → 专名/缩写（聚合物等），进保护清单（人工 seed，不产出化学式）
    return {"kind": "protected", "formula": formula}


def pubchem_formula(name: str) -> str | None:
    """PubChem PUG REST 名称→分子式（Hill 序如 O4S-2）。未收录 404 → None"""
    url = ("https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/"
           + urllib.parse.quote(name) + "/property/MolecularFormula/JSON")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "paperparse-dict-gen/0.1"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data["PropertyTable"]["Properties"][0]["MolecularFormula"]
    except Exception:  # noqa: BLE001 - 404/网络错误 → None
        return None


def normalize_hill(hill: str) -> str:
    """PubChem Hill 式 → pyvalem 可解析形态（O4S-2 → O4S-2 已可；C2H3O2- 可直接）"""
    return hill


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pubchem", action="store_true", help="PubChem 交叉验证离子清单")
    ap.add_argument("--seed", default=str(DEFAULT_SEED))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    seed = json.loads(Path(args.seed).read_text(encoding="utf-8"))

    # ---- 1. 化学式（离子 + 化合物）----
    from periodictable import elements as pt_elements
    elements_table = sorted(e.symbol for e in pt_elements if e.symbol)
    chemistry, variable_f, protected, invalid = [], [], [], []
    protected.extend(seed.get("protected_names", []))   # 强制专名保护（含能解析成假式的缩写）
    for item in seed.get("ions", []) + [{"formula": c} for c in seed.get("compounds", [])]:
        formula = item["formula"]
        r = classify(formula)
        if r["kind"] == "chemistry":
            chemistry.append({
                "id": "DOM-CHEM-%03d" % (len(chemistry) + 1),
                "formula": r["formula"], "elements": r["elements"],
                "charge": r["charge"], "unicode": r["unicode"],
                "latex": r["latex"], "name": item.get("name", ""),
                "confidence": 0.95, "enabled": True,
                "note": "seed ion/compound", "pubchem": None})
        elif r["kind"] == "variable":
            variable_f.append({
                "id": "DOM-VAR-%03d" % (len(variable_f) + 1),
                "formula": r["formula"], "elements": r["elements"],
                "charge": r["charge"], "unicode": r["unicode"],
                "latex": r["latex"], "variable": r["variable"],
                "confidence": 0.9, "enabled": True,
                "note": "variable subscript", "pubchem": None})
        elif r["kind"] == "protected":
            protected.append(formula)
        else:
            invalid.append({"formula": formula, "error": r.get("error")})

    # ---- 2. PubChem 交叉验证（--pubchem；离子名称→Hill 式→元素计数比对）----
    if args.pubchem:
        for entry in chemistry:
            if not entry["name"] or entry["charge"] == 0:
                continue
            hill = pubchem_formula(entry["name"])
            time.sleep(0.35)                      # 限速（官方建议 ≤5 req/s）
            if hill is None:
                entry["pubchem"] = "not-found"
                continue
            r = parse_formula(hill)
            if r[0] is None:
                entry["pubchem"] = "unparseable:%s" % hill
                continue
            stoich, chg, _, _ = r
            # PubChem 单原子离子不返回电荷（Na→"Na"）→ 仅 1 元素时跳过 charge 比对
            charge_ok = len(entry["elements"]) == 1 or chg == entry["charge"]
            match = (dict(stoich) == entry["elements"] and charge_ok)
            entry["pubchem"] = "match" if match else "MISMATCH(%s vs %s)" % (hill, entry["formula"])
        mism = [e for e in chemistry if e["pubchem"] and "MISMATCH" in e["pubchem"]]
        if mism:
            print("[warn] PubChem 不一致条目:", [(e["formula"], e["pubchem"]) for e in mism])

    # ---- 3. 单位（pint 校验 → Unicode 规范 + aliases）----
    import pint
    ureg = pint.UnitRegistry()
    unit_conv = {
        "degC": ("°C", ["℃"]), "ohm": ("Ω", ["Ohm"]), "Mohm": ("MΩ", ["MOhm"]),
        "kohm": ("kΩ", ["kOhm"]), "us": ("μs", ["µs"]), "um": ("μm", ["µm"]),
        "uL": ("μL", ["µL"]), "ug": ("μg", ["µg"]), "umol": ("μmol", ["µmol"]),
        "uF": ("μF", ["µF"]), "uC": ("μC", ["µC"]), "cm-1": ("cm⁻¹", ["cm−1"]),
        "cm-2": ("cm⁻²", ["cm−2"]), "m-1": ("m⁻¹", ["m−1"]), "m-2": ("m⁻²", ["m−2"]),
        "cm-3": ("cm⁻³", ["cm−3"]), "m3": ("m³", ["m3"]), "cm3": ("cm³", ["cm3"]),
        "cm2": ("cm²", ["cm2"]),
    }
    units, unit_warn = [], []
    _POW_FIX = {"cm-1": "cm^-1", "cm-2": "cm^-2", "m-1": "m^-1", "m-2": "m^-2",
                "cm-3": "cm^-3", "m3": "m^3", "cm3": "cm^3", "cm2": "cm^2",
                "m2": "m^2", "cm-4": "cm^-4", "m-3": "m^-3", "g/cm3": "g/cm^3",
                "mg/mL": "mg/mL", "mol/L": "mol/L"}
    for u in seed.get("units", []):
        sym, aliases = unit_conv.get(u, (u, []))
        try:
            ureg(_POW_FIX.get(u, u))
            units.append({"id": "DOM-UNIT-%03d" % (len(units) + 1),
                          "symbol": sym, "aliases": aliases, "ascii": u,
                          "confidence": 0.95, "enabled": True, "note": ""})
        except Exception as e:  # noqa: BLE001
            unit_warn.append({"unit": u, "error": str(e)[:80]})

    # ---- 4. 输出 ----
    out = {
        "schema_version": 1,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "build_domain_dict.py + seed_base.json (pyvalem/pint/periodictable 校验, PubChem 交叉验证)",
        "elements": elements_table,
        "chemistry": chemistry,
        "variable_formulas": variable_f,
        "units": units,
        "protected_names": sorted(set(protected)),
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    print("化学式条目:", len(chemistry), "| 变量下标:", len(variable_f),
          "| 单位:", len(units), "| 专名保护:", len(protected), "| 无效:", len(invalid))
    if invalid:
        print("无效条目:", invalid)
    if unit_warn:
        print("单位警告:", unit_warn)
    print("输出:", args.out)


if __name__ == "__main__":
    main()
