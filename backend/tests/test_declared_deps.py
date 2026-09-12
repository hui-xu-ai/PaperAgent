# -*- coding: utf-8 -*-
"""依赖声明守卫：代码里 import 的第三方包，必须已在对应 `pyproject.toml` 声明。

来源（2026-09-12 发布期实测）：CI **首次**在干净 Windows 环境跑 pytest 就红了两例
（`test_multi_charge` / `test_latex_valid_strict`）——根因是 `paperparse` 用了
`pyvalem` / `pylatexenc` / `latex2mathml` 却**从未声明**：本地 venv 恰好装过，所以
从没暴露；干净环境里不会崩，而是**静默降级**成"化学式/LaTeX 校验永远不通过"，
是会悄悄毁掉解析质量的那类坑。`paperkb` 的 `fitz`/`docx`、后端的 `PyYAML` 同样漏。

守卫范围：`packages/paperparse`、`packages/paperkb`、`backend/app` 的**全部 .py**。
"""
from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# import 名 → 发行名（少数不一致的）
DIST_ALIAS = {
    "fitz": "pymupdf", "dotenv": "python-dotenv", "yaml": "pyyaml",
    "PIL": "pillow", "docx": "python-docx", "bs4": "beautifulsoup4",
    "webview": "pywebview", "pymupdf": "pymupdf",
}
# 通过 `pip install -e packages/<x>` 就地安装的兄弟包（不在 pyproject 的 dependencies 里）
SIBLINGS = {"paperparse", "paperkb"}

CASES = (
    ("packages/paperparse", ROOT / "packages/paperparse", ROOT / "packages/paperparse/pyproject.toml"),
    ("packages/paperkb", ROOT / "packages/paperkb", ROOT / "packages/paperkb/pyproject.toml"),
    ("backend/app", ROOT / "backend" / "app", ROOT / "pyproject.toml"),
)


def _imports(root: Path) -> set[str]:
    mods: set[str] = set()
    for p in root.rglob("*.py"):
        if any(part in ("__pycache__", "build", "dist") for part in p.parts):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:      # 语法坏文件交给别的测试
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    mods.add(a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods.add(node.module.split(".")[0])
    return mods


def _declared(pyproject: Path) -> set[str]:
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    deps = list(data.get("project", {}).get("dependencies", []))
    for extra in (data.get("project", {}).get("optional-dependencies") or {}).values():
        deps += list(extra)
    out = set()
    for d in deps:
        name = re.split(r"[<>=!~\[; ]", d.strip())[0].strip().lower()
        out.add(name.replace("_", "-"))
    return out


@pytest.mark.parametrize("label,src,pyproject", CASES, ids=[c[0] for c in CASES])
def test_third_party_imports_are_declared(label: str, src: Path, pyproject: Path):
    first_party = {p.name for p in src.iterdir() if p.is_dir()} | {src.name}
    third = {m for m in _imports(src)
             if m not in sys.stdlib_module_names
             and m not in first_party
             and m not in SIBLINGS
             and not m.startswith("_")}
    declared = _declared(pyproject)
    missing = sorted(m for m in third
                     if DIST_ALIAS.get(m, m).lower() not in declared and m.lower() not in declared)
    assert not missing, (
        f"{label} 里有第三方 import 未在 {pyproject.relative_to(ROOT)} 声明：{missing}\n"
        "（干净环境会 ImportError 或静默降级 —— 见本文件 docstring 的实测教训）")
