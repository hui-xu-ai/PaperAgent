# -*- coding: utf-8 -*-
r"""只读核对：问答侧的"全文前缀"与**编译请求的前缀**是否逐字节一致（决定缓存能否继承）。

用户模型（2026-09-12）："这些会话继承了文献全文输入 + 编译输出整个初始过程的前缀"。
本脚本用**真实 document.json** 构造两边的开头，直接比对字节。

用法：python tools\dbg_prefix_inherit_check.py [doc_json]
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

DEFAULT_DOC = Path("library/10.1002_adma.202407106/document.json")


def main() -> None:
    doc_json = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DOC
    if not doc_json.is_file():
        raise SystemExit(f"未找到 {doc_json}")

    from paperkb.compile import _prompt_l1
    from paperkb.context import shared_ctx
    from paperkb.doc import read_document

    doc = read_document(doc_json)
    shared = shared_ctx(doc)
    meta = {"title": "T", "year": "2024", "journal": "", "keywords": [], "abstract": ""}
    compile_prompt = _prompt_l1(meta, doc, "")

    print(f"shared_ctx 长度            = {len(shared)} 字符")
    print(f"编译请求 prompt 长度        = {len(compile_prompt)} 字符")
    print(f"编译 prompt 是否以 shared 开头 = {compile_prompt.startswith(shared)}")
    n = len(shared)
    print(f"逐字节比较前 {n} 字符        = {'一致 ✅' if compile_prompt[:n] == shared else '不一致 ❌'}")
    print("\n共享前缀开头 120 字：")
    print("  " + shared[:120].replace("\n", " / "))
    print("\n共享前缀结尾 120 字：")
    print("  " + shared[-120:].replace("\n", " / "))
    print("\n结论：问答侧只要也以该字符串作为 `role=user` 消息的开头，即可继承编译已建立的"
          "提示词缓存（第 1 个会话也不必重付全文全价）。")


if __name__ == "__main__":
    main()
