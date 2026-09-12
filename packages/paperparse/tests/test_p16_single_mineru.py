# -*- coding: utf-8 -*-
"""P16 单 MinerU 修复 + QA 检查单测"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from paperparse.core.repair_paragraphs import repair_md_paragraphs
from paperparse.core.p14_pipeline import build_markdown
from paperparse.core.md_qa_check import (check_markdown_qa, classify_block,
                                         _strip_trailing_refs)
from paperparse.core.latex_normalize import normalize_formula_fragments
from paperparse.core.p14_pipeline import (_fallback_merge_intro_first,
                                          _is_paddle_authoritative,
                                          _formula_equivalent,
                                          _is_formula_conflict,
                                          _wrap_paddle_formulas,
                                          _fix_formula_commands,
                                          _apply_arbitrations,
                                          _resolve_replacement_chars,
                                          _formula_self_check,
                                          _brace_imbalance,
                                          _is_isolated_short_char)
from paperparse.core.repair_paragraphs import RepairItem


class TestIsolatedShortChar:
    """P16：孤立短字符段落清洗——单字符/无词短片段删除，有词保留"""

    def test_isolated_chars_are_noise(self):
        assert _is_isolated_short_char("d") is True
        assert _is_isolated_short_char("b.") is True
        assert _is_isolated_short_char("1") is True
        assert _is_isolated_short_char("Co") is True

    def test_has_word_kept(self):
        assert _is_isolated_short_char("b. Working principle of the device") is False
        assert _is_isolated_short_char("The surface electrical conductivity") is False
        assert _is_isolated_short_char("CoO_x") is False

    def test_build_markdown_drops_isolated_char(self):
        from paperparse.core.p14_pipeline import build_markdown
        items = [RepairItem(para_id="R1", text="d", kind="body"),
                 RepairItem(para_id="R2", text="A real sentence here.", kind="body")]
        md = build_markdown(items)
        assert "d" not in md.split("\n\n")[0] or "d" not in md
        assert "A real sentence here." in md


class TestCrossFormulaMerge:
    """问题1：跨公式合并 bug 回归——公式是结构锚，两侧正文不得跨公式合并"""

    def _skeleton(self):
        from paperparse.core.skeleton_local import (LocalSkeleton, LocalPara,
                                                    LocalLine)
        lines = [
            LocalLine(line_id="L001", page=1, bbox=(37.6, 100, 300, 112),
                      text="1. Introduction", kind="heading"),
            LocalLine(line_id="L002", page=1, bbox=(49.5, 120, 300, 400),
                      text="The equivalent stiffness can be calculated as: "
                           "E = 3.87 where m is the mass and l is the length.",
                      kind="body"),
        ]
        return LocalSkeleton(
            lines=lines,
            paragraphs=[LocalPara(para_id="LP001", lines=[lines[0]],
                                  text=lines[0].text, kind="body",
                                  closed=True, pages=[1]),
                        LocalPara(para_id="LP002", lines=[lines[1]],
                                  text=lines[1].text, kind="body",
                                  closed=True, pages=[1])])

    def test_equation_blocks_cross_merge(self):
        """公式块（$$）夹在 "…calculated as:" 与 "where m is…" 之间 → 不合并"""
        sk = self._skeleton()
        md = """# Title

## 1. Introduction

The equivalent stiffness can be calculated as:

$$
E = 3.87
$$

where m is the mass and l is the length.

## 2. Results
"""
        res = repair_md_paragraphs(md, sk)
        merged = [r for r in res.paragraphs if r.source == "merged"]
        assert not any("calculated as" in r.text and "where m is" in r.text
                       for r in merged), "跨公式不得合并：'where m is' 不能拼到 '…as:' 后"
        # 公式保留为独立 equation 段
        eq = [r for r in res.paragraphs if r.kind == "equation"]
        assert any("E = 3.87" in r.text for r in eq), "公式块应保留"

    def test_no_skip_merge_across_equation(self):
        """B 分支（隔段合并）不得**跨公式跳跃**：p(引导句) + $$方程$$ + legend，
        legend 不覆盖共同本地段时，p 不得与公式另一侧的后段拼成一段
        （实证：merge_skip_middle 把 "…calculated as:" 与 "the parameters
        were measured." 拼成 "calculated as: the parameters…"）"""
        from paperparse.core.skeleton_local import (LocalSkeleton, LocalPara,
                                                    LocalLine)
        lines = [
            LocalLine(line_id="L001", page=1, bbox=(37.6, 100, 300, 112),
                      text="1. Introduction", kind="heading"),
            LocalLine(line_id="L002", page=1, bbox=(49.5, 120, 300, 400),
                      text="The stiffness can be calculated as: k = 3E/h. "
                           "the parameters were measured.",
                      kind="body"),
        ]
        sk = LocalSkeleton(
            lines=lines,
            paragraphs=[LocalPara(para_id="LP001", lines=[lines[0]],
                                  text=lines[0].text, kind="body",
                                  closed=True, pages=[1]),
                        LocalPara(para_id="LP002", lines=[lines[1]],
                                  text=lines[1].text, kind="body",
                                  closed=True, pages=[1])])
        md = """# Title

## 1. Introduction

The stiffness can be calculated as:

$$
E = 1
$$

where m is the mass.

the parameters were measured.

## 2. Results
"""
        res = repair_md_paragraphs(md, sk)
        # 跨公式：p 与公式另一侧的后段不得合并
        merged = [r for r in res.paragraphs if r.source == "merged"]
        assert not any("calculated as" in r.text
                       and "parameters were measured" in r.text
                       for r in merged), "B 分支不得跨公式跳跃合并"
        # 公式保留为独立 equation 段
        eq = [r for r in res.paragraphs if r.kind == "equation"]
        assert any("E = 1" in r.text for r in eq), "公式块应保留"
        # 引导句本身独立保留
        assert any(r.text.strip().startswith("The stiffness can be calculated as:")
                   for r in res.paragraphs)

    def test_single_dollar_equation_is_anchor(self):
        """单 $ 对整段显示公式（mineru 偶发）也应识别为 equation 结构锚"""
        from paperparse.core.skeleton_local import (LocalSkeleton, LocalPara,
                                                    LocalLine)
        lines = [
            LocalLine(line_id="L001", page=1, bbox=(37.6, 100, 300, 112),
                      text="1. Introduction", kind="heading"),
            LocalLine(line_id="L002", page=1, bbox=(49.5, 120, 300, 400),
                      text="The stiffness can be calculated as: E = 1 "
                           "where m is the mass.",
                      kind="body"),
        ]
        sk = LocalSkeleton(
            lines=lines,
            paragraphs=[LocalPara(para_id="LP001", lines=[lines[0]],
                                  text=lines[0].text, kind="body",
                                  closed=True, pages=[1]),
                        LocalPara(para_id="LP002", lines=[lines[1]],
                                  text=lines[1].text, kind="body",
                                  closed=True, pages=[1])])
        md = """# Title

## 1. Introduction

The stiffness can be calculated as:

$E = 1$

where m is the mass.

## 2. Results
"""
        res = repair_md_paragraphs(md, sk)
        eq = [r for r in res.paragraphs if r.kind == "equation"]
        assert any("E = 1" in r.text for r in eq), "单 $ 显示公式应判为 equation"
        merged = [r for r in res.paragraphs if r.source == "merged"]
        assert not any("calculated as" in r.text and "where m is" in r.text
                       for r in merged), "单 $ 公式两侧正文不得跨公式合并"


class TestPanelLabelClean:
    """问题2：(a)(b) 图面板标签清洗——body 剥、caption 保留"""

    def _items(self):
        return [
            RepairItem(para_id="RP001", text="(a)", kind="body"),
            RepairItem(para_id="RP002", text="(a)\n(b)", kind="body"),
            RepairItem(para_id="RP003", text="(c)4", kind="body"),
            RepairItem(para_id="RP004",
                       text="(d) Dual-responsive electro-magneto ionic actuation",
                       kind="body"),
            RepairItem(para_id="RP005",
                       text="The results are shown in Figure 2a and b.",
                       kind="body"),
            RepairItem(para_id="RP006",
                       text="Figure 2. Nanoscale characterization. a) SEM, b) TEM.",
                       kind="caption"),
            RepairItem(para_id="RP007", text="## 2. Results", kind="heading"),
        ]

    def test_body_panel_labels_stripped(self):
        md = build_markdown(self._items())
        # 纯标签段被删
        assert "(a)" not in md and "(b)" not in md and "(c)4" not in md
        # 标签+真实标题 → 保留标题、剥标签
        assert "Dual-responsive electro-magneto" in md
        assert not re_search(r"^\(d\)", md)
        # 正文含 Fig 引用（无孤立 (a) 标签）→ 保留
        assert "Figure 2a and b." in md

    def test_caption_subfigure_kept(self):
        md = build_markdown(self._items())
        # 图注子图编号 a) b) 保留
        assert "a) SEM" in md and "b) TEM" in md


class TestQaCheck:
    def test_classify(self):
        assert classify_block("## 2. Results") == "heading"
        assert classify_block("![](images/x.jpg)") == "image"
        assert classify_block("Figure 1. caption here.") == "caption"
        assert classify_block("$$\nE=3\n$$") == "equation"
        assert classify_block("[12] C. Zhao et al.") == "ref_entry"
        assert classify_block("The body text here.") == "body"

    def test_strip_trailing_refs(self):
        assert _strip_trailing_refs("text here. [12]").endswith(".")
        # 括号引用 (Fig. 1a) 被剥 → 暴露真正的句尾句号
        assert _strip_trailing_refs("text. (Fig. 1a)").endswith(".")

    def test_qa_flags(self):
        md = """## 2. Results

a

short para

A complete and proper sentence ending with a period here.

the next paragraph starts lowercase without a terminal and has no end

## References

[1] Ref here.
"""
        r = check_markdown_qa(md)
        # "a" 和 "short para" 过短
        shorts = [p for p in r["paragraphs"] if "short" in p["flags"]]
        assert len(shorts) >= 2
        # 段首小写 + 无结束符
        low = [p for p in r["paragraphs"] if "lower_start" in p["flags"]]
        assert any("without a terminal" in p["text"] for p in low)

    def test_fig_caption_mismatch(self):
        md = "![](images/a.jpg)\n\nFig. 1. cap one.\n\n![](images/b.jpg)\n"
        r = check_markdown_qa(md)
        assert r["figures"]["image_markers"] == 2
        assert r["figures"]["captions"] == 1
        assert r["figures"]["match"] is False
        assert any("图图注不对应" in i for i in r["issues"])


class TestFormulaNormalize:
    """P16 方案A：公式碎片归一化（mineru 空格拆散 → 合法紧凑 LaTeX）"""

    def test_collapse_number_fragments(self):
        assert normalize_formula_fragments("${ \\approx } 0 . 3 4$") == \
            "${\\approx}0.34$"
        assert normalize_formula_fragments("$3 2 . 2 ^ { \\circ }$") == \
            "$32.2^{\\circ}$"

    def test_collapse_mathrm(self):
        assert normalize_formula_fragments(
            "$\\mathrm { C o O } _ { \\mathrm { x } } @ \\mathrm { L I G }$") == \
            "$\\mathrm{CoO}_{\\mathrm{x}}@\\mathrm{LIG}$"

    def test_control_space_preserved(self):
        # backslash-space（控制空格）必须保留
        assert normalize_formula_fragments(
            "$4 3 . 1 ^ { \\circ } \\ ( 2 \\theta )$") == \
            "$43.1^{\\circ}\\ (2\\theta)$"

    def test_block_equation(self):
        assert normalize_formula_fragments(
            "$$\nE = 3. 8 7 \\pi^ {2} \\frac {m f ^ {2} l ^ {3}}{h t ^ {3}}\\tag{1}\n$$") == \
            "$$E=3.87\\pi^{2}\\frac{mf^{2}l^{3}}{ht^{3}}\\tag{1}$$"

    def test_text_cmd_spaces_kept(self):
        # \text{} 内空格保留
        assert normalize_formula_fragments(
            "$\\text { in the } x$") == "$\\text { in the }x$"


class TestIntroFirstMerge:
    """P16 问题9保底：前言第一段过短(<80词) → 与下一段拼接"""

    def test_merge_short_intro_first(self):
        md = """## 1. Introduction

Artificial muscles, designed to emulate natural muscles manifest reversible
transformations in real-world applications.

Electro-ionic soft actuators, characterized by lightweight and flexibility,
have emerged as promising candidates.

## 2. Results
"""
        out = _fallback_merge_intro_first(md)
        assert "applications.\n\nElectro-ionic" not in out
        assert "applications. Electro-ionic" in out

    def test_no_merge_when_long(self):
        md = ("## 1. Introduction\n\n" + "word " * 90 + ".\n\n"
              "The next paragraph is separate here.\n\n## 2. Results\n")
        out = _fallback_merge_intro_first(md)
        assert "word" in out and "The next paragraph" in out
        # 长首段不拼 → 两段之间仍有空行
        assert "\n\nThe next paragraph" in out

    def test_no_intro_no_merge(self):
        md = "## 2. Results\n\nA short paragraph.\n\nMore text.\n"
        assert _fallback_merge_intro_first(md) == md


class TestPaddleAuthoritative:
    """P16：百度OCR为准——**2026-08-26 缩窄**：只对纯文本词级小噪声（拼写/断词/
    标点/空格）自动百度；公式/LaTeX/上标/单位/数学符号不自动替换（走
    _formula_equivalent 等价→保留 mineru、不等价→复核），防百度漏识别（BF₄ 的 4）。"""

    def _c(self, m, p):
        return {"mineru": {"text": m}, "paddleocr": {"text": p}}

    def test_word_error(self):
        assert _is_paddle_authoritative(self._c("Eficient", "Efficient"))

    def test_missing_space(self):
        assert _is_paddle_authoritative(self._c("volatili zation", "volatilization"))

    def test_punctuation_missing_period(self):
        assert _is_paddle_authoritative(self._c("end", "end."))

    def test_latex_superscript(self):
        # 格式等价差异（作者上标）→ 不自动百度替换（char_conflicts _eq 已跳过）
        assert not _is_paddle_authoritative(self._c("$^{a,1}$", "<sup>a,1</sup>"))

    def test_unit_superscript(self):
        # Unicode 下标/公式符号 → 不自动百度替换（_eq 归一后等价跳过）
        assert not _is_paddle_authoritative(self._c("BF4", "BF\u2084"))

    def test_whitespace(self):
        assert _is_paddle_authoritative(self._c(" ", ""))

    def test_formula_equivalent(self):
        # mineru LaTeX 与 paddle 明文同义 → 内容等价（保留 LaTeX，不替换）
        c = {"mineru": {"text": "$\\mathrm{Co(O_x/P_x)@P\\cdot LIG\\cdot P.P}$"},
             "paddleocr": {"text": "Co(O_x/P_x)@P-LIG-P.P"}}
        assert _is_formula_conflict(c)
        assert _formula_equivalent(c)

    def test_formula_not_equivalent(self):
        c = {"mineru": {"text": "$\\mathrm{CoO_x}$"},
             "paddleocr": {"text": "different substance"}}
        assert _is_formula_conflict(c)
        assert not _formula_equivalent(c)

    def test_wrap_paddle_formula(self):
        assert "$\\mathrm{Co(O_x/P_x)@P-LIG-P.P}$" in \
            _wrap_paddle_formulas("the Co(O_x/P_x)@P-LIG-P.P actuator")
        assert _wrap_paddle_formulas("normal words only") == \
            "normal words only"

    def test_wrap_paddle_formula_residues(self):
        """P16 Q2：历史残留公式缺 $ 案例——度单位/Unicode 数学符/带花括号上标"""
        assert "$\\mathrm{to1.92\u00b0s-1}$" in \
            _wrap_paddle_formulas("to1.92\u00b0s-1")
        assert "$\\mathrm{\u0394V=\u03a6-\u03c6}$" in \
            _wrap_paddle_formulas("\u0394V=\u03a6-\u03c6")
        # 旧正则会把 ^ 后的 { 抛到 $ 外产生 "$\mathrm{s^}${-1}" 破损 → 现整体包
        assert "degree $\\mathrm{s^{-1}}$" == \
            _wrap_paddle_formulas("degree s^{-1}")
        assert "9.80 $\\mathrm{Am^{-2}}$" == \
            _wrap_paddle_formulas("9.80 Am^{-2}")

    def test_wrap_skips_corrupted_source(self):
        """P16 回归：源乱码（未配对$或花括号不平衡）→ 不包裹，防越搞越坏（$$ 伪块）"""
        c = "0.98\u00b0s { - 1 }$(bareLIG),to1.92\u00b0s-1}}"
        assert _wrap_paddle_formulas(c) == c

    def test_wrap_skips_incomplete_signal(self):
        r"""P16 回归：E_ 残缺（无后续内容）→ 不包成 $\mathrm{E_}$（防 E_$Fermi 碎片）"""
        assert _wrap_paddle_formulas("potential, E_ Fermi level") == \
            "potential, E_ Fermi level"

    def test_fix_formula_commands(self):
        r"""P16 Q2：mineru 把氮气识别成希腊字母 Nu → \mathrm{N}（保渲染+语义正确）"""
        assert _fix_formula_commands(r"$\Nu_{2}$") == r"$\mathrm{N}_{2}$"
        assert _fix_formula_commands(r"\Nu_2") == r"\mathrm{N}_{2}"
        # 无数字下标的 \Nu / 小写 \nu 不误改
        assert _fix_formula_commands(r"$\Nu$") == r"$\Nu$"
        assert _fix_formula_commands(r"$\nu$") == r"$\nu$"

    def test_apply_arbitration_wraps_formula(self):
        """P16 Q2：公式类采纳百度明文 → 落地包回 $...$（缺 $ 根治）"""
        from paperparse.core.dual_ai_review import Arbitration
        para = "the conductance is $\\mathrm{CoO_x}$ in the film"
        c = {"type": "text_conflict", "page": 1,
             "mineru": {"text": "$\\mathrm{CoO_x}$"},
             "paddleocr": {"text": "CoO_x"},
             "evidence": {"i1": 0, "i2": 0, "j1": 0, "j2": 0}}
        a = Arbitration(id=0, diff_type="text_conflict", page=1,
                        verdict="paddleocr", reason="百度OCR为准", confidence=1.0,
                        mineru={"text": "$\\mathrm{CoO_x}$"},
                        paddleocr={"text": "CoO_x"})
        out, audit = _apply_arbitrations(para, [c], [a])
        assert "$\\mathrm{CoO_x}$" in out, out
        assert any(x["action"] == "arbitrate_replace" for x in audit)

    def test_apply_arbitration_no_wrap_word(self):
        """P16 Q2：普通单词采纳不包 $（公式判定不命中）"""
        from paperparse.core.dual_ai_review import Arbitration
        para = "Eficient performance"
        c = {"type": "text_conflict", "page": 1,
             "mineru": {"text": "Eficient"},
             "paddleocr": {"text": "Efficient"},
             "evidence": {"i1": 0, "i2": 0, "j1": 0, "j2": 0}}
        a = Arbitration(id=0, diff_type="text_conflict", page=1,
                        verdict="paddleocr", reason="百度OCR为准", confidence=1.0,
                        mineru={"text": "Eficient"},
                        paddleocr={"text": "Efficient"})
        out, _ = _apply_arbitrations(para, [c], [a])
        assert out == "Efficient performance", out


class TestReplaceCharResolve:
    """P16 � 乱码兜底解析：百度通道对齐取正确字符（� 恒为缺陷）"""

    def test_caprolactone(self):
        # poly(�-caprolactone) → poly(ε-caprolactone)，百度明文 ε 直接取
        m = "using poly(\ufffd-caprolactone) (PCL) aligned fiber"
        p = "using poly(\u03b5-caprolactone) (PCL) aligned fiber"
        out, n = _resolve_replacement_chars(m, p)
        assert n == 1
        assert "\ufffd" not in out
        assert "poly(\u03b5-caprolactone)" in out

    def test_phi_latex_command(self):
        # Φ and � represent → 百度 $\varphi$（LaTeX 命令需映射还原成 φ）
        m = "the work functions $\\Delta V = \\Phi - \\varphi$ (\u03a6 and \ufffd represent the work function)"
        p = "the work functions $\\Delta V = \\Phi - \\varphi$ (\u03a6 and $\\varphi$ represent the work function)"
        out, n = _resolve_replacement_chars(m, p)
        assert n == 1
        assert "\ufffd" not in out
        assert "\u03a6 and \u03c6 represent" in out

    def test_phi_plain(self):
        # 百度直接明文 φ
        m = "(\u03a6 and \ufffd represent)"
        p = "(\u03a6 and \u03c6 represent)"
        out, n = _resolve_replacement_chars(m, p)
        assert n == 1
        assert "\u03a6 and \u03c6 represent" in out

    def test_no_ref_no_change(self):
        m = "poly(\ufffd-caprolactone)"
        out, n = _resolve_replacement_chars(m, "")
        assert n == 0 and out == m

    def test_no_fffd_no_change(self):
        m = "normal text"
        out, n = _resolve_replacement_chars(m, "normal text")
        assert n == 0 and out == m


class TestFormulaSelfCheck:
    """P16 输出前公式自检：$配对/括号配对——安全项修复+记录"""

    def test_fix_missing_closing_brace(self):
        # 截断：\mathrm{CoO_x 缺 } → 补足
        md = "the $\\mathrm{CoO_x$ value"
        out, issues = _formula_self_check(md)
        assert "$\\mathrm{CoO_x}$" in out
        assert any("缺}" in i for i in issues)

    def test_escaped_braces_not_miscounted(self):
        # \{ \} 转义不误判
        md = "set $\\{a, b\\}$ and $\\mathrm{Co}$"
        out, issues = _formula_self_check(md)
        assert out == md and not issues

    def test_unpaired_dollar_reported(self):
        # 奇数 $ → 只记录不改
        md = "text $broken formula"
        out, issues = _formula_self_check(md)
        assert "$" in out
        assert any("不配对" in i or "奇数" in i for i in issues)

    def test_balanced_untouched(self):
        md = "normal $a_{i} + b^{2}$ text"
        out, issues = _formula_self_check(md)
        assert out == md and not issues

    def test_brace_imbalance(self):
        assert _brace_imbalance("a_{i} + b") == 0
        assert _brace_imbalance("\\mathrm{CoO_x") == 1
        assert _brace_imbalance("\\mathrm{a}}") == -1


class TestContentPairHybrid:
    """P16 Q1 hybrid 坐标配对：内容为主，近歧义时坐标圈内消歧（不排除段）"""

    def _build(self):
        from paperparse.core.repair_paragraphs import RepairItem
        from paperparse.middleware.schema import TextBlock, ParserBlocks
        from paperparse.core.skeleton_local import LocalPara, LocalLine
        p1 = RepairItem(para_id="P1", kind="body", md_idx=[1],
                        text="The sample was heated to 300 C for two hours "
                             "then cooled slowly")
        p2 = RepairItem(para_id="P2", kind="body", md_idx=[2],
                        text="The sample was heated to 300 C for two hours "
                             "then cooled slowly overnight under vacuum")
        # 坐标：P1 在第3页 y100-120；P2 在第9页 y700-720
        md_of_local = {
            1: [LocalPara(para_id="LP1", kind="body", lines=[
                LocalLine(line_id="L1", page=3, bbox=(0, 100, 0, 120),
                          text=p1.text)])],
            2: [LocalPara(para_id="LP2", kind="body", lines=[
                LocalLine(line_id="L2", page=9, bbox=(0, 700, 0, 720),
                          text=p2.text)])],
        }
        # 块：内容与两段都高重叠（近歧义），bbox 落在 P1 的页/y 区间
        blk = TextBlock(block_id="B1", page=3, bbox=(0, 105, 0, 115),
                        text="The sample was heated to 300 C for two hours "
                             "then cooled slowly overnight",
                        kind="body", source="paddleocr")
        pblocks = ParserBlocks(source="paddleocr", pages=9, blocks=[blk])
        return [p1, p2], pblocks, md_of_local

    def test_coordinate_disambiguates_near_tie(self):
        from paperparse.core.p14_pipeline import _content_pair_paddle
        items, pblocks, md_of_local = self._build()
        pair, _ = _content_pair_paddle(items, pblocks, md_of_local=md_of_local)
        # 坐标圈内 → P1（近歧义时优先空间正确的段）
        assert "P1" in pair and "P2" not in pair

    def test_without_coords_global_content(self):
        from paperparse.core.p14_pipeline import _content_pair_paddle
        items, pblocks, _ = self._build()
        pair, _ = _content_pair_paddle(items, pblocks)
        # 无坐标 → 内容全局最优 P2
        assert "P2" in pair and "P1" not in pair


def re_search(pat, text):
    import re
    return re.search(pat, text)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
