# -*- coding: utf-8 -*-
"""译文形态归一测试（2026-09-23 用户报障"上下标有的加了 $、有的丢失"）。"""
from __future__ import annotations

import pytest

from paperkb.textnorm import (html_script_to_tex, normalize_citation_superscripts,
                              suspicious_superscripts)


@pytest.mark.parametrize("raw, want", [
    ("断裂而下降。^[[38]] 图2a展示", "断裂而下降。^{[38]} 图2a展示"),   # 模型自造双层括号
    ("更有利于IL的进入。^[[34]] 一般", "更有利于IL的进入。^{[34]} 一般"),
    ("特性决定。^[15] 不同浸渍", "特性决定。^{[15]} 不同浸渍"),          # 单层方括号
    ("如^[12–14]所述", "如^{[12–14]}所述"),                          # 短破折号区间
    ("见^[15,16];", "见^{[15,16]};"),                               # 逗号并列
    ("^[[38] 缺右", "^{[38]} 缺右"),                                 # 不对称（少一个右括号）
    ("^[38]] 缺左", "^{[38]} 缺左"),                                 # 不对称（多一个右括号）
])
def test_normalize_bracket_superscripts(raw, want):
    got, n = normalize_citation_superscripts(raw)
    assert got == want
    assert n == 1


def test_normalize_is_idempotent():
    once, n1 = normalize_citation_superscripts("下降。^[[38]] 图2a ^[15]")
    twice, n2 = normalize_citation_superscripts(once)
    assert once == "下降。^{[38]} 图2a ^{[15]}"
    assert twice == once and n2 == 0, "第二次不应再改动（幂等）"


@pytest.mark.parametrize("keep", [
    "$^{[29]}$",            # 已规范
    "^{[38]}",              # 英文原文约定（渲染层负责包 $）
    "^[见附录]",             # 真·内联脚注（非数字内容）不碰
    "x^[a]",                # 非引用内容不碰
])
def test_no_false_positive(keep):
    got, n = normalize_citation_superscripts("前文" + keep + "后文")
    assert got == "前文" + keep + "后文"
    assert n == 0


def test_html_script_to_tex_keeps_semantics():
    """`<sup>`/`<sub>` 必须转成 TeX 记法——旧实现直接删标签会丢上标（用户报障）。"""
    assert html_script_to_tex("温度<sup>[21,22]</sup>升高") == "温度^{[21,22]}升高"
    assert html_script_to_tex("H<sub>2</sub>O") == "H_{2}O"
    assert html_script_to_tex("无标签") == "无标签"
    # 配合清标签后，语义仍在
    from paperkb.translate.pipeline import _strip_html_tags

    assert _strip_html_tags(html_script_to_tex("<sup>[3]</sup>")) == "^{[3]}"


@pytest.mark.parametrize("raw, want", [
    ("membrane. ^{[34]} Therefore", "membrane. $^{[34]}$ Therefore"),
    (r"$\mathrm{BF4^{-}}$ 与 ^{2}", r"$\mathrm{BF4^{-}}$ 与 $^{2}$"),
    # ★关键：数学环境内的 _{x} 不得被撑成非法嵌套
    (r"$\mathrm{Co(O_{x})}$ 保持", r"$\mathrm{Co(O_{x})}$ 保持"),
    ("$$E = m c^2$$ 与 x_{i}", "$$E = m c^2$$ 与 x$_{i}$"),
    ("已是 $^{[29]}$ 规范", "已是 $^{[29]}$ 规范"),        # 不重复包
])
def test_wrap_bare_scripts(raw, want):
    from paperkb.textnorm import wrap_bare_scripts

    got, _n = wrap_bare_scripts(raw)
    assert got == want
    again, n2 = wrap_bare_scripts(got)
    assert again == got and n2 == 0, "幂等"


def test_wrap_bare_scripts_noop_when_clean():
    from paperkb.textnorm import wrap_bare_scripts

    for t in ("纯文本没有上下标", "$^{[1]}$ 已是规范", ""):
        got, n = wrap_bare_scripts(t)
        assert got == t and n == 0


def test_suspicious_reports_leftovers():
    """监测：归一后仍以 `^[` 开头的片段会被报出来（真脚注/模型新形态）。"""
    left, n = normalize_citation_superscripts("下降^[[38]] 见^[附录A]")
    assert n == 1
    assert suspicious_superscripts(left) == ["^[附录A]"]


# ---------------------------------------------------------------- 数学段花括号配平
# 2026-09-23 用户实测（Qwen 译 adma）：译文里出现 `$\mathrm{Co(O_{x}/P_{x})$核心`，
# KaTeX（throwOnError:false）渲染成红字。公式逐字来自英文源文 ⇒ `$...$` 内必须配平。


@pytest.mark.parametrize("raw, want", [
    # ★用户实测的坏形态：`\mathrm{` 的收尾 `}` 被模型弄丢（少 `}` → 段尾补齐）
    (r"$\mathrm{Co(O_{x}/P_{x})$核心", r"$\mathrm{Co(O_{x}/P_{x})}$核心"),
    # 反向：多一个 `}`（源文两种写法混抄）→ 删掉多余的那个
    (r"$\mathrm{Co(O_{x}/P_{x})}@P-LIG}$", r"$\mathrm{Co(O_{x}/P_{x})}@P-LIG$"),
    # `$$...$$` 同样适用
    (r"$$\frac{a{b}$$", r"$$\frac{a{b}}$$"),
])
def test_balance_math_braces_fixes(raw, want):
    from paperkb.textnorm import balance_math_braces

    got, n = balance_math_braces(raw)
    assert got == want and n == 1
    again, n2 = balance_math_braces(got)
    assert again == got and n2 == 0, "幂等"


@pytest.mark.parametrize("keep", [
    r"$\mathrm{Co(O_{x}/P_{x})}$ 保持",     # 配平不动
    r"$$E = m c^2$$",                       # 无花括号
    r"$$\frac{a}{b}$$ 与 $c$",              # 多个配平段并存
    r"$\{x\}$ 转义",                        # 转义的 \{ \} 是字面量，不参与计数
    r"$\{x$ 只有转义左括号",                 # 缺的是"真括号"，转义的不补
    "纯文本没有公式",
    "",
])
def test_balance_math_braces_noop(keep):
    from paperkb.textnorm import balance_math_braces

    got, n = balance_math_braces(keep)
    assert got == keep and n == 0, f"{keep!r} 不该被改"


def test_balance_math_braces_nested():
    """嵌套分组（`\\text{}` 里再嵌 `{}`）不能被误当作"多一个"。"""
    from paperkb.textnorm import balance_math_braces

    t = r"$\text{for all } x \in {1,2}$"
    got, n = balance_math_braces(t)
    assert got == t and n == 0


@pytest.mark.parametrize("keep", [
    # `$...$` 是全文配对：两处普通 `$` 之间夹着 `{}` 时，**不像公式就不许动**（宁可漏修）
    "价格 $100 到 ${200 区间} 与 $300 结束",
    "散文 $带 {花括号 的 $ 片段",
])
def test_balance_math_braces_refuses_non_math(keep):
    """★闸门：不配平但**看不出是公式**的 `$...$` 段一律不碰（防改坏散文/代码）。"""
    from paperkb.textnorm import balance_math_braces

    got, n = balance_math_braces(keep)
    assert got == keep and n == 0


def test_balance_math_braces_handles_latex_linebreak():
    """`\\\\{`（LaTeX 换行 + 真括号）里那个 `{` 是**真括号**，必须参与计数、该补就补。"""
    from paperkb.textnorm import balance_math_braces

    raw = "$a \\\\{b_{1}$"          # 实际文本：$a \\{b_{1}$ —— `\\` 是换行，`{` 是真括号
    got, n = balance_math_braces(raw)
    assert got == "$a \\\\{b_{1}}$" and n == 1


def test_unbalanced_math_spans_reports():
    """监测：配平前能报出坏段，配平后为空（供回归脚本全库体检）。"""
    from paperkb.textnorm import balance_math_braces, unbalanced_math_spans

    bad = r"前文 $\mathrm{Co(O_{x}/P_{x})$ 后文"
    assert unbalanced_math_spans(bad) == [r"\mathrm{Co(O_{x}/P_{x})"]
    good, n = balance_math_braces(bad)
    assert n == 1 and unbalanced_math_spans(good) == []
