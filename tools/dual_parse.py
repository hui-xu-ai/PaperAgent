#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: tools/dual_parse.py
功能: P11 双 PDF 解析对照 CLI：mineru（主）+ PaddleOCR-VL-1.6（辅）双通道
      解析 → 对齐/差异分类报告 → 融合（补缺+冲突 review）→ 清洗规则挖掘入库
用法:
  .venv\\Scripts\\python.exe tools/dual_parse.py <pdf> [--out work/dual]
      [--parser mineru-v4|mineru] [--corpus] [--persist-rules] [--rules-dir <dir>]
      [--min-len 40] [--no-parse-mineru] [--no-parse-paddleocr]
说明:
  --corpus        学习语料同步落盘 learning_workspace/corpus/dual/<pdf_stem>/
  --persist-rules 挖掘的清洗规则入库（rule_library learned 层；默认仅产出候选文件）
  --no-parse-*    跳过对应通道的云端解析（复用 work/dual/<stem>/ 已有 blocks）
"""
import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paperparse.api import dual_parse  # noqa: E402
from paperparse.middleware.schema import ParserBlocks  # noqa: E402


def _load_blocks(path: Path) -> ParserBlocks:
    data = json.loads(path.read_text(encoding="utf-8"))
    return ParserBlocks(source="mineru", pages=0, blocks=data)


def main() -> int:
    ap = argparse.ArgumentParser(description="P11 双 PDF 解析对照")
    ap.add_argument("pdf", help="PDF 路径")
    ap.add_argument("--out", default="work/dual", help="产物根目录（默认 work/dual）")
    ap.add_argument("--parser", default="mineru-v4", choices=["mineru-v4", "mineru"],
                    help="mineru 通道（默认 mineru-v4 高精度）")
    ap.add_argument("--corpus", action="store_true",
                    help="学习语料落盘 learning_workspace/corpus/dual/<pdf_stem>/")
    ap.add_argument("--persist-rules", action="store_true",
                    help="挖掘规则入库（rule_library learned 层）")
    ap.add_argument("--rules-dir", default=None, help="规则库目录（默认 rule_library 外部根）")
    ap.add_argument("--min-len", type=int, default=40, help="补缺门控最小文本长度")
    ap.add_argument("--ai-review", action="store_true",
                    help="矛盾项先调便宜模型（硅基流动 V4-Flash）仲裁")
    ap.add_argument("--no-parse-mineru", action="store_true", help="跳过 mineru 云端解析")
    ap.add_argument("--no-parse-paddleocr", action="store_true", help="跳过 paddleocr 云端解析")
    args = ap.parse_args()

    pdf = Path(args.pdf)
    if not pdf.exists():
        print("ERROR: PDF 不存在: %s" % pdf)
        return 2
    out = Path(args.out) / pdf.stem
    out.mkdir(parents=True, exist_ok=True)

    r = dual_parse(str(pdf), parser=args.parser, out_dir=args.out,
                   corpus_dir="learning_workspace/corpus/dual" if args.corpus else None,
                   persist_rules=args.persist_rules, rules_dir=args.rules_dir,
                   min_len=args.min_len, ai_review=args.ai_review,
                   skip_mineru=args.no_parse_mineru,
                   skip_paddleocr=args.no_parse_paddleocr)
    if r["status"] != "success":
        print("ERROR: %s" % r.get("error"))
        return 1
    print("双通道解析完成: %s" % r["pdf"])
    print("  对齐: %s" % json.dumps(r["stats"]["dual"], ensure_ascii=False))
    print("  融合: %s" % json.dumps(r["stats"]["fuse"], ensure_ascii=False))
    print("  AI预校验: %s" % json.dumps(r["stats"]["ai_review"], ensure_ascii=False))
    print("  规则: mined=%d persisted=%d" % (r["stats"]["rules"]["mined"],
                                             r["stats"]["rules"]["persisted"]))
    print("  产物:")
    for k, v in r["paths"].items():
        if v:
            print("    %-16s %s" % (k, v))
    if r["rules_mined"]:
        print("  挖掘规则候选:")
        for rule in r["rules_mined"]:
            print("    [%s/%s] conf=%.2f %s -> %s" % (
                rule["category"], rule["pattern_type"], rule["confidence"],
                rule["pattern"][:40], rule.get("replacement", "")[:40]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
