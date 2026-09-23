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
