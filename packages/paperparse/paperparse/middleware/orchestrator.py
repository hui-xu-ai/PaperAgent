#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: skill/src/paperparse/middleware/orchestrator.py
功能: 管线编排（L2 中间调度层核心）：
      - 按序执行阶段（S0→S7），单阶段失败 → 错误信封 → 审计 → 返回 failed（含部分产物）
      - 断点续跑（resume）：跳过已有检查点的阶段
      - 警告汇总（PAPER-0101/0102/0103）与统计（页数/耗时/解析器）
对外接口: Orchestrator
版本: v1.0.0 (2026-08-19)
版本历史:
  v1.0.0 初始版本
"""
from __future__ import annotations

import time
from pathlib import Path

from paperparse.config import AppConfig, load_config
from paperparse.core.document_builder import doi_dir_name
from paperparse.middleware.audit import Auditor, new_run_id
from paperparse.middleware.errors import PaperError, to_envelope, wrap_unknown
from paperparse.middleware.schema import OutputPaths, PipelineResult, RunStats
from paperparse.middleware.stages import StageContext, StageRender, build_default_stages

__all__ = ["Orchestrator"]


class Orchestrator:
    """[全局] 单篇文献管线编排器（每篇一个 run，审计独立）"""

    def __init__(self, cfg: AppConfig | None = None):
        self.cfg = cfg or load_config()
        self._stages = build_default_stages()

    # ---------- 主入口 ----------

    def run(self, pdf_path: str | Path, *,
            parser: str = "auto", dpi: int | None = None,
            calibration_md: str | Path | None = None,
            template: str | None = None,
            out_dir: str | Path = "output",
            resume: bool = False,
            run_id: str | None = None,
            auditor: Auditor | None = None) -> PipelineResult:
        """[全局] 执行完整管线

        参数:
            pdf_path: PDF 路径
            parser: auto | pymupdf | mineru
            dpi: 图片渲染分辨率
            calibration_md: 校准 MD 路径（None=自动探测）
            template: Markdown 模板名
            out_dir: 输出根目录
            resume: 是否断点续跑（跳过已有检查点阶段）
            run_id: 指定运行 ID（审计目录复用）
            auditor: 外部注入审计器（测试用）
        返回:
            PipelineResult
        """
        started = time.time()
        run_id = run_id or new_run_id()

        # 暂用目录名：统一命名规则——有效 DOI 风格文件名保持；否则净化后的 PDF 文件名
        # （P2-3：不再用 run_id，保证用户按文件名能找到产物）
        from paperparse.core.document_builder import output_dir_name
        tmp_name = output_dir_name(None, pdf_path)
        paper_dir = Path(out_dir) / tmp_name
        paper_dir.mkdir(parents=True, exist_ok=True)

        auditor = auditor or Auditor(run_id, paper_dir / "audit" / run_id)
        auditor.log_event("orchestrator", "input",
                          summary="pdf=%s parser=%s resume=%s" % (
                              Path(pdf_path).name, parser, resume))

        ctx = StageContext(pdf_path, self.cfg, auditor, paper_dir)
        ctx.options = {
            "parser": parser,
            "dpi": dpi or self.cfg.pdf_render_dpi,
            "calibration_md": str(calibration_md) if calibration_md else None,
            "template": template or self.cfg.md_template,
        }

        failed_stage = ""
        error_envelope = None
        for stage in self._stages:
            t0 = time.time()
            try:
                if resume and stage.load(ctx):
                    auditor.log_event(stage.name, "info", summary="检查点恢复，跳过")
                    continue
                stage.validate(ctx)
                stage.run(ctx)
                stage.save(ctx)
                auditor.log_event(stage.name, "output",
                                  summary="完成",
                                  duration_ms=int((time.time() - t0) * 1000))
            except PaperError as exc:
                error_envelope = to_envelope(exc)
                failed_stage = stage.name
                auditor.log_error(stage.name, error_envelope,
                                  duration_ms=int((time.time() - t0) * 1000))
                break
            except Exception as exc:            # 防崩溃：任何异常都可溯源
                wrapped = wrap_unknown(exc, stage.name)
                error_envelope = to_envelope(wrapped)
                failed_stage = stage.name
                auditor.log_error(stage.name, error_envelope,
                                  duration_ms=int((time.time() - t0) * 1000))
                break

        # 低置信拼接警告
        if ctx.stitch_result and ctx.stitch_result.low_confidence_ids:
            from paperparse.middleware.schema import WarningItem
            ctx.warnings.append(WarningItem(
                code="PAPER-0101", stage="S3",
                user_message="%d 个段落拼接置信度低，建议人工复核" % len(
                    ctx.stitch_result.low_confidence_ids)))
            auditor.log_warning("S3", "PAPER-0101",
                                "低置信段落: %s" % ctx.stitch_result.low_confidence_ids)

        # 目录重命名（若 S6 已改 DOI 名）——P2-10：无 DOI 用净化后 PDF 文件名，禁 paper_<hash>
        target = output_dir_name(ctx.metadata.doi if ctx.metadata else None, ctx.pdf_path)
        if ctx.paper_dir.name != target and not (ctx.paper_dir.parent / target).exists():
            new_dir = ctx.paper_dir.parent / target
            if new_dir.parent.exists():
                ctx.paper_dir.rename(new_dir)
                ctx.paper_dir = new_dir

        # 审计目录最终位置（DOI 目录内）+ 监督报告生成
        final_audit = ctx.paper_dir / "audit" / run_id
        try:
            if auditor.out_dir != final_audit:
                if not final_audit.exists():
                    final_audit.parent.mkdir(parents=True, exist_ok=True)
                    auditor.out_dir.rename(final_audit)
            auditor.relocate(final_audit)
        except OSError:
            auditor.relocate(final_audit)
        try:
            auditor.render_report()
        except Exception:
            pass  # 报告生成失败不阻塞主流程（审计事件已落盘）

        duration = round(time.time() - started, 2)
        md_exists = (ctx.paper_dir / "paper.md").exists()
        status = "failed" if error_envelope else ("success" if md_exists else "partial")

        # v2.2 解析来源统计：mineru_v1.md 存在=云端融合；否则本地；latex_count=document.json $ 数
        try:
            mineru_md = ctx.intermediate / "mineru_v1.md"
            if mineru_md.exists():
                src = parser if parser in ("mineru", "mineru-v4") else "mineru"
                ctx.parse_source = src
            else:
                ctx.parse_source = "pymupdf-local" if parser in ("pymupdf", "auto") else "%s(云端未产出)" % parser
            doc_p = ctx.intermediate / "document.json"
            if doc_p.exists():
                import json as _json
                d = _json.load(open(doc_p, encoding="utf-8"))
                ctx.latex_count = sum((p.get("text_en") or "").count("$")
                                      for p in d.get("paragraphs", []))
        except Exception:
            pass

        result = PipelineResult(
            run_id=run_id,
            status=status,
            outputs=OutputPaths(
                md=str(ctx.paper_dir / "paper.md") if md_exists else "",
                images_dir=str(ctx.paper_dir / "images") if ctx.figures else "",
                document_json=str(ctx.intermediate / "document.json")
                if (ctx.intermediate / "document.json").exists() else "",
                audit_dir=str(final_audit),
            ),
            warnings=ctx.warnings,
            error=error_envelope,
            stats=RunStats(
                pages=ctx.pdf_info.pages if ctx.pdf_info else 0,
                tokens=0,
                duration_sec=duration,
                parser=ctx.parser_used,
                # v2.2 解析来源反馈：mineru_v1.md 存在=云端融合；否则本地。latex_count=document.json $ 数
                parse_source=ctx.parse_source,
                latex_count=ctx.latex_count,
            ),
        )
        auditor.log_event("orchestrator", "output",
                          summary="status=%s pages=%d duration=%.1fs" % (
                              status, result.stats.pages, duration),
                          duration_ms=int(duration * 1000))
        return result
