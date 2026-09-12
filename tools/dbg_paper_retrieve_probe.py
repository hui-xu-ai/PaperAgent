# -*- coding: utf-8 -*-
r"""只读探针：复现"文献提问吃不到正文"的检索环节。

机理假设：`ChatService._retrieve` 用 `_tokenize(question) ∩ 段落词集` 做**逐词交集**，
中文问题对**未翻译的英文段落**交集为空 → 返回 []；而 `ask_stream` 里"编译笔记补充"排在
`if not retrieved:` 兜底**之前** → 笔记一旦命中，摘要兜底就不执行 ⇒ 模型只拿到
题名 + 章节索引（段落 ID）+ 六维笔记，没有正文。

用法（在仓库根跑）：python tools\dbg_paper_retrieve_probe.py
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, str(Path("backend").resolve()))   # app.* 可导入

DOC = Path("library/10.1002_adma.202407106/document.json")
QUESTIONS = [
    "这篇文献的创新点是什么",
    "这篇文献采用了什么方法制作的IPMC驱动器",
    "请问磁响应来源是什么",
    "What is the magnetic responsive mechanism?",
    "LIG electrode fabrication",
]


def main() -> None:
    from app.services.chat_service import _tokenize
    from paperparse.core.document_builder import load_document

    if not DOC.is_file():
        raise SystemExit(f"未找到 {DOC}（该篇可能还没解析）")
    doc = load_document(str(DOC))
    paras = [p for p in doc.paragraphs
             if not p.is_heading and not p.is_caption and (p.text_en or p.text_zh)]
    print(f"document.json: 段落总数={len(doc.paragraphs)} 参与匹配={len(paras)}")
    n_zh = sum(1 for p in paras if (p.text_zh or "").strip())
    print(f"其中带中文译文(text_zh)的段落 = {n_zh}（0 ⇒ 中文问题不可能靠词交集命中）\n")

    # 预建段落词集（与 _retrieve 同逻辑）
    para_tokens = [set(_tokenize(p.text_en or "")) | set(_tokenize(p.text_zh or "")) for p in paras]

    for q in QUESTIONS:
        qs = set(_tokenize(q))
        hit_counts = [len(qs & pt) for pt in para_tokens]
        n_hit = sum(1 for h in hit_counts if h > 0)
        best = sorted((h for h in hit_counts if h > 0), reverse=True)[:5]
        print(f"问题: {q!r}")
        print(f"  问题词元={len(qs)} 个 → 命中段落数 = {n_hit}，最高命中词数 = {best}")
        print(f"  ⇒ _retrieve 返回 {'空（无正文进上下文）' if n_hit == 0 else '非空'}")


if __name__ == "__main__":
    main()
