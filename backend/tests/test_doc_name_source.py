# -*- coding: utf-8 -*-
"""核心数据文件名的**单一来源**守卫（见 `docs/DATA-LAYOUT.md` §文件名）。

背景（2026-09-23 用户报障"重译后 zh.md/en_zh.md 全英文"）：`library/` 与
`knowledge_base/` 两侧各有一份**同名**的核心数据文件（`document.json`），
而代码里有 7 处各自手拼这个名字 —— "写的一侧"和"读的一侧"指到不同目录时，
就是**静默的错源 bug**（译文写进 kb、渲染却读 library ⇒ 变体退回英文）。

本守卫把病根按住：
  1) 产品代码（`packages/paperkb` + `backend`）不许裸写该文件名的字符串字面量，
     一律走 `paperkb.layout` 的 `LIB_DOC_NAME` / `KB_DOC_NAME` / `doc_path()`；
  2) 状态键 / 契约字段名等**非路径**用法，必须显式写 `# doc-name-ok` 注解
     （逼作者当场声明"这不是路径"，而不是悄悄再开一个来源）；
  3) 前端不许自己拼知识库路径（改由后端 `shared_doc_json` 解析）。

范围说明：`packages/paperparse`（解析引擎）**不在**本守卫内——它只写解析中间产物
（`intermediate/document.json`，恒为解析库侧），不参与"kb / library 二选一"。
真要做两侧改名时，引擎侧位置见 `docs/DATA-LAYOUT.md` 的改名清单。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# 允许裸写该字面量的文件：**常量定义处**（唯一来源）
ALLOW_FILES = {
    "packages/paperkb/paperkb/layout.py",
}
# 扫描范围：产品代码（测试/归档/临时区不算）
SCAN_DIRS = ("packages/paperkb", "backend")
SCAN_EXCLUDE = ("/tests/", "/archive/", "/work/", ".venv", "/migrations/")
# 非路径用法的显式注解
MARKER = "doc-name-ok"

PAT = re.compile(r"""["']document\.json["']""")


def _iter_sources():
    for base in SCAN_DIRS:
        for p in (ROOT / base).rglob("*.py"):
            rel = p.relative_to(ROOT).as_posix()
            if any(x in p.as_posix() for x in SCAN_EXCLUDE):
                continue
            yield rel, p


def test_no_bare_doc_filename_literal():
    """产品代码里不许裸写核心数据文件名（唯一来源 = layout.py 常量）。"""
    bad: list[str] = []
    for rel, p in _iter_sources():
        if rel in ALLOW_FILES:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            if not PAT.search(line):
                continue
            if line.lstrip().startswith("#"):
                continue                      # 注释里的说明文字（非代码路径）
            if MARKER in line:
                continue                      # 显式声明过的非路径用法
            bad.append(f"{rel}:{i}: {line.strip()}")
    assert not bad, (
        "禁止裸写核心数据文件名（`document.json`）——请改用 "
        "`from .layout import doc_basename, doc_path` / `KB_DOC_NAME` / `LIB_DOC_NAME`；"
        "若确为非路径用法（状态键等），在该行加 `# doc-name-ok` 注解。\n"
        + "\n".join(bad))


def test_frontend_does_not_build_kb_doc_path():
    """前端不许自己拼知识库正文路径（唯一来源在后端 `shared_doc_json`）。"""
    js = ROOT / "frontend" / "js" / "papers.js"
    assert js.is_file(), js
    # 只抓"路径拼接"形态（`.../document.json`）；提示文案里的文件名不算
    path_pat = re.compile(r"[/\\]document\.json")
    hits = [f"papers.js:{i}: {l.strip()}"
            for i, l in enumerate(js.read_text(encoding="utf-8").splitlines(), 1)
            if path_pat.search(l) and not l.lstrip().startswith("//")]
    assert not hits, (
        "前端不得拼知识库路径（目录名/文件名会与数据层漂移）；"
        "传 doi 由后端 `shared_doc_json` 解析：\n" + "\n".join(hits))


def test_layout_constants_are_the_single_source():
    """常量本身可用，且两侧当前同名（改名时才允许分叉——那时本断言要跟着改）。"""
    from paperkb.layout import KB_DOC_NAME, LIB_DOC_NAME, doc_basename, doc_path

    assert LIB_DOC_NAME == "document.json"
    assert KB_DOC_NAME == LIB_DOC_NAME
    assert doc_basename(kb=True) == KB_DOC_NAME
    assert doc_basename() == LIB_DOC_NAME
    assert doc_path("x", kb=True).as_posix() == f"x/{KB_DOC_NAME}"
