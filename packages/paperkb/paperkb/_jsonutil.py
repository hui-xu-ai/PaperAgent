# -*- coding: utf-8 -*-
r"""容错 JSON 解析工具——翻译（translate.pipeline）与编译（compile）**共用单一来源**。

LLM（尤其智谱 glm 等推理模型）输出的 JSON 常带三类瑕疵，令严格 ``json.loads`` 失败：
  ① **非法转义**：LaTeX 残留在字符串里写成单反斜杠（``$\mathrm{…}$`` 的 ``\m``、``\c``
     不是合法 JSON 转义）；
  ② **推理前缀 / 尾部杂文本**：JSON 前后夹带思维链或说明文字；
  ③ **输出截断**：触顶后 JSON 没收尾，但内部子对象往往仍完整。

此前只有 translate.pipeline 有这套兜底，compile 侧用裸 ``json.loads`` + 贪婪 ``{.*}`` ⇒
glm 编译输出含 LaTeX 反斜杠时整轮判死（"L1 编译输出无效（JSON 缺失 one_liner）"）。
抽到本模块后两侧同源，避免再次漂移。
"""
from __future__ import annotations

import json
import re

# 合法 JSON 转义：\" \\ \/ \b \f \n \r \t \uXXXX；其余反斜杠一律视为 LaTeX 残留待修复。
_INVALID_JSON_ESC_RE = re.compile(r'\\(?!["\\/bfnrt]|u[0-9a-fA-F]{4})')


def repair_json_escapes(s: str) -> str:
    """把"非合法 JSON 转义"的反斜杠补成双反斜杠（合法 → 解析时还原为单反斜杠）。"""
    return _INVALID_JSON_ESC_RE.sub(lambda m: "\\\\", s)


def loads_lenient(s: str) -> dict | None:
    """``json.loads``，失败时修复非法 LaTeX 转义再解析。返回 dict 或 None。"""
    try:
        d = json.loads(s)
        return d if isinstance(d, dict) else None
    except json.JSONDecodeError:
        pass
    try:
        d = json.loads(repair_json_escapes(s))
        return d if isinstance(d, dict) else None
    except json.JSONDecodeError:
        return None


def balanced_extract(text: str, start: int) -> dict | None:
    """从 ``text[start]=='{'`` 起做平衡括号提取，返回解析成功的 dict；否则 None。

    处理嵌套花括号 / 字符串内引号与转义（含 LaTeX 非法转义修复）。截断输入里完整的
    内层子对象也能被单独救回。
    """
    depth = in_str = esc = 0
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return loads_lenient(text[start:i + 1])
    return None


def strip_code_fence(text: str) -> str:
    """剥 ```` ```json … ``` ```` 围栏并 trim。"""
    t = (text or "").strip()
    return re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", t, flags=re.S).strip()


def extract_json_object(text: str, expected_keys: tuple[str, ...] = ()) -> dict:
    """从可能含推理前后缀 / 非法转义 / 截断的 LLM 输出里**稳健提取首个 JSON 对象**。

    顺序：① 整体宽松解析 → ② 按 ``expected_keys`` 定位 ``{"key"`` 平衡提取（容忍前后杂文本）
    → ③ 任意 ``{`` 平衡提取（截断数组里救完整子对象）→ ④ 贪婪 ``{.*}``。全失败返回 ``{}``。
    """
    t = strip_code_fence(text)
    if not t:
        return {}
    d = loads_lenient(t)
    if d is not None:
        return d
    for key in expected_keys:
        for m in re.finditer(r'\{"%s"' % re.escape(key), t):
            obj = balanced_extract(t, m.start())
            if obj is not None:
                return obj
    for m in re.finditer(r"\{", t):
        obj = balanced_extract(t, m.start())
        if obj is not None:
            return obj
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        d = loads_lenient(m.group(0))
        if d is not None:
            return d
    return {}
