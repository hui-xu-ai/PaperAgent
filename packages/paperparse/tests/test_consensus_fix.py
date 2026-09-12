#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""consensus_fix 单测：领域词典校正层（共识错误——双通道同错兜底）

覆盖：
  1. 畸形判定：带电荷缺下标（BF− → BF₄⁻）各形态（plain/HTML/LaTeX）
  2. 完整形态不动（BF4− / BF₄⁻ / SO₄²⁻ LaTeX / PO₄³⁻）
  3. 裸元素名绝不改（BF 无电荷；blood flow 语境）
  4. 专名保护（Nafion / PEDOT:PSS / P.P / PI 假式）
  5. 单位规范化（℃ → °C）
  6. 非词典元素不动作（Am2 镅假单位、Co(O_x/P_x) 复杂式）
  7. 词典双源加载（builtin + learned 合并；learned 覆盖同 formula）
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paperparse.core.consensus_fix import (  # noqa: E402
    apply_domain_fix, load_domain_dict, reset_cache)


@pytest.fixture(autouse=True)
def _clean_cache():
    reset_cache()
    yield
    reset_cache()


class TestMorphology:
    """形态分层提取：同一种畸形不同 OCR 形态都应校正"""

    def test_plain_unicode(self):
        out, fixes = apply_domain_fix("ions (BF− of IL)")
        assert "BF₄⁻" in out
        assert fixes and fixes[0]["confidence"] >= 0.95

    def test_html_sup(self):
        # mineru 源形态：BF<sup>−</sup>（缺下标 4）
        out, fixes = apply_domain_fix("BF<sup>−</sup> 离子")
        assert "BF₄⁻" in out
        assert fixes

    def test_latex_charge_only(self):
        # LaTeX 带电荷缺下标 → 补下标（块内不产生 $$ 嵌套）
        out, fixes = apply_domain_fix(r"$\mathrm{BF}^{-}$ 与 EMIM+")
        assert r"$\mathrm{B}\mathrm{F}_{4}^{-}$" in out
        assert out.count("$") == 2
        assert fixes

    def test_other_ions(self):
        # 真实残缺形态：电荷保留、下标丢失（SO²⁻ 实为 SO₄²⁻）
        # Cl− 是完整氯离子（− 与 ⁻ 仅表示差异）→ 不修
        out, _ = apply_domain_fix("SO²⁻ 与 PO³⁻ 与 NO− 与 Cl−")
        assert "SO₄²⁻" in out and "PO₄³⁻" in out and "NO₃⁻" in out
        assert "Cl−" in out

    def test_charge_incomplete_review(self):
        # 电荷也残缺（SO− vs SO₄²⁻）：不落地，进 review 候选（conf 0.8）
        out, fixes = apply_domain_fix("SO− 与 PO−")
        assert out == "SO− 与 PO−"               # 不自动改
        rev = [f for f in fixes if f.get("review")]
        assert rev and all(f["confidence"] < 0.95 for f in rev)


class TestNoChange:
    """守卫：完整形态/裸元素/非词典绝不动作"""

    def test_complete_forms_untouched(self):
        for t in ["BF4−", "BF₄⁻", "SO₄²⁻", "NH₄⁺"]:
            out, fixes = apply_domain_fix(t)
            assert out == t, t
            assert not fixes, t

    def test_complete_latex_untouched(self):
        t = r"$\mathrm{SO}_{4}^{2-}$ 与 $\mathrm{PO}_{4}^{3-}$"
        out, fixes = apply_domain_fix(t)
        assert out == t
        assert not fixes

    def test_bare_element_never_changed(self):
        # "BF" 无电荷（可能=血流/缩写）绝不改
        for t in ["blood flow (BF) 与", "BF 与 LIG", "the BF value"]:
            out, fixes = apply_domain_fix(t)
            assert "BF₄" not in out
            assert not fixes

    def test_non_dict_element_untouched(self):
        # Am2（镅假式，单位语境）不在词典 → 不动
        t = "9.80 Am2 kg-1"
        out, fixes = apply_domain_fix(t)
        assert out == t
        assert not fixes

    def test_complex_latex_untouched(self):
        t = r"the $\mathrm{Co(O_{x}/P_{x})@P{\cdot}LIG}$"
        out, fixes = apply_domain_fix(t)
        assert out == t
        assert not fixes


class TestProtected:
    """专名保护：可被解析成假式的缩写/专名不动"""

    def test_polymer_abbrevs(self):
        for t in ["Nafion 溶液", "PEDOT:PSS 中的 P.P", "PI 膜", "PVA 薄膜"]:
            out, fixes = apply_domain_fix(t)
            assert out == t, t
            assert not fixes, t

    def test_emim_preserved(self):
        out, fixes = apply_domain_fix("EMIM+ 与 BF−")
        assert "EMIM" in out
        assert "BF₄⁻" in out          # 同段 BF− 仍修


class TestUnits:
    """单位规范化"""

    def test_degree_celsius(self):
        out, fixes = apply_domain_fix("在 100 ℃ 温度")
        assert "100°C" in out
        assert fixes and fixes[0]["context"] == "unit"

    def test_normal_units_untouched(self):
        for t in ["1.38 s", "200 mT", "100 kHz", "0.5 V"]:
            out, fixes = apply_domain_fix(t)
            assert out == t
            assert not fixes


class TestDictSources:
    """双源加载：builtin + learned（学习闭环入库）"""

    def test_learned_override(self, tmp_path, monkeypatch):
        # 构造外部 learned 层：新增条目 "Hx-"（虚构离子，缺下标）
        rules_dir = tmp_path / "rules"
        learned = rules_dir / "learned"
        learned.mkdir(parents=True)
        entry = {
            "id": "R-TEST-001", "formula": "Hx-", "elements": {"H": 3}, "charge": -1,
            "unicode": "H₃⁻", "latex": r"\mathrm{H}_{3}^{-}",
            "confidence": 0.95, "enabled": True, "note": "test"
        }
        (learned / "domain.json").write_text(
            json.dumps({"chemistry": [entry]}, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setenv("RULES_DIR", str(rules_dir))
        reset_cache()
        d = load_domain_dict()
        assert any(e["formula"] == "Hx-" for e in d["chemistry"])
        # 引擎应用 learned 条目
        out, fixes = apply_domain_fix("H− 离子")
        assert "H₃⁻" in out
        assert fixes


class TestStatsShape:
    """fix 记录结构（audit 用）"""

    def test_fix_shape(self):
        _, fixes = apply_domain_fix("BF− 与 100 ℃")
        assert len(fixes) == 2
        for f in fixes:
            assert {"rule", "before", "after", "confidence", "context"} <= set(f)
