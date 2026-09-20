# -*- coding: utf-8 -*-
"""容错 JSON 解析回归钉（paperkb._jsonutil + compile._parse_json）。

背景（2026-09-21 用户实测 glm-5.3-flash）：编译偶发 "L1 编译输出无效（JSON 缺失 one_liner）"
→ _note.md 不生成。根因：compile 侧旧 `_parse_json` 只用裸 json.loads + 贪婪 {.*}，而 glm
编译输出的 JSON 字符串里含 LaTeX 单反斜杠（`$\mathrm{…}$` 的 `\m` 是非法 JSON 转义）或带
思维链前后缀 → 整段解析失败。修复=与 translate 共用 `_jsonutil`（非法转义修复 + 键定位平衡提取）。
"""
from __future__ import annotations

import json

from paperkb import _jsonutil
from paperkb.compile import _parse_json


def test_repair_invalid_latex_escapes():
    r"""LaTeX 单反斜杠（非法 JSON 转义）被补成双反斜杠后可解析。"""
    bad = r'{"one_liner": "$\mathrm{X}$ 和 \approx20"}'   # \m / \a 非法转义
    try:
        json.loads(bad)
        raised = False
    except json.JSONDecodeError:
        raised = True
    assert raised, "单反斜杠 LaTeX 应让严格 json.loads 失败"
    d = _jsonutil.loads_lenient(bad)
    assert d and d["one_liner"] == r"$\mathrm{X}$ 和 \approx20"


def test_compile_parse_reasoning_prefix_and_escapes():
    """compile._parse_json：思维链前缀 + LaTeX 非法转义 → 仍能提取含 one_liner 的对象。"""
    raw = (
        "好的，这是编译结果：\n"
        '{"one_liner": "一步激光直写 $\\mathrm{Co(O_{x}/P_{x})@P-LIG}$ 双模驱动", '
        '"background": {"text": "导电率 $\\approx20$ 高", "paras": ["P001"]}, '
        '"wiki": "## 方法论批判\\n路径 $\\mathrm{CoP_{x}}$ 分析", '
        '"ai_value": 4}'
    )
    d = _parse_json(raw)
    assert d.get("one_liner"), "应救回 one_liner（旧实现返回 {} → 编译判死）"
    assert "background" in d and "wiki" in d
    assert "\\mathrm" in d["wiki"], "LaTeX 反斜杠应原样保留"


def test_compile_parse_code_fence():
    """```json 围栏包裹也能解析。"""
    raw = '```json\n{"one_liner": "甲", "wiki": "w"}\n```'
    assert _parse_json(raw).get("one_liner") == "甲"


def test_compile_parse_clean_json_unchanged():
    """干净 JSON 行为不变。"""
    raw = json.dumps({"one_liner": "干净", "summary": "s"}, ensure_ascii=False)
    d = _parse_json(raw)
    assert d.get("one_liner") == "干净" and d.get("summary") == "s"


def test_extract_json_object_truncated_array_recovers_inner():
    """截断输出：整体解析失败时，从任意 { 平衡提取首个完整子对象。"""
    trunc = '{"a": 1} 后面还有 {"one_liner": "乙"} 但整体没收尾'
    d = _jsonutil.extract_json_object(trunc, expected_keys=("one_liner",))
    assert d.get("one_liner") == "乙"


def test_extract_json_object_empty_on_garbage():
    assert _jsonutil.extract_json_object("完全不是 JSON") == {}
