# -*- coding: utf-8 -*-
"""复核写回目标守卫：确认修改必须写进**定版（kb）**，与编译/翻译同源（方案 A）。

为什么单独守这条（2026-09-16）：
· 用户要求"解析 → 校核 → 确认唯一版本 → 编译与翻译都用这一份"；
· 复核（`review_service.apply_choice`）此前**只写 library**，而编译读 kb ⇒ 复核结果对编译不可见；
· 现改为经 `paperkb.api.translation_target` 解析写回目标（= 定版）。
真实链路验证的阻碍：需要**双通道解析真的产出差异点**的论文才会走到该分支（实测某篇 pending=0 走不到），
故用本单测钉住"写回目标解析"这一环，真实分支待有用例论文时再端到端复测。
"""
from __future__ import annotations

import json

from paperkb import api as kbapi
from paperkb.config import Roots


def test_review_writeback_target_is_canonical(tmp_path):
    """复核写回目标必须等于定版（kb）；这是"复核对编译可见"的前提。"""
    roots = Roots(data_dir=tmp_path / "data", library_dir=tmp_path / "library",
                  kb_dir=tmp_path / "knowledge_base")
    rid = "10.1002_adma.202407106"
    for base in (roots.library_dir, roots.kb_dir):
        d = base / rid
        d.mkdir(parents=True, exist_ok=True)
        (d / "document.json").write_text(json.dumps({"metadata": {"doi": rid},
                                                     "paragraphs": []}), encoding="utf-8")
    kbapi.init_kb(roots)

    lib_doc = roots.library_dir / rid / "document.json"
    canon = kbapi.canonical_doc_json(rid)
    target = kbapi.translation_target(str(lib_doc))
    assert "knowledge_base" in canon, f"定版应在 kb，实际 {canon}"
    assert target == canon, f"复核/翻译写回目标({target}) 必须等于定版({canon})"


def test_review_writeback_falls_back_when_not_imported(tmp_path):
    """尚未纳入 kb（只有 library）⇒ 写回 library，绝不丢用户改动。"""
    roots = Roots(data_dir=tmp_path / "data", library_dir=tmp_path / "library",
                  kb_dir=tmp_path / "knowledge_base")
    rid = "10.1002/newpaper"
    d = roots.library_dir / rid
    d.mkdir(parents=True, exist_ok=True)
    doc = d / "document.json"
    doc.write_text(json.dumps({"metadata": {"doi": rid}, "paragraphs": []}), encoding="utf-8")
    kbapi.init_kb(roots)
    assert kbapi.translation_target(str(doc)) == str(doc)
