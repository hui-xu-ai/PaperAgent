#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线解析（P-ENHANCE R01）：本地 stitch + 指定 MinerU full.md 融合，不调用 MinerU API。

用途：语料基线产物生成（corpus/baseline/<名>/paper.md + document.json）。
原理：替换 Orchestrator 的 StageParse 为 OfflineParse——本地行级解析后，把备份的
MinerU full.md 复制为 intermediate/mineru_v1.md（content_list.json 同存，供 R02
S1.5 布局骨架使用），parser 参数传 "mineru-v4" 使 S3.5 触发 build_mineru_doc 融合。

用法:
    .venv\\Scripts\\python.exe tools/parse_offline.py <pdf> --full-md <full.md> [--content-list <content_list.json>] [--out-dir corpus/baseline] [--template recognized]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 项目根（.env 读取）

from paperparse.config import load_config
from paperparse.core.document_builder import output_dir_name
from paperparse.core.pymupdf_fallback import extract_blocks
from paperparse.middleware.orchestrator import Orchestrator
from paperparse.middleware.stages import (
    StageBuild,
    StageCalibrate,
    StageFigures,
    StageLayout,
    StageLayoutSkeleton,
    StageMetadata,
    StageParse,
    StageRender,
    StageStitch,
    StageValidate,
)


class OfflineParse(StageParse):
    """S1 离线版：本地行级解析 + 指定 MinerU full.md/content_list.json 落入 intermediate/。"""

    name = "S1_parse"

    def __init__(self, full_md: str | Path | None = None,
                 content_list: str | Path | None = None):
        super().__init__()
        self.full_md = Path(full_md) if full_md else None
        self.content_list = Path(content_list) if content_list else None

    def run(self, ctx) -> None:
        ctx.blocks = extract_blocks(ctx.pdf_path)
        ctx.parser_used = "pymupdf"
        ctx.auditor.log_event(
            self.name, "output",
            summary="offline pymupdf blocks=%d pages=%d" % (
                len(ctx.blocks.blocks), ctx.blocks.pages))
        if self.full_md and self.full_md.exists():
            shutil.copy2(self.full_md, ctx.intermediate / "mineru_v1.md")
            if self.content_list and self.content_list.exists():
                shutil.copy2(self.content_list,
                             ctx.intermediate / "mineru_content_list.json")
            ctx.auditor.log_event(
                self.name, "output",
                summary="离线注入 MinerU 产物：%s (+content_list=%s)" % (
                    self.full_md.name, self.content_list is not None))


def main() -> int:
    ap = argparse.ArgumentParser(description="离线解析：本地 + MinerU 备份产物融合（零 API）")
    ap.add_argument("pdf", help="源 PDF 路径")
    ap.add_argument("--full-md", help="MinerU full.md 路径（备份产物）")
    ap.add_argument("--content-list", help="MinerU content_list.json 路径（可选）")
    ap.add_argument("--out-dir", default="corpus/baseline", help="输出根目录")
    ap.add_argument("--template", default="recognized", help="渲染模板名")
    ap.add_argument("--dpi", type=int, default=None, help="图片 DPI（默认引擎配置 300）")
    ap.add_argument("--ai-review", action="store_true",
                    help="启用 S7.5 AI 审查（FakeAI 占位；真实 DeepSeek 由应用侧注入）")
    args = ap.parse_args()

    pdf = Path(args.pdf)
    if not pdf.exists():
        print("ERROR: PDF 不存在: %s" % pdf, file=sys.stderr)
        return 2

    # R10：重跑前清理目标目录的旧 images/ 与 intermediate（防图片残留计数虚高；
    # 全新离线跑非 resume，无断点依赖）
    target = Path(args.out_dir) / output_dir_name(None, pdf)
    if target.exists():
        for sub in ("images", "intermediate"):
            p = target / sub
            if p.exists():
                shutil.rmtree(p, ignore_errors=True)

    cfg = load_config()
    orch = Orchestrator(cfg)
    stages = [
        StageValidate(), OfflineParse(args.full_md, args.content_list),
        StageLayoutSkeleton(), StageLayout(), StageStitch(), StageCalibrate(),
        StageFigures(), StageMetadata(), StageBuild(),
    ]
    if args.ai_review:
        import os
        from paperparse.llm.client import FakeAI, set_ai
        from paperparse.middleware.stages import StageAIReview
        os.environ["RULES_AI_REVIEW"] = "1"
        set_ai(FakeAI())
        stages.append(StageAIReview())
    stages.append(StageRender())
    orch._stages = stages
    result = orch.run(pdf, parser="mineru-v4", dpi=args.dpi,
                      out_dir=args.out_dir, template=args.template)
    out = {
        "status": result.status,
        "run_id": result.run_id,
        "md": result.outputs.md,
        "document_json": result.outputs.document_json,
        "images_dir": result.outputs.images_dir,
        "parse_source": result.stats.parse_source if result.stats else None,
        "latex_count": result.stats.latex_count if result.stats else 0,
        "pages": result.stats.pages if result.stats else 0,
        "duration_sec": result.stats.duration_sec if result.stats else 0,
        "warnings": [w.user_message for w in result.warnings],
        "error": str(result.error) if result.error else None,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if result.status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
