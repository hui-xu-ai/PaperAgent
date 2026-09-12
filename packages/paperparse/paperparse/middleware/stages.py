#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: skill/src/paperparse/middleware/stages.py
功能: 管线阶段定义（管道-过滤器模式）：S0 校验 → S1 解析 → S2 版面 → S3 拼接 →
      S3.5 校准 → S4 图片 → S5 元数据 → S6 构建 → S7 渲染
      每阶段独立可测；产物落盘检查点（intermediate/）支持断点续跑；
      阶段失败只影响自身，由编排器统一决策（重试/降级/中止）
对外接口: StageContext / Stage / 各阶段类 / build_default_stages
版本: v1.0.0 (2026-08-19)
版本历史:
  v1.0.0 初始版本
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pymupdf

from paperparse.config import AppConfig
from paperparse.core.calibration_md import apply_calibration, match_paragraphs, parse_md
from paperparse.core.document_builder import (
    build_document,
    doi_dir_name,
    extract_references,
    output_dir_name,
    save_document,
)
from paperparse.core.image_extract import extract_figures
from paperparse.core.layout import analyze, reading_order
from paperparse.core.markdown_render import render as render_md
from paperparse.core.metadata import extract_metadata
from paperparse.core.mineru_client import MineruClient
from paperparse.core.pdf_validate import validate_pdf
from paperparse.core.pymupdf_fallback import extract_blocks, page_sizes
from paperparse.core.stitch_code import stitch
from paperparse.middleware.audit import Auditor
from paperparse.middleware.errors import PaperError, to_envelope, wrap_unknown
from paperparse.middleware.schema import (
    LayoutInfo,
    ParserBlocks,
    PipelineResult,
    RunStats,
    WarningItem,
)

__all__ = ["StageContext", "Stage", "build_default_stages"]


class StageContext:
    """[全局] 管线运行上下文：跨阶段共享产物与配置（文件系统为消息总线）"""

    def __init__(self, pdf_path: str | Path, cfg: AppConfig,
                 auditor: Auditor, paper_dir: str | Path):
        self.pdf_path = Path(pdf_path)
        self.cfg = cfg
        self.auditor = auditor
        self.paper_dir = Path(paper_dir)
        self.images_dir = self.paper_dir / "images"
        self.intermediate = self.paper_dir / "intermediate"
        self.intermediate.mkdir(parents=True, exist_ok=True)

        # 阶段产物
        self.pdf_info = None
        self.blocks: ParserBlocks | None = None
        self.skeleton = None       # P-ENHANCE R02: LayoutSkeleton（S1.5，content_list 布局骨架）
        self.layout: LayoutInfo | None = None
        self.ordered_blocks: list = []
        self.stitch_result = None
        self.calib_report = None
        self.figures: list = []
        self.metadata = None
        self.document = None
        self.md_text = ""
        self.warnings: list[WarningItem] = []
        self.parser_used = ""
        self.options: dict = {}
        # v2.2 解析来源反馈（Orchestrator 汇总到 RunStats）
        self.parse_source = ""      # mineru-v4 | mineru | pymupdf-local | auto-local
        self.latex_count = 0        # document.json 中 $ 总数

    # ---------- 检查点 ----------
    def checkpoint(self, name: str, data) -> Path:
        """[全局] 产物落盘（intermediate/<name>.json）"""
        p = self.intermediate / ("%s.json" % name)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return p

    def has_checkpoint(self, name: str) -> bool:
        return (self.intermediate / ("%s.json" % name)).exists()

    def load_checkpoint(self, name: str) -> dict:
        return json.loads((self.intermediate / ("%s.json" % name)).read_text(encoding="utf-8"))


class Stage:
    """[全局] 阶段基类：实现 validate/run；可选 save/load 支持断点续跑"""

    name = ""

    def validate(self, ctx: StageContext) -> None:
        """[全局] 前置校验（不满足抛 PaperError）"""

    def run(self, ctx: StageContext) -> None:
        """[全局] 阶段主逻辑"""
        raise NotImplementedError

    def save(self, ctx: StageContext) -> None:
        """[全局] 检查点保存（默认不落盘）"""

    def load(self, ctx: StageContext) -> bool:
        """[全局] 检查点恢复；返回是否恢复成功"""
        return False


# ---------- 具体阶段 ----------

class StageValidate(Stage):
    name = "S0_validate"

    def run(self, ctx: StageContext) -> None:
        ctx.pdf_info = validate_pdf(ctx.pdf_path)
        ctx.auditor.log_event(self.name, "output",
                              summary="pages=%d text_layer=%s" % (
                                  ctx.pdf_info.pages, ctx.pdf_info.has_text_layer))

    def save(self, ctx: StageContext) -> None:
        ctx.checkpoint(self.name, ctx.pdf_info.model_dump(mode="json"))

    def load(self, ctx: StageContext) -> bool:
        if ctx.has_checkpoint(self.name):
            from paperparse.middleware.schema import PdfInfo
            ctx.pdf_info = PdfInfo.model_validate(ctx.load_checkpoint(self.name))
            return True
        return False


class StageParse(Stage):
    """[全局] S1 解析：本地行级解析（主通道）+ MinerU v1 签名上传（可选增强，产出第二校准源）
    parser=auto → 仅本地；parser=mineru → 本地 + MinerU v1 Markdown（存 intermediate/mineru_v1.md）
    """

    name = "S1_parse"
    DEGRADE_CODES = ("PAPER-0010", "PAPER-0014", "PAPER-0015")

    def run(self, ctx: StageContext) -> None:
        parser = ctx.options.get("parser", "auto")
        # 主通道：本地行级解析（结构，总是先跑；失败仍整体中止由上层判断）
        ctx.blocks = extract_blocks(ctx.pdf_path)
        ctx.parser_used = "pymupdf"
        ctx.auditor.log_event(self.name, "output",
                              summary="pymupdf 行级 blocks=%d pages=%d" % (
                                  len(ctx.blocks.blocks), ctx.blocks.pages))
        # v2.1 监督机制：云端解析（MinerU）分两通道——
        #   mineru    = v1 免费签名上传（无需密钥，限流，出 LaTeX）
        #   mineru-v4 = v4 批量通道（需 MINERU_API_KEY，高精度）
        # 失败**中止**（raise，不静默降级本地）——由上层反馈用户选择（重试/换通道/本地）。
        if parser in ("mineru", "mineru-v4"):
            try:
                import shutil
                from paperparse.core.mineru_client import MineruClient, QuotaTracker
                client = MineruClient(ctx.cfg)
                quota = QuotaTracker(str(ctx.intermediate / "mineru_quota.json"),
                                     daily_limit=ctx.cfg.mineru_daily_page_limit)
                out_md = ctx.intermediate / "mineru_v1.md"
                if parser == "mineru-v4":
                    if not ctx.cfg.mineru_api_key:
                        raise PaperError(
                            "PAPER-0013", stage=self.name,
                            detail={"reason": "mineru-v4 精准解析需要 MINERU_API_KEY"
                                    "（v1 免费通道 mineru 无需密钥）"})
                    blocks = client.extract_v4_batch(
                        ctx.pdf_path, quota=quota,
                        on_state=lambda s: ctx.auditor.log_event(
                            self.name, "info", summary="mineru_v4 state=%s" % s))
                    if blocks.raw_path and Path(blocks.raw_path).exists():
                        shutil.copy2(blocks.raw_path, out_md)
                    else:
                        raise PaperError("PAPER-0014", stage=self.name,
                                         detail={"reason": "mineru-v4 未产出 full.md"})
                    # R02：原始排版 JSON 落盘（S1.5 布局骨架消费）
                    if blocks.raw_content_list and Path(blocks.raw_content_list).exists():
                        shutil.copy2(blocks.raw_content_list,
                                     ctx.intermediate / "mineru_content_list.json")
                else:
                    client.extract_v1(ctx.pdf_path, quota=quota,
                                      on_state=lambda s: ctx.auditor.log_event(
                                          self.name, "info", summary="mineru_v1 state=%s" % s),
                                      out_md=out_md)
                ctx.auditor.log_event(self.name, "output",
                                      summary="mineru markdown 已存 %s（文本主体）" % out_md.name)
            except PaperError as exc:
                # 监督：云端解析失败 → 记录并中止（不静默降级），上层反馈用户决定
                ctx.auditor.log_event(self.name, "error",
                                      summary="mineru 解析失败（%s）→ 中止" % exc.code,
                                      payload={"code": exc.code, "detail": exc.detail})
                raise

    def save(self, ctx: StageContext) -> None:
        ctx.checkpoint(self.name, ctx.blocks.model_dump(mode="json"))

    def load(self, ctx: StageContext) -> bool:
        if ctx.has_checkpoint(self.name):
            ctx.blocks = ParserBlocks.model_validate(ctx.load_checkpoint(self.name))
            return True
        return False


class StageLayoutSkeleton(Stage):
    """[全局] S1.5 布局骨架（P-ENHANCE R02）：MinerU content_list.json →
    类型过滤/双栏判定/阅读序/角色标注/章节树（LayoutSkeleton）。

    消费 intermediate/mineru_content_list.json（StageParse 落盘 / OfflineParse 注入）；
    无该文件（pymupdf 本地 / mineru v1 通道）则跳过，管线行为不变。
    """

    name = "S1.5_skeleton"

    def run(self, ctx: StageContext) -> None:
        cl = ctx.intermediate / "mineru_content_list.json"
        if not cl.exists():
            ctx.auditor.log_event(self.name, "info", summary="无 content_list.json，跳过")
            return
        from paperparse.core.layout_skeleton import build_skeleton, load_content_list
        ctx.skeleton = build_skeleton(load_content_list(str(cl)))
        st = ctx.skeleton.stats
        ctx.auditor.log_event(
            self.name, "output",
            summary="kept=%d filtered=%d two_column=%s sections=%d roles=%s" % (
                st["kept"], st["filtered"], st["two_column"], st["sections"],
                st["roles"]))


class StageLayout(Stage):
    name = "S2_layout"
    def run(self, ctx: StageContext) -> None:
        from paperparse.core.block_classify import classify_lines
        sizes = page_sizes(ctx.pdf_path)
        classify_lines(ctx.blocks.blocks)              # 先分类（正文/标题/图注/meta）
        ctx.layout = analyze(ctx.blocks.blocks, page_sizes=sizes)
        ctx.ordered_blocks = reading_order(ctx.blocks.blocks, ctx.layout)
        kinds = {}
        for b in ctx.ordered_blocks:
            kinds[b.kind] = kinds.get(b.kind, 0) + 1
        ctx.auditor.log_event(self.name, "output", summary="two_column=%s noise=%d ref_pages=%s kinds=%s" % (
            ctx.layout.is_two_column, len(ctx.layout.noise_block_ids),
            ctx.layout.reference_zone_pages, kinds))

    def save(self, ctx: StageContext) -> None:
        ctx.checkpoint(self.name, ctx.layout.model_dump(mode="json"))

    def load(self, ctx: StageContext) -> bool:
        if ctx.has_checkpoint(self.name):
            ctx.layout = LayoutInfo.model_validate(ctx.load_checkpoint(self.name))
            ctx.ordered_blocks = reading_order(ctx.blocks.blocks, ctx.layout)
            return True
        return False


class StageStitch(Stage):
    name = "S3_stitch"

    def run(self, ctx: StageContext) -> None:
        ctx.stitch_result = stitch(ctx.ordered_blocks, ctx.layout)
        ctx.auditor.log_event(self.name, "output",
                              summary="paragraphs=%d low_conf=%d" % (
                                  ctx.stitch_result.stats.get("paragraph_count"),
                                  len(ctx.stitch_result.low_confidence_ids)))

    def save(self, ctx: StageContext) -> None:
        ctx.checkpoint(self.name, ctx.stitch_result.model_dump(mode="json"))

    def load(self, ctx: StageContext) -> bool:
        if ctx.has_checkpoint(self.name):
            from paperparse.middleware.schema import StitchResult
            ctx.stitch_result = StitchResult.model_validate(ctx.load_checkpoint(self.name))
            return True
        return False


class StageCalibrate(Stage):
    """[全局] S3.5 校准 MD（可选；找不到校准文件则跳过并记录 info）"""

    name = "S3.5_calibrate"

    def _find_calib(self, ctx: StageContext) -> Path | None:
        """[局部] 校准源优先级：显式路径 > 探测（input/、tests/samples/、同目录）> MinerU v1 Markdown"""
        cfg_path = ctx.options.get("calibration_md")
        if cfg_path:
            p = Path(cfg_path)
            return p if p.exists() else None
        stem = ctx.pdf_path.stem
        candidates = [
            ctx.pdf_path.with_suffix(".md"),
            Path(ctx.cfg.input_dir) / (stem + ".md"),
            Path("tests/samples") / (stem + ".md"),
        ]
        for c in candidates:
            if c.exists():
                return c
        # MinerU v1 Markdown 仅在本次请求云端通道（mineru/mineru-v4）时作为校准源；
        # 否则跳过（修复：上次 run 残留的 mineru_v1.md 会被 pymupdf/auto 无条件复用，
        # 造成"本地解析却显示高精度/污染融合"的假象）
        parser = ctx.options.get("parser", "auto")
        v1_md = ctx.intermediate / "mineru_v1.md"
        if parser in ("mineru", "mineru-v4") and v1_md.exists():
            return v1_md
        return None

    def run(self, ctx: StageContext) -> None:
        # v2.0.1 修复：parser=mineru 时 mineru_v1.md 是**文本主体**（LaTeX），只要存在就
        # 无条件融合（build_mineru_doc 段落级替换写回含 $ 的 mineru 全文）——不依赖校准源选择
        # （原实现把融合挂在"校准源==mineru_v1.md"分支，但 _find_calib 会被同目录 .md 抢占，
        # 导致 mineru 的 LaTeX 从未进入 document.json）
        # v2.3.0：融合仅限本次请求云端通道（mineru/mineru-v4）——防止上次 run 残留的
        # mineru_v1.md 被 pymupdf/auto 复用造成假高精度（run_id 隔离性修复）
        parser = ctx.options.get("parser", "auto")
        v1_md = ctx.intermediate / "mineru_v1.md"
        if parser in ("mineru", "mineru-v4") and v1_md.exists():
            from paperparse.core.mineru_doc_builder import build_mineru_doc
            replaced = build_mineru_doc(ctx.stitch_result.paragraphs,
                                        v1_md.read_text(encoding="utf-8"))
            ctx.auditor.log_event(self.name, "output",
                                  summary="mineru LaTeX 融合 replaced=%d paragraphs" % replaced)
        calib_path = self._find_calib(ctx)
        if calib_path is None:
            ctx.auditor.log_event(self.name, "info", summary="未找到校准 MD，跳过")
            return
        calib = parse_md(calib_path)
        report = match_paragraphs(ctx.stitch_result.paragraphs, calib)
        apply_calibration(ctx.stitch_result, report)
        ctx.calib_report = report
        ctx.auditor.log_event(self.name, "output",
                              summary="matched=%d fixed=%d unmatched=%d avg_dice=%.3f" % (
                                  len(report.matched_paragraph_ids),
                                  len(report.fixed_paragraph_ids),
                                  report.unmatched_count,
                                  report.stats.get("avg_best_dice", 0.0)))

    def save(self, ctx: StageContext) -> None:
        if ctx.calib_report:
            ctx.checkpoint(self.name, ctx.calib_report.model_dump(mode="json"))

    def load(self, ctx: StageContext) -> bool:
        if ctx.has_checkpoint(self.name):
            from paperparse.middleware.schema import CalibrationReport
            # resume 场景：段落文本来自 S3 stitch checkpoint（本地文本未融合）——
            # 若 mineru_v1.md 存在则重跑 LaTeX 融合（build_mineru_doc 幂等：已融合段不再替换）
            v1_md = ctx.intermediate / "mineru_v1.md"
            if v1_md.exists():
                from paperparse.core.mineru_doc_builder import build_mineru_doc
                build_mineru_doc(ctx.stitch_result.paragraphs,
                                 v1_md.read_text(encoding="utf-8"))
            ctx.calib_report = CalibrationReport.model_validate(ctx.load_checkpoint(self.name))
            apply_calibration(ctx.stitch_result, ctx.calib_report)
            return True
        return False


class StageFigures(Stage):
    name = "S4_figures"

    def run(self, ctx: StageContext) -> None:
        dpi = int(ctx.options.get("dpi", ctx.cfg.pdf_render_dpi))
        # R10：骨架驱动优先（MinerU content_list figure 角色 bbox → 渲染，覆盖矢量图；
        # get_image_info 漏矢量图——cej 41 图仅提取 2 的根因）；无骨架回退原逻辑
        if ctx.skeleton is not None:
            from paperparse.core.image_extract import extract_figures_from_skeleton
            ctx.figures = extract_figures_from_skeleton(
                ctx.pdf_path, ctx.skeleton, ctx.images_dir, dpi=dpi,
                blocks=ctx.blocks.blocks)
        else:
            ctx.figures = extract_figures(ctx.pdf_path, ctx.blocks.blocks,
                                          ctx.images_dir, dpi=dpi)
        ctx.auditor.log_event(self.name, "output",
                              summary="figures=%d" % len(ctx.figures))

    def save(self, ctx: StageContext) -> None:
        from paperparse.middleware.schema import Figure
        ctx.checkpoint(self.name, [f.model_dump(mode="json") for f in ctx.figures])

    def load(self, ctx: StageContext) -> bool:
        if ctx.has_checkpoint(self.name):
            from paperparse.middleware.schema import Figure
            ctx.figures = [Figure.model_validate(d) for d in ctx.load_checkpoint(self.name)]
            return True
        return False


class StageMetadata(Stage):
    name = "S5_metadata"

    def run(self, ctx: StageContext) -> None:
        try:
            with pymupdf.open(str(ctx.pdf_path)) as doc:
                meta = dict(doc.metadata)
        except Exception:
            meta = None
        ctx.metadata = extract_metadata(ctx.ordered_blocks,
                                        source_pdf=str(ctx.pdf_path), pdf_meta=meta)
        # R06：布局骨架校正元数据（title/authors/keywords 以 content_list 为准）
        if ctx.skeleton is not None:
            from paperparse.core.metadata import enhance_with_skeleton
            enhance_with_skeleton(ctx.metadata, ctx.skeleton)
        missing = [k for k, v in
                   (("title", ctx.metadata.title), ("doi", ctx.metadata.doi),
                    ("abstract", ctx.metadata.abstract), ("keywords", ctx.metadata.keywords))
                   if not v]
        if missing:
            ctx.warnings.append(WarningItem(
                code="PAPER-0102", stage=self.name,
                user_message="元数据不完整：%s" % ", ".join(missing)))
            ctx.auditor.log_warning(self.name, "PAPER-0102", "元数据缺失: %s" % missing)
        ctx.auditor.log_event(self.name, "output",
                              summary="title_len=%d authors=%d doi=%s keywords=%d" % (
                                  len(ctx.metadata.title), len(ctx.metadata.authors),
                                  ctx.metadata.doi, len(ctx.metadata.keywords)))

    def save(self, ctx: StageContext) -> None:
        ctx.checkpoint(self.name, ctx.metadata.model_dump(mode="json"))

    def load(self, ctx: StageContext) -> bool:
        if ctx.has_checkpoint(self.name):
            from paperparse.middleware.schema import ArticleMetadata
            ctx.metadata = ArticleMetadata.model_validate(ctx.load_checkpoint(self.name))
            return True
        return False


class StageBuild(Stage):
    """[全局] S6 构建：DOI 目录命名（若与暂用名不同则重命名目录）+ document.json"""

    name = "S6_build"

    def run(self, ctx: StageContext) -> None:
        refs = extract_references(ctx.ordered_blocks, ctx.layout)
        if not refs and ctx.layout.reference_zone_pages:
            ctx.warnings.append(WarningItem(
                code="PAPER-0103", stage=self.name,
                user_message="参考文献解析不完整（0 条）"))
            ctx.auditor.log_warning(self.name, "PAPER-0103", "参考文献 0 条")

        # 图数 ↔ 图注数 相互校验（用户问题 8：位置与数量准确性）
        captions = [p for p in ctx.stitch_result.paragraphs if p.is_caption]
        if ctx.figures and len(ctx.figures) != len(captions):
            ctx.warnings.append(WarningItem(
                code="PAPER-0105", stage=self.name,
                user_message="图片与图注数量不一致（图 %d 张 / 图注 %d 条）" % (
                    len(ctx.figures), len(captions))))
            ctx.auditor.log_warning(self.name, "PAPER-0105",
                                    "figures=%d captions=%d" % (len(ctx.figures), len(captions)))
        elif ctx.figures:
            ctx.auditor.log_event(self.name, "output",
                                  summary="图注↔图数量校验通过（%d）" % len(ctx.figures))

        ctx.document = build_document(
            ctx.metadata, ctx.stitch_result, figures=ctx.figures, references=refs,
            run_id=ctx.auditor.run_id,
            warnings=[w.user_message for w in ctx.warnings])
        # 文档卫生 pass（渲染/导出前统一执行，document.json 即干净源）：
        # ① 骨架标题校正（R02：先补全结构——MinerU content_list 权威 heading → 补缺失/
        #    纠正误分类/拆分粘连；插入的标题段随后可被规则引擎修复）
        # ② 规则引擎（R04：消费 rules/ 规则库，6 类别顺序 + 置信度门控 + audit；
        #    替代原 KNOWN_FIXES 硬编码——已知修复库已迁移为 builtin 规则）
        # ③ 乱码重提取（含 U+FFFD 段落按 coords 调本地 PyMuPDF 提取对应片段替换）
        n_skel = 0
        if ctx.skeleton is not None:
            from paperparse.core.skeleton_fix import apply_skeleton_headings
            n_skel = apply_skeleton_headings(ctx.document, ctx.skeleton)
        from paperparse.core.rule_engine import apply_rules, save_report
        # P12 安全红线：双通道模式排除 source=mining 挖掘规则（只经通道作用域清洗，
        # 不直接作用最终文档——防止学习规则改错正确结果）
        report = apply_rules(ctx.document, ctx.cfg.rules_dir or None, ctx.pdf_path,
                             exclude_source=ctx.options.get("exclude_rule_source"))
        save_report(report, ctx.intermediate / "rule_report.json")
        n_rules = len(report.applied)
        from paperparse.core.anomaly_detect import repair_garbled
        n_garbled = repair_garbled(ctx.document, ctx.pdf_path)
        if n_rules or n_garbled or n_skel or report.pending:
            ctx.auditor.log_event(
                self.name, "output",
                summary="骨架标题校正 %d / 规则修复 %d 段 / 待确认 %d / 乱码重提取 %d 段"
                        % (n_skel, n_rules, len(report.pending), n_garbled))
        save_document(ctx.document, ctx.intermediate / "document.json")

        # DOI 目录命名：暂用名 → 正式名（P2-10：无 DOI 用净化后 PDF 文件名，禁 paper_<hash>）
        target = output_dir_name(ctx.metadata.doi, ctx.pdf_path)
        if ctx.paper_dir.name != target:
            new_dir = ctx.paper_dir.parent / target
            if not new_dir.exists() and new_dir.parent.exists():
                ctx.paper_dir.rename(new_dir)
                ctx.paper_dir = new_dir
                ctx.images_dir = ctx.paper_dir / "images"
                ctx.intermediate = ctx.paper_dir / "intermediate"
                ctx.auditor.relocate(ctx.paper_dir / "audit" / ctx.auditor.run_id)

    def save(self, ctx: StageContext) -> None:
        pass  # document.json 已在 run 中落盘

    def load(self, ctx: StageContext) -> bool:
        p = ctx.intermediate / "document.json"
        if p.exists():
            from paperparse.core.document_builder import load_document
            ctx.document = load_document(p)
            return True
        return False


class StageAIReview(Stage):
    """[全局] S7.5 AI 审查（P-ENHANCE R05，可选）：待审清单 → LLM 判断 → 修复 + 规则候选。

    启用：环境变量 RULES_AI_REVIEW=1（应用侧解析后自动跑；FakeAI/mock 可离线测试）。
    AI provider：llm.client.get_ai()（应用注入 DeepSeekAI；测试用 FakeAI）。
    """

    name = "S7.5_ai_review"

    def run(self, ctx: StageContext) -> None:
        import os
        if os.getenv("RULES_AI_REVIEW", "0").strip() not in ("1", "true", "yes", "on"):
            ctx.auditor.log_event(self.name, "info", summary="未启用（RULES_AI_REVIEW!=1），跳过")
            return
        from paperparse.core.ai_review import build_review_items, review_items
        from paperparse.core.rule_library import add_rule
        items = build_review_items(ctx.document, ctx.cfg.rules_dir or None,
                                   enable_mid_join=True)
        if not items:
            ctx.auditor.log_event(self.name, "info", summary="无待审项，跳过")
            return
        ctx.auditor.log_event(self.name, "output",
                              summary="待审 %d 项" % len(items))
        result = review_items(items, ctx.document)
        added = 0
        for cand in result.get("candidates", []):
            try:
                add_rule(ctx.cfg.rules_dir or None, cand, level="learned")
                added += 1
            except ValueError:
                pass
        ctx.auditor.log_event(
            self.name, "output",
            summary="AI 审查: 修复 %d / 忽略 %d / 规则候选入库 %d" % (
                result.get("fixed", 0), result.get("ignored", 0), added))
        if result.get("fixed", 0):
            save_document(ctx.document, ctx.intermediate / "document.json")


class StageRender(Stage):
    name = "S7_render"
    def run(self, ctx: StageContext) -> None:
        template = ctx.options.get("template", ctx.cfg.md_template)
        ctx.md_text = render_md(ctx.document, template=template)
        md_path = ctx.paper_dir / "paper.md"
        md_path.write_text(ctx.md_text, encoding="utf-8")
        ctx.auditor.log_event(self.name, "output",
                              summary="paper.md bytes=%d" % len(ctx.md_text.encode("utf-8")))


def build_default_stages() -> list[Stage]:
    """[全局] 默认阶段管线（S0→S7 + 可选 S7.5）"""
    return [StageValidate(), StageParse(), StageLayoutSkeleton(), StageLayout(),
            StageStitch(), StageCalibrate(), StageFigures(), StageMetadata(),
            StageBuild(), StageAIReview(), StageRender()]
