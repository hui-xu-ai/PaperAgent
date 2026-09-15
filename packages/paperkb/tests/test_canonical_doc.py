# -*- coding: utf-8 -*-
"""定版（唯一权威文件）契约测试 —— 2026-09-16 用户决策（方案 A）的守卫。

背景（审计 C2/C3）：此前 `document.json` 有两份可写副本，而编译读 kb、翻译"读 kb 写 library"、
复核只写 library ⇒ 来源与前缀分叉、成品区可能永久停在旧译文。用户要求：
**解析 → 校核 → 确认唯一版本 → 编译与翻译都用这一份**（来源统一、前缀统一）。

本测试锁定四条不变量：
1. kb 有副本时，`canonical_doc_json` 必须返回 **kb 那份**（定版）；
2. kb 没有时返回 library（此时 library 就是要被纳入的源）；
3. `translation_target(library 路径)` 必须解析到 **与 canonical 同一个路径**（读写同源）；
4. 定位不到（无 kb 无 library）时返回 ""，而 `translation_target` 必须**原样返回传入路径**
   （绝不因定位失败而丢写入）。
"""
from __future__ import annotations

import json

from paperkb import api as kbapi
from paperkb.config import Roots


def _mk_roots(tmp_path, *, in_kb: bool, in_library: bool, rid: str = "10.1/abc"):
    roots = Roots(data_dir=tmp_path / "data", library_dir=tmp_path / "library",
                  kb_dir=tmp_path / "knowledge_base")
    doc = {"metadata": {"doi": rid}, "paragraphs": []}
    for flag, base in ((in_library, roots.library_dir), (in_kb, roots.kb_dir)):
        if flag:
            d = base / rid.replace("/", "_")
            d.mkdir(parents=True, exist_ok=True)
            (d / "document.json").write_text(json.dumps(doc), encoding="utf-8")
    return roots


def test_canonical_prefers_kb_when_both_exist(tmp_path):
    roots = _mk_roots(tmp_path, in_kb=True, in_library=True)
    kbapi.init_kb(roots)
    hit = kbapi.canonical_doc_json("10.1_abc")
    assert hit, "两侧都有时应返回定版路径"
    assert "knowledge_base" in hit, f"定版必须是 kb 那份，实际 {hit}"


def test_canonical_falls_back_to_library_before_kb_import(tmp_path):
    roots = _mk_roots(tmp_path, in_kb=False, in_library=True)
    kbapi.init_kb(roots)
    hit = kbapi.canonical_doc_json("10.1_abc")
    assert hit and "library" in hit, f"未纳入 kb 时应回退 library，实际 {hit}"


def test_translation_target_equals_canonical(tmp_path):
    """**读写同源**：翻译/复核的写回目标必须与编译读的那份是同一路径。"""
    roots = _mk_roots(tmp_path, in_kb=True, in_library=True)
    kbapi.init_kb(roots)
    lib_doc = roots.library_dir / "10.1_abc" / "document.json"
    canon = kbapi.canonical_doc_json("10.1_abc")
    target = kbapi.translation_target(str(lib_doc))
    assert target == canon, f"写回目标({target}) 必须等于定版({canon})"


def test_translation_target_never_loses_write(tmp_path):
    """定位不到时必须原样返回传入路径（不许因为解析失败而把译文写丢）。"""
    roots = _mk_roots(tmp_path, in_kb=False, in_library=False)
    kbapi.init_kb(roots)
    ghost = tmp_path / "nowhere" / "rid-x" / "document.json"
    assert kbapi.translation_target(str(ghost)) == str(ghost)
