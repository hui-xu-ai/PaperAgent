#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 审查真实调用实测（P-ENHANCE R07）：对指定论文的 document.json 跑
build_review_items（mid-join 候选 + latex 语法）+ review_items（真实 DeepSeek），
报告修复数/忽略数/token 消耗/耗时。仅测量用，不落盘规则。

用法:
    .venv\\Scripts\\python.exe tools/ai_review_run.py <论文名> [--max-items 50]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paperparse.core.ai_review import build_review_items, review_items
from paperparse.middleware.schema import ArticleDocument


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paper", help="论文名（corpus/baseline/<名>/intermediate/document.json）")
    ap.add_argument("--max-items", type=int, default=50)
    args = ap.parse_args()

    doc_p = Path("learning_workspace/corpus/baseline") / args.paper / "intermediate" / "document.json"
    if not doc_p.exists():
        print("ERROR: 无 document.json: %s" % doc_p, file=sys.stderr)
        return 2
    doc = ArticleDocument.model_validate(json.loads(doc_p.read_text(encoding="utf-8")))

    # 真实 DeepSeek provider（应用侧 llm_service）
    from backend.app.config import get_settings
    from backend.app.services.llm_service import init_llm
    settings = get_settings()
    ai = init_llm({
        "id": "deepseek", "name": "DeepSeek",
        "base_url": settings.deepseek_base_url,
        "model": settings.deepseek_model,
        "api_key": settings.deepseek_api_key,
    })

    items = build_review_items(doc, enable_mid_join=True)
    print("待审项: %d（上限 %d）" % (len(items), args.max_items))
    if not items:
        print("无待审项")
        return 0

    t0 = time.time()
    result = review_items(items, doc, ai=ai, max_items=args.max_items)
    elapsed = round(time.time() - t0, 1)
    usage = getattr(ai, "last_usage", None)
    print(json.dumps({
        "elapsed_sec": elapsed,
        "fixed": result.get("fixed", 0),
        "ignored": result.get("ignored", 0),
        "candidates": len(result.get("candidates", [])),
        "usage": usage,
        "raw_head": (result.get("raw") or "")[:500],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
