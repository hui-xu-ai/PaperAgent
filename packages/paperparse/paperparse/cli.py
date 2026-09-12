#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: skill/src/paperparse/cli.py
功能: 命令行入口（人工使用/调试/AI 工具后端）：
      run / status / audit / render-md / list-tools / help / selftest / version
对外接口: main
版本: v1.0.1 (2026-08-19)
版本历史:
  v1.0.1 接入统一门面 api.py：新增 run/status/audit/render-md/list-tools/help
  v1.0.0 初始版本（selftest + version）
"""
from __future__ import annotations

import argparse
import json
import socket
import sys

from rich.console import Console
from rich.table import Table

from paperparse import __version__
from paperparse.config import load_config
from paperparse.middleware.errors import PaperError, to_envelope

console = Console()


def _check_deps() -> list[tuple[str, bool, str]]:
    """[局部] 检查核心依赖版本"""
    rows: list[tuple[str, bool, str]] = []
    for mod, name in [("pymupdf", "pymupdf"), ("requests", "requests"),
                      ("pydantic", "pydantic"), ("jinja2", "jinja2"),
                      ("rich", "rich"), ("dotenv", "python-dotenv"),
                      ("tqdm", "tqdm")]:
        try:
            m = __import__(mod)
            rows.append((name, True, getattr(m, "__version__", "?")))
        except Exception:
            rows.append((name, False, "未安装"))
    return rows


def _check_mineru_connectivity(cfg) -> tuple[bool, str]:
    """[局部] MinerU 连通性分层诊断：DNS → HTTPS"""
    host = cfg.mineru_base_url.split("//")[-1].split("/")[0]
    try:
        socket.getaddrinfo(host, 443)
    except Exception as e:
        return False, f"DNS 解析失败: {host} ({e})"
    try:
        import requests
        r = requests.get(cfg.mineru_base_url, timeout=10)
        return True, f"可达（HTTP {r.status_code}）"
    except Exception as e:
        return False, f"HTTPS 失败: {e}"


def _emit(obj, as_json: bool) -> None:
    """[局部] 统一输出：JSON 或 rich 打印
    （JSON 用原生 print 单行输出——rich 折行会破坏 JSON 合法性，批量子进程/工具解析依赖）"""
    if as_json:
        print(json.dumps(obj, ensure_ascii=False))
    else:
        console.print(obj)


def cmd_run(args) -> int:
    """[全局] 转换单篇 PDF"""
    import paperparse.api as api
    try:
        result = api.convert_pdf(
            args.pdf, parser=args.parser, dpi=args.dpi,
            calibration_md=args.calibration_md, template=args.template,
            out_dir=args.out_dir, resume=args.resume)
        data = result.model_dump(mode="json")
    except PaperError as exc:
        _emit(to_envelope(exc).model_dump(mode="json"), args.json)
        return 1
    _emit(data, args.json)
    if args.json:
        return 0
    console.print(f"[bold]状态:[/bold] {data['status']}  run_id={data['run_id']}")
    if data["outputs"]["md"]:
        console.print(f"[bold]输出:[/bold] {data['outputs']['md']}")
    for w in data["warnings"]:
        console.print(f"[yellow]警告 {w['code']}:[/yellow] {w['user_message']}")
    if data["error"]:
        console.print(f"[red]错误 {data['error']['code']}:[/red] {data['error']['user_message']}")
    return 0 if data["status"] != "failed" else 1


def cmd_status(args) -> int:
    """[全局] 查询运行状态"""
    import paperparse.api as api
    _emit(api.status(args.run_id, out_dir=args.out_dir), args.json)
    return 0


def cmd_audit(args) -> int:
    """[全局] 审计查询"""
    import paperparse.api as api
    events = api.audit(args.run_id, out_dir=args.out_dir,
                       stage=args.stage, level=args.level)
    if args.json:
        _emit(events, True)
        return 0
    table = Table(title=f"audit {args.run_id}", show_header=True)
    for col in ("时间", "阶段", "类型", "摘要", "token", "耗时ms"):
        table.add_column(col)
    for e in events:
        table.add_row(e["ts"][11:19], e["stage"], e["kind"], e["summary"],
                      str(e["tokens"] or ""), str(e["duration_ms"] or ""))
    console.print(table)
    return 0


def cmd_render_md(args) -> int:
    """[全局] 从 document.json 重渲染"""
    import paperparse.api as api
    try:
        md = api.render_md(args.document, template=args.template)
    except PaperError as exc:
        _emit(to_envelope(exc).model_dump(mode="json"), args.json)
        return 1
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(md)
        console.print(f"已写入 {args.output}（{len(md.encode('utf-8'))} 字节）")
        return 0
    _emit({"markdown": md, "bytes": len(md.encode("utf-8"))}, args.json)
    return 0


def cmd_list_tools(args) -> int:
    """[全局] 工具索引"""
    import paperparse.api as api
    _emit(api.list_tools(), args.json)
    return 0


def cmd_help(args) -> int:
    """[全局] 工具详情"""
    import paperparse.api as api
    try:
        _emit(api.tool_help(args.tool), args.json)
    except PaperError as exc:
        _emit(to_envelope(exc).model_dump(mode="json"), args.json)
        return 1
    return 0


def cmd_selftest(args) -> int:
    """[全局] 环境自检"""
    table = Table(title="PaperAIReader 自检", show_header=True)
    table.add_column("检查项")
    table.add_column("状态")
    table.add_column("详情")
    table.add_row("Python", "OK", sys.version.split()[0])
    all_ok = True
    for name, ok, ver in _check_deps():
        table.add_row(f"依赖 {name}", "OK" if ok else "缺失", ver)
        all_ok = all_ok and ok
    cfg = load_config()
    if cfg.mineru_api_key:
        table.add_row("MinerU API Key", "OK", "已配置")
    else:
        table.add_row("MinerU API Key", "缺失", "请在 .env 配置（本地解析仍可用）")
    ok, msg = _check_mineru_connectivity(cfg)
    table.add_row("MinerU 连通性", "OK" if ok else "不可达", msg)
    console.print(table)
    return 0 if all_ok else 1


def cmd_align(args) -> int:
    """[全局] T-A 段落级文字匹配校准（自动版 vs 手动校准版）"""
    import paperparse.api as api
    try:
        result = api.align_paragraphs(args.auto, args.calib, out_dir=args.out_dir)
    except FileNotFoundError as exc:
        console.print("[red]输入文件不存在: %s[/red]" % exc)
        return 1
    _emit(result, args.json)
    return 0


def cmd_version(args) -> int:
    """[全局] 版本信息"""
    console.print(f"paper-reader-skill v{__version__}")
    return 0


def cmd_process(args) -> int:
    """[全局] ★ 主流程入口（v2.0）：解析 PDF（默认精准解析）→ 翻译+总结 / 只解析"""
    import paperparse.api as api
    try:
        r = api.process_pdf(args.pdf, parser=args.parser, dpi=args.dpi,
                            template=args.template, out_dir=args.out_dir,
                            parse_only=args.parse_only)
    except Exception as exc:
        _emit({"error": str(exc)}, True)
        return 1
    _emit(r, args.json)
    if not args.json:
        if r.get("status") == "error":
            console.print(f"[red]错误:[/red] {r.get('error')}")
            console.print(r.get("note", ""))
            return 0
        console.print(f"[bold]解析完成:[/bold] {r['status']} run_id={r['run_id']} 模式={r.get('mode')}")
        console.print(f"[bold]解析来源:[/bold] {r.get('parse_source', '?')} "
                      f"| LaTeX公式: {r.get('latex_count', 0)} 个 ({'高精度' if r.get('latex') else '低精度'})")
        if r.get("mineru_md"):
            console.print(f"[bold]MinerU 原始:[/bold] {r['mineru_md']}")
        console.print(f"[bold]document.json:[/bold] {r['document_json']}")
        if r.get("mode") == "parse_only":
            console.print(f"[bold]英文版导出:[/bold] {r['export_dir']}（en.md+images+document.json，未翻译）")
        else:
            console.print(f"[bold]prompt 文件:[/bold] {r['prompt_path']} ({r['prompt_chars']} 字符)")
            console.print(f"[bold]下一步:[/bold] {r['note']}")
    return 0


def cmd_query(args) -> int:
    """[全局] 局部查询 document.json 指定段落/章节（不返回全文，供追加问答引用定位）"""
    import paperparse.api as api
    try:
        r = api.query_paragraphs(args.document, para_ids=args.para_id,
                                 section=args.section, include=args.include,
                                 limit_chars=args.limit)
    except Exception as exc:
        _emit({"error": str(exc)}, True)
        return 1
    _emit(r, args.json)
    if not args.json:
        for it in r["items"]:
            console.print(f"[bold]{it['para_id']}[/bold] (idx={it['idx']}, {it['section']}): "
                          f"{it['text'][:120]}")
    return 0


def cmd_batch(args) -> int:
    """[全局] 批量执行（T18/M4）：JSONL 任务队列 → 独立子进程转换 → 汇总"""
    from paperparse.middleware.batch_runner import estimate_budget, load_jobs, run_batch
    try:
        jobs = load_jobs(args.jobs)
    except FileNotFoundError as exc:
        console.print("[red]任务文件不存在: %s[/red]" % exc)
        return 1
    if args.dry_run:
        budget = estimate_budget(jobs)
        _emit(budget, args.json)
        return 0
    results = run_batch(jobs, out_dir=args.out_dir, max_workers=args.workers)
    data = [r.model_dump(mode="json") for r in results]
    _emit(data, args.json)
    if not args.json:
        for r in results:
            mark = "OK " if r.status == "success" else ("~" if r.status == "partial" else "FAIL")
            console.print(f"[bold]{mark}[/bold] {r.job_id}  status={r.status}"
                          + (f"  error={r.error_code}" if r.error_code else "")
                          + (f"  md={r.md_path}" if r.md_path else ""))
    return 0 if all(r.status == "success" for r in results) else 2


def main(argv: list[str] | None = None) -> int:
    """[全局] CLI 入口（argparse 子命令）"""
    parser = argparse.ArgumentParser(prog="paperparse", description="PaperAIReader：英文 PDF → Obsidian Markdown")

    def add_json(sp):
        sp.add_argument("--json", action="store_true", help="JSON 输出（AI 工具用）")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="转换单篇 PDF → Obsidian MD")
    p.add_argument("pdf", help="PDF 路径")
    p.add_argument("--parser", choices=["auto", "pymupdf", "mineru"], default="auto")
    p.add_argument("--dpi", type=int, default=None)
    p.add_argument("--calibration-md", default=None)
    p.add_argument("--template", default=None)
    p.add_argument("--out-dir", default="output")
    p.add_argument("--resume", action="store_true")
    add_json(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("process", help="★ 主流程：解析 PDF（默认高精度 v4，需密钥）→ 翻译+总结 / --parse-only 只解析")
    p.add_argument("pdf", help="PDF 路径")
    p.add_argument("--parser", choices=["mineru", "mineru-v4", "pymupdf", "auto"], default="mineru-v4",
                   help="解析通道：mineru-v4=付费云端高精度(需MINERU_API_KEY,默认) | mineru=v1免费云端低精度(无需密钥) | pymupdf=本地(无LaTeX) | auto=本地")
    p.add_argument("--dpi", type=int, default=None)
    p.add_argument("--template", default=None)
    p.add_argument("--out-dir", default="output")
    p.add_argument("--parse-only", action="store_true",
                   help="只解析模式：导出英文版 markdown 全套（en.md+images+document.json），不翻译不总结")
    add_json(p)
    p.set_defaults(func=cmd_process)

    p = sub.add_parser("status", help="查询运行状态")
    p.add_argument("run_id")
    p.add_argument("--out-dir", default="output")
    add_json(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("audit", help="审计监督查询")
    p.add_argument("run_id")
    p.add_argument("--out-dir", default="output")
    p.add_argument("--stage", default=None)
    p.add_argument("--level", default=None)
    add_json(p)
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("render-md", help="从 document.json 重渲染")
    p.add_argument("document")
    p.add_argument("--template", default=None)
    p.add_argument("--output", default=None)
    add_json(p)
    p.set_defaults(func=cmd_render_md)

    p = sub.add_parser("list-tools", help="工具索引")
    add_json(p)
    p.set_defaults(func=cmd_list_tools)

    p = sub.add_parser("help", help="工具详情")
    p.add_argument("tool")
    add_json(p)
    p.set_defaults(func=cmd_help)

    p = sub.add_parser("selftest", help="环境自检")
    p.set_defaults(func=cmd_selftest)

    p = sub.add_parser("align", help="T-A 段落级匹配校准（自动版 vs 手动校准版）")
    p.add_argument("auto", help="自动版 Markdown 路径")
    p.add_argument("calib", help="手动校准版 Markdown 路径")
    p.add_argument("--out-dir", default="work/para_align", help="报告输出目录")
    add_json(p)
    p.set_defaults(func=cmd_align)

    p = sub.add_parser("version", help="版本信息")
    p.set_defaults(func=cmd_version)

    p = sub.add_parser("query", help="局部查询 document.json 指定段落/章节（不返回全文）")
    p.add_argument("document", help="document.json 路径")
    p.add_argument("--para-id", action="append", help="段落 ID（可多次指定）")
    p.add_argument("--section", help="按章节名过滤")
    p.add_argument("--include", choices=["en", "zh", "both"], default="en",
                   help="返回语言（默认 en 原文）")
    p.add_argument("--limit", type=int, default=2000, help="每段返回上限字符数")
    add_json(p)
    p.set_defaults(func=cmd_query)

    p = sub.add_parser("batch", help="批量执行（JSONL 任务队列 → 独立子进程）")
    p.add_argument("jobs", help="任务队列 JSONL 路径")
    p.add_argument("--out-dir", default="output")
    p.add_argument("--workers", type=int, default=4, help="并发上限（1=串行）")
    p.add_argument("--dry-run", action="store_true", help="仅预算预估（不执行）")
    add_json(p)
    p.set_defaults(func=cmd_batch)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
