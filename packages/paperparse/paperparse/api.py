#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: skill/src/paperparse/api.py
功能: ★ 统一中间调用文件（facade）：AI 与 CLI 的唯一入口。
      所有功能包对外只经本模块维护；内部升级不影响接口（契约版本见 schema.INTERFACE_VERSION）
对外接口: convert_pdf / status / audit / render_md / list_tools / tool_help / selftest
版本: v1.0.0 (2026-08-19)
版本历史:
  v1.0.0 初始版本
"""
from __future__ import annotations

import json
from pathlib import Path

from paperparse.config import asset_root, load_config
from paperparse.core.markdown_render import render as render_md_impl
from paperparse.core.document_builder import load_document
from paperparse.middleware.audit import Auditor
from paperparse.middleware.errors import PaperError
from paperparse.middleware.orchestrator import Orchestrator
from paperparse.middleware.schema import PipelineResult

__all__ = ["convert_pdf", "status", "audit", "render_md",
           "list_tools", "tool_help", "selftest", "align_paragraphs",
           "run_batch", "query_paragraphs", "process_pdf"]

TOOLS_DIR = asset_root() / "tools"


def run_batch(jobs: list[dict], out_dir: str = "output",
              max_workers: int = 4, dry_run: bool = False) -> list[dict]:
    """[全局] T18/M4 批量执行：任务队列 → 独立子进程转换 → JobSummary 汇总（上下文隔离）

    参数:
        jobs: 任务列表 [{"job_id","pdf","parser"?,"calibration_md"?,"template"?}]
        out_dir: 输出根目录（每任务 <out_dir>/<job_id>/）
        max_workers: 并发上限（1=串行）
        dry_run: True=仅预算预估（返回预算预估 dict）
    返回:
        JobSummary dict 列表（dry_run 时返回预算预估 dict）
    """
    from paperparse.middleware.batch_runner import estimate_budget, run_batch as _rb
    if dry_run:
        return [estimate_budget(jobs)]
    results = _rb(jobs, out_dir=out_dir, max_workers=max_workers)
    return [r.model_dump(mode="json") for r in results]


def align_paragraphs(auto_md: str, calib_md: str,
                     out_dir: str = "work/para_align") -> dict:
    """[全局] T-A 段落级文字匹配校准（自我学习"眼睛"）：自动版 vs 手动校准版
    段落对齐 → 对齐报告（匹配/错位/缺段/多段/差异定位到句子与字符）+
    拼接算法缺陷清单（供 T-B 修复与 AI 调试经验学习）

    参数:
        auto_md: 自动版 Markdown（如 output/<DOI>/paper.md）
        calib_md: 手动校准版 Markdown（如 paper_手动校准版.md）
        out_dir: 报告输出目录（align_report.json/.txt + defect_list.json）
    返回:
        {"stats": {...}, "defects": [...], "json_report": str, "text_report": str}
    报错:
        FileNotFoundError: 任一输入文件缺失（上层转为 PAPER-0501）
    """
    from paperparse.core.para_align import align_docs, parse_md_paras
    from paperparse.core.para_align_report import (
        defect_list, render_json_report, render_text_report)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    auto = parse_md_paras(auto_md)
    calib = parse_md_paras(calib_md)
    rep = align_docs(auto, calib)
    defs = defect_list(rep, auto, calib)
    jp = render_json_report(rep, out / "align_report.json")
    tp = render_text_report(rep, out / "align_report.txt")
    dl = out / "defect_list.json"
    dl.write_text(json.dumps({"stats": rep.stats, "defects": defs},
                             ensure_ascii=False, indent=2), encoding="utf-8")
    return {"stats": rep.stats, "defects": defs,
            "json_report": str(jp), "text_report": str(tp),
            "defect_list": str(dl)}


def convert_pdf(pdf_path: str, *, parser: str = "auto", dpi: int | None = None,
                calibration_md: str | None = None, template: str | None = None,
                out_dir: str = "output", resume: bool = False,
                run_id: str | None = None) -> PipelineResult:
    """[全局] 单篇 PDF → Obsidian Markdown（主工具）

    参数:
        pdf_path: PDF 路径
        parser: auto(默认，本地解析) | pymupdf | mineru
        dpi: 图片渲染分辨率（默认 300）
        calibration_md: 校准 MD 路径（默认自动探测 input/、tests/samples/、同目录）
        template: 模板名（obsidian_bilingual / obsidian_bilingual_alt / plain）
        out_dir: 输出根目录（默认 output/）
        resume: 断点续跑
        run_id: 指定运行 ID
    返回:
        PipelineResult（status: success/partial/failed）
    报错:
        参数非法 → PAPER-0501（由 pydantic/调度层包装，不裸抛）
    """
    cfg = load_config()
    return Orchestrator(cfg).run(
        pdf_path, parser=parser, dpi=dpi, calibration_md=calibration_md,
        template=template, out_dir=out_dir, resume=resume,
        run_id=run_id)


def process_pdf(pdf_path: str, *, parser: str = "mineru-v4", dpi: int | None = None,
                template: str | None = None, out_dir: str = "output",
                run_id: str | None = None, parse_only: bool = False) -> dict:
    """[全局] ★ 主流程工具（v2.2 解析监督版）：
    解析 PDF → document.json。解析通道（默认高精度需密钥，询问用户后可换）：
      mineru-v4 = 云端付费（需 MINERU_API_KEY，**高精度**）——默认
      mineru     = v1 免费云端（无需密钥，**低精度**，仅显式选择）
      pymupdf   = 本地（无 LaTeX）
    监督机制：云端解析失败 → **中止并返回错误**（不静默降级本地），由 agent 反馈用户选择。

    流程:
      1. 通道校验：mineru-v4 且未配置 MINERU_API_KEY → 立即报错返回（提示可用 v1 免费/本地）；
      2. 解析（云端失败中止，错误信息含原因）；
      3. parse_only=True（解析模式）：导出 en.md + images + document.json 全套（0 AI token）；
         parse_only=False（默认模式）：再生成翻译 prompt 文件，交 agent 一次性翻译+总结。

    参数:
        pdf_path: PDF 路径
        parser: mineru(v1免费) | mineru-v4(需key) | pymupdf(本地) | auto(本地)
        dpi: 图片渲染分辨率
        template: 模板名
        out_dir: 输出根目录（默认 output/）
        run_id: 指定运行 ID
        parse_only: True=只解析（导出英文版全套，不翻译）；False=默认模式（解析+翻译+总结）
    返回:
        {"status", "run_id", "mode", "document_json", "prompt_path", "prompt_chars",
         "export_dir", "error", "note"}
    """
    from paperparse.config import load_config as _lc

    # 1) 通道校验（v2.1 监督）：仅 mineru-v4 需要密钥；mineru（v1 免费）无需密钥
    if parser == "mineru-v4" and not _lc().mineru_api_key:
        return {"status": "error", "run_id": None, "mode": "default" if not parse_only else "parse_only",
                "document_json": None, "prompt_path": None, "prompt_chars": 0,
                "export_dir": None,
                "error": "未配置 MINERU_API_KEY。mineru-v4 精准解析需要密钥；"
                         "可改用 mineru（v1 免费云端，无需密钥）或 pymupdf（本地，无 LaTeX）。",
                "note": "解析未执行；请选择解析通道或配置密钥后重试。"}

    # 2) 解析（云端失败会中止并带原因返回，不静默降级）
    result = convert_pdf(pdf_path, parser=parser, dpi=dpi, template=template,
                         out_dir=out_dir, run_id=run_id)
    if result.status != "success":
        detail = getattr(result.error, "detail", None) or str(result.error)
        return {"run_id": result.run_id, "status": result.status,
                "mode": "default" if not parse_only else "parse_only",
                "document_json": None, "prompt_path": None,
                "prompt_chars": 0, "export_dir": None,
                "error": "解析失败：%s" % detail,
                "note": ("请选择：①重试 ②更换解析通道（mineru v1 免费 / mineru-v4 需密钥 / "
                         "pymupdf 本地） ③检查网络/配额。未获用户确认前不继续。")}
    doc_json = Path(result.outputs.document_json).resolve()

    # v2.2 解析来源反馈：实际生效通道 + LaTeX 统计（让调用方明确知道精度）
    parse_source, latex_count, mineru_md = _parse_source_info(str(doc_json), parser)

    # 3) 解析模式：导出英文版全套（不翻译不总结，0 AI token）
    if parse_only:
        ex = _export_parse_only(str(doc_json), out_root=out_dir)
        return {"status": "success", "run_id": result.run_id, "mode": "parse_only",
                "document_json": str(doc_json),
                "prompt_path": None, "prompt_chars": 0,
                "export_dir": ex["output_dir"],
                "error": None,
                "parse_source": parse_source, "latex": latex_count > 0,
                "latex_count": latex_count, "mineru_md": mineru_md,
                "note": "只解析模式完成：en.md + images + document.json 已导出（未翻译未总结）。"}

    # 4) 默认模式：返回 document_json（翻译/总结由知识库流水线 paperkb 统一处理，
    #    不再生成网页往返 prompt——2026-08-27 瘦身移除 generate_web_prompt）
    return {"status": "success", "run_id": result.run_id, "mode": "default",
            "document_json": str(doc_json),
            "prompt_path": None, "prompt_chars": 0, "export_dir": None,
            "error": None,
            "parse_source": parse_source, "latex": latex_count > 0,
            "latex_count": latex_count, "mineru_md": mineru_md,
            "note": "解析完成。翻译/总结由知识库流水线（paperkb）统一处理。"}


def _parse_source_info(document_json: str, parser: str) -> tuple[str, int, str | None]:
    """[局部] 解析来源反馈：统计 document.json 的 LaTeX($) 数量并判定实际生效通道。

    返回:
        (parse_source, latex_count, mineru_md)
          parse_source: "mineru"(v1免费) | "mineru-v4" | "pymupdf-local" | "auto-local"
          latex_count: document.json 中 $ 总数（>0 表示 LaTeX 公式保留，高精度）
          mineru_md: mineru_v1.md 路径（云端解析时）或 None
    """
    from pathlib import Path
    import json as _json
    doc_p = Path(document_json)
    mineru_md = doc_p.parent / "mineru_v1.md"
    has_mineru = mineru_md.exists()
    latex_count = 0
    try:
        d = _json.load(open(doc_p, encoding="utf-8"))
        latex_count = sum((p.get("text_en") or "").count("$")
                          for p in d.get("paragraphs", []))
    except Exception:
        latex_count = 0
    if has_mineru:
        parse_source = "mineru-v4" if parser == "mineru-v4" else "mineru"
    else:
        parse_source = "pymupdf-local" if parser in ("pymupdf", "auto") else ("%s(云端未产出)" % parser)
    return parse_source, latex_count, str(mineru_md) if has_mineru else None


def _export_parse_only(document_json: str, out_root: str = "output") -> dict:
    """[局部] 只解析模式导出：en.md + images + document.json 全套（无 AI 内容，0 AI token）。

    解析产物（document.json/images/audit）已位于 out_root/<DOI>/ 下，故就地补写 en.md 即可，
    不复制任何文件（避免 out_root 与解析目录重叠时 copytree 源=目标 报 WinError 32）。
    """
    from pathlib import Path
    from paperparse.core.document_builder import output_dir_name
    from paperparse.core.markdown_render import render_variant

    doc = load_document(document_json)
    json_path = Path(document_json).resolve()
    doi_dir = output_dir_name(doc.metadata.doi, doc.metadata.source_pdf)
    # 2026-08-26 修复：out_dir = document.json 所在篇目录（兼容 intermediate 中间层；
    # 原 parent.parent 在无 intermediate 时错写 <out_root>/ 顶层 → 顶层 <DOI>.en.md）
    _d = json_path.parent
    if _d.name == "intermediate":
        _d = _d.parent
    out_dir = _d            # <out_root>/<DOI>
    out_dir.mkdir(parents=True, exist_ok=True)

    en_md = out_dir / ("%s.en.md" % doi_dir)
    en_md.write_text(render_variant(doc, "recognized"), encoding="utf-8")

    img_dest = out_dir / "images"
    return {"output_dir": str(out_dir), "en_md": str(en_md),
            "images": str(img_dest) if img_dest.exists() else "",
            "document_json": str(json_path)}


def status(run_id: str, out_dir: str = "output") -> dict:
    """[全局] 查询运行状态（扫描 out_dir 下所有 audit/<run_id>）

    返回:
        {run_id, found, audit_dirs, event_count, last_event}
    """
    found: list[str] = []
    last = None
    count = 0
    for p in Path(out_dir).glob("*/audit/%s/events.jsonl" % run_id):
        found.append(str(p.parent))
        try:
            lines = p.read_text(encoding="utf-8").strip().splitlines()
            count += len(lines)
            if lines:
                last = json.loads(lines[-1])
        except OSError:
            continue
    return {"run_id": run_id, "found": bool(found), "audit_dirs": found,
            "event_count": count, "last_event": last}


def audit(run_id: str, out_dir: str = "output",
          stage: str | None = None, level: str | None = None) -> list[dict]:
    """[全局] 监督查询：返回审计事件（摘要级，不含文献全文）"""
    events: list[dict] = []
    for p in Path(out_dir).glob("*/audit/%s/events.jsonl" % run_id):
        auditor = Auditor(run_id, p.parent)
        evs = auditor.load_from_disk()
        for e in evs:
            if stage and e.stage != stage:
                continue
            if level and e.kind != level:
                continue
            events.append(e.model_dump(mode="json"))
    return events


def render_md(document_json: str, template: str = "obsidian_bilingual") -> str:
    """[全局] 从 document.json 重渲染（翻译/总结写回后调用）"""
    doc = load_document(document_json)
    return render_md_impl(doc, template=template)


def query_paragraphs(document_json: str, para_ids: list[str] | None = None,
                     section: str | None = None, include: str = "en",
                     limit_chars: int = 2000) -> dict:
    """[全局] 局部查询：按段号/章节返回 document.json 指定段落文本片段
    （默认只回英文、限长截断，**不返回全文**；供追加问答时定位引用段落，避免重读全文）

    参数:
        document_json: document.json 路径
        para_ids: 段落 ID 列表（如 ["P008","P012"]）
        section: 章节名过滤（精确匹配 p.section）
        include: en/zh/both（默认 en，只回英文原文）
        limit_chars: 每段返回上限字符数（默认 2000）
    返回:
        {"found", "items": [{idx, para_id, section, is_heading, text}], "sections"}
    """
    doc = load_document(document_json)
    items: list[dict] = []
    for i, p in enumerate(doc.paragraphs):
        if para_ids and p.para_id not in para_ids:
            continue
        if section and (p.section or "") != section:
            continue
        parts = []
        if include in ("en", "both"):
            parts.append(p.text_en or "")
        if include in ("zh", "both") and p.text_zh:
            parts.append(p.text_zh)
        t = "\n".join(parts).strip()
        if len(t) > limit_chars:
            t = t[:limit_chars] + "…(截断)"
        items.append({"idx": i, "para_id": p.para_id, "section": p.section or "",
                      "is_heading": p.is_heading, "text": t})
        if len(items) >= 50:
            break
    sections = sorted({(p.section or "") for p in doc.paragraphs if p.section})
    return {"found": len(items), "items": items, "sections": sections,
            "note": "局部片段（非全文），追加问答引用时请用 para_id/idx"}


def list_tools(scope: str = "core") -> list[dict]:
    """[全局] 可用工具列表（渐进式加载索引：每工具仅名称/描述/成本）

    scope（v2.0）:
      core     = 默认/解析模式工具（仅 process_pdf）——默认值，AI 零选择
      all      = core + advanced（高级模式；读 SKILL.advanced.md 后按需调用）
      dev 工具（audit/status/selftest/measure/study/m5/render_md/convert_pdf）永不返回（help 仍可查）
    """
    tools = []
    if TOOLS_DIR.exists():
        for p in sorted(TOOLS_DIR.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            usage = data.get("usage", "advanced")
            if usage == "dev":
                continue
            if scope == "core" and usage != "core":
                continue
            tools.append({
                "name": data.get("name", p.stem),
                "usage": usage,
                "description": data.get("description", ""),
                "when_to_use": data.get("when_to_use", ""),
                "cost_estimate": data.get("cost_estimate", ""),
                "errors": data.get("errors", []),
            })
    return tools


def process_pdf_v2(pdf_path: str, *, md_path: str | None = None,
                   md_text: str | None = None, out_dir: str = "output/v2",
                   paddle: bool = True, ai_review: bool = True,
                   third_decide: bool = True, ai_synthesis: bool = True,
                   provider=None, run_id: str | None = None,
                   paddle_blocks_path: str | None = None,
                   sf_ocr: bool = False,
                   cancel_check=None, on_wait=None) -> dict:
    """[全局] ★ P14 管线门面：本地骨架（权威边界）+ mineru full.md（文本基底）
    + 拼接修复 + 双通道验证 + 段落级字符仲裁 → 修复后 markdown + document.json。

    md_path/md_text 二选一（mineru v4 full.md）；paddle=False 时纯 M1-M6
    （本地骨架 + 修复，不调 paddleocr/AI）；paddle_blocks_path 复用既有
    paddleocr blocks.json（云 API 队列满/离线时）。
    **third_decide**（PARSE_THIRD_DECIDE：第三信号 PDF 文本层直接裁决，默认开）/
    **ai_synthesis**（PARSE_AI_SYNTHESIS：AI 综合建议，默认开）必须在此显式透传——
    ★2026-09-17 修：门面此前**没有这两个形参**，而 `engine_service._parse_pdf_dual`
    一直在传 ⇒ `TypeError: unexpected keyword argument 'third_decide'` 被上层
    `except` 吞成"双通道解析异常，降级单通道" ⇒ **真机解析自 9/16 起一直走降级链**
    （第三信号/AI 综合建议/参考文献闸门在生产里从未生效）。守卫见
    `backend/tests/test_engine_service.py::test_engine_kwargs_accepted_by_api_facade`。
    sf_ocr/cancel_check/on_wait：P15 透传底层管线（sf_ocr 已弃用仅 debug；
    cancel_check/on_wait 官方云队列等待机制）。实现见
    paperparse.core.p14_pipeline.process_pdf_v2。
    """
    from paperparse.core.p14_pipeline import process_pdf_v2 as _run
    return _run(pdf_path, md_path=md_path, md_text=md_text, out_dir=out_dir,
                paddle=paddle, ai_review=ai_review,
                third_decide=third_decide, ai_synthesis=ai_synthesis,
                provider=provider,
                run_id=run_id, paddle_blocks_path=paddle_blocks_path,
                sf_ocr=sf_ocr, cancel_check=cancel_check, on_wait=on_wait)


def tool_help(name: str) -> dict:
    """[全局] 工具详情（渐进式加载：仅加载指定工具的完整 schema）"""
    if not TOOLS_DIR.exists():
        raise PaperError("PAPER-0501", stage="tool_help",
                         detail={"reason": "tools 目录不存在", "path": str(TOOLS_DIR)})
    p = TOOLS_DIR / ("%s.json" % name)
    if not p.exists():
        raise PaperError("PAPER-0501", stage="tool_help",
                         detail={"reason": "未知工具", "available": [t["name"] for t in list_tools()]})
    return json.loads(p.read_text(encoding="utf-8"))


def selftest() -> dict:
    """[全局] 结构化自检（环境/依赖/配置/连通性）"""
    import socket
    import sys
    from paperparse.cli import _check_deps
    cfg = load_config()

    host = cfg.mineru_base_url.split("//")[-1].split("/")[0]
    try:
        socket.getaddrinfo(host, 443)
        dns_ok = True
    except Exception:
        dns_ok = False

    return {
        "python": sys.version.split()[0],
        "deps": {name: ok for name, ok, _ in _check_deps()},
        "mineru_api_key_configured": bool(cfg.mineru_api_key),
        "mineru_base_url": cfg.mineru_base_url,
        "mineru_dns_ok": dns_ok,
        "templates": [p.name[: -len(".md.j2")] for p in (asset_root() / "templates").glob("*.md.j2")],
    }
