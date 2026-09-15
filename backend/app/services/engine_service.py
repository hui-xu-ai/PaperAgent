# -*- coding: utf-8 -*-
"""引擎接入层：paperparse.api 的唯一封装（T03）。

原则：
- 所有引擎调用只经本模块（隔离引擎内部变化，改动只影响一处）。
- `doc_summary()` 只提取元数据 + 章节索引（**不含全文**），
  供前端论文卡片与对话稳定前缀使用（Token 纪律：轻量、固定）。
- 翻译/总结走 paperkb（combined_translate，写回 document.json），
  由 kbmeta_service 注入的 LLM 适配完成，全文不进对话历史。
"""
from __future__ import annotations

import logging
from pathlib import Path

from ..config import Settings

logger = logging.getLogger(__name__)


def _paper_dir(document_json: str | Path) -> Path:
    """[局部] document.json → 篇目录（library/<DOI>/）：兼容两种布局——
    生产（process_pdf_v2 直写 library/<DOI>/document.json，p.parent 即篇目录）
    与 parse-only 中间态（library/<DOI>/intermediate/document.json，需再上一层）。
    2026-08-26 修复：原 p.parent.parent 硬编码在无 intermediate 层时错写成
    library/ 顶层，产生顶层 <DOI>.en.md 重复产物。"""
    d = Path(document_json).resolve().parent
    if d.name == "intermediate":
        d = d.parent
    return d


class EngineError(Exception):
    """引擎调用失败（结构化消息，含错误码）。"""

    def __init__(self, message: str, code: str = "", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


# P2-B：解析来源 → 用户可读标签（引擎 process_pdf 返回的 parse_source 取值）
PARSE_SOURCE_LABELS: dict[str, str] = {
    "mineru-v4": "MinerU 精准解析（v4 高精度）",
    "mineru": "MinerU 免费通道（v1 低精度）",
    "pymupdf-local": "本地解析（pymupdf 低精度）",
    "auto-local": "本地解析（auto 低精度）",
    # P12：双 PDF 解析（mineru-v4 主 + PaddleOCR-VL-1.6 辅 + AI 仲裁）
    "dual": "双通道解析（MinerU 精准 + PaddleOCR-VL + AI 仲裁）",
    # P15 Step5：P14 文本管线（本地骨架权威边界 + mineru full.md 文本基底 +
    # 拼接修复 + 双通道验证 + 段落内字符仲裁）——生产基底（默认）
    # 2026-08-26：文案改直观（用户反馈 "P14 管线" 术语不友好）
    "p14": "双通道OCR+AI（MinerU + 百度OCR + AI仲裁）",
}


def parse_source_label(source: str) -> str:
    """未知来源兜底显示原值。"""
    return PARSE_SOURCE_LABELS.get(source, source or "未知")


# 2026-09-12 批2（用户拍板）：**解析硬门禁**——没有 MinerU Key 就没有可靠解析。
# 免费 v1 通道（限流 + 精度低）与本地 pymupdf（无 LaTeX/表格/公式）已从生产链移除，
# 因此无 Key 时**直接拒绝解析**而不是静默降级（用户原话："质量不可靠宁可不解析"）。
MINERU_REQUIRED_MSG = (
    "未配置 MinerU API Key，解析功能已禁用（免费/本地通道已按质量要求移除）。"
    "请到「设置中心 → 解析 → MinerU 精准解析」填写 Key 并点「保存解析设置」，"
    "然后重新导入或重新解析。Key 申请：https://mineru.net/apiManage"
)


def _wrap(fn_name: str, result: dict) -> dict:
    """引擎返回信封统一检查：status != success 时抛 EngineError。"""
    status = result.get("status")
    if status in (None, "success"):
        return result
    err = result.get("error") or result.get("note") or f"{fn_name} 失败"
    code = ""
    if isinstance(err, dict):
        code = str(err.get("code", ""))
        err = str(err.get("user_message") or err.get("detail") or err)
    raise EngineError(str(err), code=code)


class TaskCancelled(Exception):
    """P15：任务取消信号（官方云队列等待期间用户取消）——与普通失败区分：
    解析链**不降级**（取消是用户意图，不是通道失败），直接向上传播终止任务。"""


class EngineService:
    """paperparse.api facade 的薄封装。"""

    def __init__(self, settings: Settings, kb=None, kbmeta=None):
        self.settings = settings
        import paperparse.api as api
        self._api = api
        # R4：依赖改为构造注入（kb 访问器 / kbmeta 访问器），不再 import container 摸单例。
        # 未注入（单测/独立调用）时回退模块 accessor。
        self._kb = kb      # 可调用 accessor（返回 KnowledgeBaseService）或实例
        self._kbmeta = kbmeta  # 可调用 accessor（返回 KbMetaService）或实例

    # ---------------------------------------------------------- 依赖访问器（DI）
    def _get_kb(self):
        kb = self._kb
        if kb is not None:
            return kb() if callable(kb) else kb
        return None

    def _get_kbmeta(self):
        kbmeta = self._kbmeta
        if kbmeta is not None:
            return kbmeta() if callable(kbmeta) else kbmeta
        from .kbmeta_service import get_kbmeta

        return get_kbmeta()

    # ---------------------------------------------------------- 解析
    def _parse_chain(self, parser: str | None = None) -> list[str]:
        """生产解析通道：**仅** mineru-v4（精准，需 Key）。

        2026-09-12 批2：原降级链 mineru-v4 → mineru v1 免费 → pymupdf 本地已按用户决策收敛
        （质量不可靠的通道不再作为兜底）；无 Key 的情形由 `parse_pdf` 前置门禁直接拒绝，
        不会走到这里。`parser` 显式传入时按指定通道（内部/离线调用用）。
        """
        if parser:
            return [parser]
        return ["mineru-v4"]

    def parse_pdf(self, pdf_path: str | Path, run_id: str | None = None,
                  parser: str | None = None,
                  cancel_check=None, on_wait=None) -> dict:
        """精准解析（parse_only=True）：document.json + en.md + images，0 AI token。

        **前置硬门禁（批2）**：未配置 MinerU Key（`.env`/os.environ 实时判定）→ 直接抛
        `EngineError(MINERU_REQUIRED_MSG)`，不再降级免费/本地通道（用户决策："质量不可靠
        宁可不解析"）。
        P12/P14：默认走**双通道**（mineru-v4 主 + PaddleOCR-VL-1.6 辅 + 字符仲裁），
        管线由 `settings.pipeline` 决定（p14 生产基底）；双通道失败仍降级**单通道
        mineru-v4**（同为精准通道，不是换低质量通道）。
        P15：cancel_check/on_wait 官方云队列等待机制透传——cancel_check() 为 True
        立即抛 TaskCancelled（**不降级**，用户取消是意图）；on_wait(sec, attempt)
        上报"队列繁忙等待中"状态。
        """
        from ..config import mineru_ready
        if not mineru_ready(self.settings.mineru_api_key):
            logger.error("解析被拒绝：未配置 MinerU API Key（硬门禁，批2）")
            raise EngineError(MINERU_REQUIRED_MSG, code="PAPER-MINERU-REQUIRED")
        pdf_path = str(Path(pdf_path).resolve())
        if cancel_check and cancel_check():
            raise TaskCancelled("用户取消（解析开始前）")
        errors: list[str] = []
        # P12/P14：双通道模式（无显式 parser 时；显式指定则走指定链）
        # 双通道是增强路径：任何异常都降级单通道（Q2），不阻塞任务
        if parser is None and self._parse_mode_dual():
            try:
                return self._parse_pdf_dual(pdf_path, run_id,
                                            cancel_check=cancel_check,
                                            on_wait=on_wait)
            except TaskCancelled:
                raise
            except EngineError as e:
                errors.append(f"dual: {e}")
                logger.warning("双通道解析失败，降级单通道: %s", e)
            except Exception as e:  # noqa: BLE001 - 双通道任何异常都降级，绝不阻塞
                errors.append(f"dual: {e}")
                logger.warning("双通道解析异常，降级单通道: %s", e)
        for p in self._parse_chain(parser):
            try:
                logger.info("解析 PDF: %s parser=%s run_id=%s", pdf_path, p, run_id)
                result = self._api.process_pdf(
                    pdf_path, parser=p,
                    out_dir=self.settings.engine_work_root,
                    run_id=run_id, parse_only=True,
                )
                wrapped = _wrap("process_pdf", result)
                # G13：若发生过云端通道失败（降级到更低精度通道），附上失败原因供任务提示
                if errors:
                    wrapped["warnings"] = errors
                # P2-B：保证解析来源始终返回（引擎缺失时按实际通道兜底）
                if not wrapped.get("parse_source"):
                    wrapped["parse_source"] = p
                # P2-C：无论通道，清洗图片占位符并补齐图片行（重渲染 en.md）
                try:
                    self._post_parse_clean(wrapped["document_json"])
                except Exception as e:  # noqa: BLE001 - 清洗失败不阻塞主流程
                    logger.warning("解析后清洗失败（不阻塞）: %s", e)
                return wrapped
            except EngineError as e:
                errors.append(f"{p}: {e}")
                logger.warning("解析通道 %s 失败: %s", p, e)
        raise EngineError("解析失败（全部通道尝试完毕）: " + " | ".join(errors))

    # ---------------------------------------------------------- P12/P14 管线
    def _parse_mode_dual(self) -> bool:
        """解析模式：settings_service（SQLite）覆盖 > 环境变量（默认 dual）。"""
        try:
            from .container import get_settings_service
            return get_settings_service().get_parse().get("mode") == "dual"
        except Exception:  # noqa: BLE001 - 设置服务不可用时按 env 默认
            return self.settings.parse_mode == "dual"

    def _parse_pdf_dual(self, pdf_path: str, run_id: str | None = None,
                        cancel_check=None, on_wait=None) -> dict:
        """P15 Step5：parse_mode=dual 下的管线调度。

        **2026-08-26 冻结 P12**：pipeline=p12 回退路径（process_pdf_dual）已移除
        （P14 为生产基底，P12 双通道 + 规则引擎退役归档）——配置值 p12 静默回落 p14。
        - p14（生产基底）：P14 文本管线（本地骨架权威边界 + mineru full.md
          文本基底 + 拼接修复 + 双通道验证 + 段落内字符仲裁）；
        - AI 仲裁 provider/ai_review 组装（_assemble_provider）；
          TokenGuard context="arbitration:<pdf_stem>" 按篇隔离（Q5）；
        - P15：cancel_check/on_wait 透传（官方云队列等待机制），取消**不降级**。
        """
        ai_review, provider = self._assemble_provider(pdf_path)
        if self.settings.pipeline == "p12":
            logger.warning("PAPERPARSE_PIPELINE=p12 已冻结（P14 为生产基底），回落 p14")
        # ---- P14 文本管线（生产基底）----
        stem = Path(pdf_path).stem
        work = Path(self.settings.dual_work_root) / stem
        md_path = self._ensure_mineru_md(pdf_path, work)
        result = self._api.process_pdf_v2(
            pdf_path, md_path=str(md_path),
            # ★ 统一规范库（T01）：所有解析/翻译/导出产物就地存放
            #   library/<stem>/（engine_work_root）——P12 同布局，不得外置
            out_dir=self.settings.engine_work_root,
            paddle=True,
            ai_review=provider is not None and ai_review,
            provider=provider,
            run_id=run_id,
            cancel_check=cancel_check,
            on_wait=on_wait,
        )
        wrapped = _wrap("process_pdf_v2", result)
        if not wrapped.get("parse_source"):
                wrapped["parse_source"] = "p14"
        try:
            self._post_parse_clean(wrapped["document_json"])
        except Exception as e:  # noqa: BLE001 - 清洗失败不阻塞主流程
            logger.warning("解析后清洗失败（不阻塞）: %s", e)
        return wrapped

    def _assemble_provider(self, pdf_path: str) -> tuple[bool, object]:
        """P15 Step5：AI 仲裁 provider/ai_review 组装（p12/p14 分支共用）。

        解析配置：settings_service（SQLite）> env（container 未初始化时，如测试/CLI）；
        仲裁 provider = **当前激活 LLM 供应商**（默认 .env 魔塔 ModelScope 免费
        额度；GUI 可切换）；TokenGuard context="arbitration:<pdf_stem>"——与文献
        对话上下文完全隔离（Q5），按篇重置计数。
        仲裁失败/未配置 provider → 跳过仲裁继续本地落地（不阻塞解析）。
        返回 (ai_review, provider)；provider 为 None 时调用方不启用仲裁。
        """
        try:
            from .container import get_settings_service as _svc
            parse_cfg = _svc().get_parse()
        except Exception:  # noqa: BLE001
            parse_cfg = {"mode": self.settings.parse_mode,
                         "ai_review": self.settings.ai_review}
        ai_review = bool(parse_cfg.get("ai_review", True))
        provider = None
        if ai_review:
            # 当前激活供应商：settings_service > env（DEEPSEEK_* 兜底）
            active = None
            guard = None
            try:
                from .container import get_guard, get_settings_service as _svc2
                active = _svc2().get_active_provider(masked=False)
                guard = get_guard()
            except Exception:  # noqa: BLE001 - 容器未初始化（测试/CLI）
                guard = None
            if not active or not active.get("api_key"):
                if self.settings.deepseek_api_key:
                    active = {"id": "deepseek", "name": "DeepSeek",
                              "base_url": self.settings.deepseek_base_url,
                              "model": self.settings.deepseek_model,
                              "api_key": self.settings.deepseek_api_key}
            if active and active.get("api_key"):
                from paperparse.core.dual_ai_review import OpenAICompatProvider
                stem = Path(pdf_path).stem
                if guard is not None:
                    guard.reset_context("arbitration:" + stem)   # 按篇重置（Q5 隔离）
                provider = OpenAICompatProvider(
                    api_key=active["api_key"],
                    base=active.get("base_url", ""),
                    model=active.get("model", ""),
                    on_usage=None if guard is None else
                    (lambda pt, ct, _a=active, _s=stem: guard.record(
                        "arbitration:" + _s, pt, ct,
                        provider=_a.get("id", ""), model=_a.get("model", ""))))
            else:
                logger.warning("未配置 LLM 供应商，跳过 AI 仲裁（本地落地仍进行）")
        return ai_review, provider

    def _ensure_mineru_md(self, pdf_path: str, work: Path) -> Path:
        """P15 Step5：mineru full.md 获取 + md5 缓存（p14 文本基底）。

        extract_v4_batch **无 md5 缓存**（每次重新上传耗配额）→ 这里按 PDF md5
        缓存：命中（meta.json.pdf_md5 == 当前 md5 且 mineru_full.md 存在）直接
        复用；否则经 MineruClient 拉取（v4 通道产物含解压到本地的 full.md =
        raw_path），复制到 work/mineru_full.md 并记录 meta.json。
        返回 mineru_full.md 路径。
        """
        import hashlib
        import json
        import shutil
        import time

        cur = hashlib.md5(Path(pdf_path).read_bytes()).hexdigest()
        meta: dict = {}
        if (work / "meta.json").exists():
            try:
                meta = json.loads((work / "meta.json").read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 - meta 损坏按未缓存处理
                meta = {}
        if meta.get("pdf_md5") == cur and (work / "mineru_full.md").exists():
            logger.info("mineru full.md 缓存命中: %s", work / "mineru_full.md")
            return work / "mineru_full.md"
        from paperparse.config import load_config
        from paperparse.core.mineru_client import MineruClient
        mblocks = MineruClient(load_config()).extract_v4_batch(
            pdf_path, workdir="work/mineru_cache")
        if not getattr(mblocks, "raw_path", None) \
                or not Path(mblocks.raw_path).exists():
            raise EngineError("mineru v4 产物缺 full.md（raw_path 为空）")
        work.mkdir(parents=True, exist_ok=True)
        shutil.copy2(mblocks.raw_path, work / "mineru_full.md")
        (work / "meta.json").write_text(json.dumps(
            {"pdf_md5": cur, "pipeline": "p14",
             "created": time.strftime("%Y-%m-%d %H:%M:%S")},
            ensure_ascii=False, indent=2), encoding="utf-8")
        return work / "mineru_full.md"

    def parse_pdf_local(self, pdf_path: str | Path, run_id: str | None = None) -> dict:
        """本地解析降级（parser=pymupdf，MinerU 不可用时）。"""
        result = self._api.process_pdf(
            str(Path(pdf_path).resolve()),
            parser="pymupdf",
            out_dir=self.settings.engine_work_root,
            run_id=run_id,
            parse_only=True,
        )
        return _wrap("process_pdf", result)

    # ---------------------------------------------------------- 翻译+总结
    def _kb_dir_for_library(self, doi_dir: str) -> Path:
        """knowledge_base/<规范库目录名>/（T4：变体单一来源目录）。

        优先经容器注入的 kb 服务取根（可配置 kb 路径）；未注入/容器未初始化
        （单测/独立调用）回退默认根 APP_DATA_DIR/knowledge_base。
        """
        from ..config import APP_DATA_DIR

        kb_root = APP_DATA_DIR / "knowledge_base"
        kb = self._get_kb()
        if kb is not None:
            try:
                kb_root = kb.root()
            except Exception:  # noqa: BLE001 - 单测 stub 无 root() 回退默认根
                pass
        folder = Path(kb_root) / doi_dir
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    @staticmethod
    def _write_kb_variants(doc, kb_dir: Path) -> dict:
        """渲染 zh.md / en_zh.md 写入 **library 资源目录**（P0-B 2026-09-12 归位）。

        用户模型（REWORK-20260911-file-ui §3.5）：**翻译是文献的翻译 → 属 library**；
        `knowledge_base/` 只放知识库内容（编译产物/笔记/QA 卡片）。旧实现直写 kb，
        与"未编译不算知识库"冲突、且同一份译文两处各一份 → stale（实测 cej 的 kb
        副本比 library 少 87 段译文）。

        kb 侧读取有单源回退（`kb_service.read_file`/`tree` 会回 library 取，
        并按 mtime 择新），所以旧数据无需迁移、也不会读到旧副本。

        zh      ← render_variant("zh")       纯中文，无对照
        en_zh   ← render_variant("translated")  英上中下双语对照
        (D16：六维总结并入 L1 编译 _note.md，summary.md 停生成——避免重复 LLM/文件冗余)
        """
        from paperparse.core.markdown_render import render_variant

        files = {
            "zh.md": render_variant(doc, "zh"),
            "en_zh.md": render_variant(doc, "translated"),
        }
        kb_dir.mkdir(parents=True, exist_ok=True)
        paths: dict[str, str] = {}
        for name, md in files.items():
            fp = kb_dir / name
            fp.write_text(md, encoding="utf-8")
            paths[name] = str(fp)
        return paths

    def combined_translate(self, document_json: str | Path, template: str | None = None) -> dict:
        """M5 combined：翻译+总结（★paperkb 执行，写回 document.json）→ 渲染变体。

        M3b：LLM 翻译/总结已迁 paperkb（共用上下文缓存命中；产物落盘不进问答上下文）；
        paperparse 只保留渲染（normalize + kb 变体）。
        T4：变体单一来源迁移——zh.md / en_zh.md / summary.md **直写 knowledge_base/<DOI>/**
        （library 不再生成 <DOI>.md 与 variants/，旧残留由 _remove_stale_doi_files 清理）。
        template: 保留签名兼容（模板切换重渲染走 rerender_paper，重渲染 kb en_zh.md）。
        """
        p = str(Path(document_json).resolve())
        logger.info("M5 combined 翻译+总结（paperkb）: %s template=%s", p, template)
        # 1) paperkb 翻译+总结（写回 text_zh/ai_summary；LLM 由注入的 kbmeta 适配）
        tr = self._get_kbmeta().translate_now(p)
        # 2) 渲染（paperparse 保留段：LaTeX 规范化 + 变体直写 **library**）
        from paperparse.core.document_builder import load_document, output_dir_name, save_document
        from paperparse.core.latex_normalize import normalize_document

        doc = load_document(p)
        norm = normalize_document(doc)
        save_document(doc, p)
        doi_dir = output_dir_name(doc.metadata.doi, doc.metadata.source_pdf)
        paper_dir = _paper_dir(p)
        # P0-B（2026-09-12）：变体（译文）写 library 资源目录 = 翻译的真相源
        paths = self._write_kb_variants(doc, paper_dir)
        # 清理 library 旧结构残留（<DOI>.md/.zh.md/.summary.md/.en.md/.pdf、variants/）
        self._remove_stale_doi_files(paper_dir, doi_dir)
        self._remove_legacy_paper_md(paper_dir)
        result = {"library_dir": str(paper_dir), "kb_dir": str(self._kb_dir_for_library(doi_dir)),
                  "variants": paths, "document_json": p, "translate": tr}
        if norm:
            result["normalized"] = norm
        return result

    def rerender_paper(self, document_json: str | Path, template: str) -> dict:
        """按模板重渲染中英对照主产物 `library/<资源目录>/en_zh.md`（不重新翻译）。

        T06：模板切换后重渲染；P0-B（2026-09-12）起变体真相源在 **library**
        （kb 侧读取回退 + 按 mtime 择新，旧 kb 副本不会遮住新渲染结果）。
        """
        from paperparse.core.markdown_render import render
        from paperparse.core.document_builder import load_document, output_dir_name

        p = Path(document_json).resolve()
        doc = load_document(str(p))
        doi_dir = output_dir_name(doc.metadata.doi, doc.metadata.source_pdf)
        md = render(doc, template=template)
        paper_dir = _paper_dir(p)
        paper_dir.mkdir(parents=True, exist_ok=True)
        en_zh = paper_dir / "en_zh.md"
        en_zh.write_text(md, encoding="utf-8")
        self._remove_stale_doi_files(paper_dir, doi_dir)
        return {"paper_md": str(en_zh), "kb_dir": str(self._kb_dir_for_library(doi_dir)),
                "template": template}

    def root_kb(self):
        """便捷获取 kb 服务（未构造注入时返回 None；不 import container）。"""
        return self._get_kb()

    # ---------------------------------------------------------- 就地导出（T01/G5）
    def sanitize_document(self, document_json: str | Path) -> dict:
        """G5：清洗解析产物中的 MinerU 图片占位符（`<!-- image -->` 等）并保存回 document.json。

        占位符混在图注/正文开头会破坏引擎图-题注匹配（_fig_key 从开头匹配 Figure n.），
        导致导出 md 缺图片行。清洗后 document.json 干净，检索/渲染/导出全部受益。
        """
        import re
        from paperparse.core.document_builder import load_document, save_document

        p = Path(document_json).resolve()
        doc = load_document(str(p))
        # P2-C：兼容 `<!-- image -->` / `<!-- image-1 -->` / `<!-- image1 -->` / `<!-- img 2 -->` 等
        pat = re.compile(r"<!--\s*(?:image|img)[\s\-_]*\d*\s*-->", re.IGNORECASE)
        changed = False
        for para in doc.paragraphs:
            for attr in ("text_en", "text_zh"):
                t = getattr(para, attr, None)
                if t and pat.search(t):
                    setattr(para, attr, pat.sub("", t).strip())
                    changed = True
        for fig in doc.figures:
            if fig.caption and pat.search(fig.caption):
                fig.caption = pat.sub("", fig.caption).strip()
                changed = True
        if changed:
            save_document(doc, p)
        return {"changed": changed, "path": str(p)}

    @staticmethod
    def _ensure_figures(md: str, doc) -> str:
        """G5：渲染后补齐缺失的图片行（引擎图-题注匹配失败时兜底）。

        对每个 figures（file 形如 images/F001.png），若 md 中无该图行，
        则在对应 "Figure N" 题注行前插入 `![](<file>)`；同时清理残留占位符。
        """
        import re

        # P2-C：清理全部占位符变体（含 `<!-- image-1 -->`）；补齐图片行（Fig/Table/Scheme/补充图 S1）
        md = re.sub(r"<!--\s*(?:image|img)[\s\-_]*\d*\s*-->", "", md, flags=re.IGNORECASE)
        for fig in doc.figures:
            cap = (fig.caption or "").strip()
            m = re.match(r"^(?:fig(?:ure)?|table|scheme)\.?\s*(S?\d+)", cap, re.I)
            if not m:
                continue
            num = m.group(1)
            img_line = f"![]({fig.file})"
            if img_line in md:
                continue
            lines = md.split("\n")
            for i, ln in enumerate(lines):
                if re.search(rf"\bFigure\s*{re.escape(num)}\b", ln, re.I) and not ln.strip().startswith("!["):
                    if i > 0 and lines[i - 1].strip().startswith("!["):
                        break  # 题注前已有图
                    lines.insert(i, img_line)
                    break
            md = "\n".join(lines)
        return md

    @staticmethod
    def _remove_legacy_paper_md(out_dir) -> None:
        """P2-3：移除引擎解析阶段生成的 paper.md（与 <DOI>.md 中英对照产物重复）。"""
        from pathlib import Path
        stale = Path(out_dir) / "paper.md"
        try:
            if stale.exists():
                stale.unlink()
        except OSError:  # noqa: BLE001 - 删除失败不阻塞
            logger.warning("清理重复 paper.md 失败: %s", stale)

    @staticmethod
    def _remove_stale_doi_files(out_dir, doi_prefix: str) -> None:
        """T4：删除旧结构带 DOI 前缀的产物（<DOI>.md/.zh.md/.summary.md/.en.md/.pdf
        与 variants/）——源已迁移：library 只留 en.md/source.pdf，变体单一来源在 kb。
        存量清理交给 T8 迁移脚本；此处仅在生成源迁移后顺手清理，失败不阻塞。"""
        from pathlib import Path
        d = Path(out_dir)
        for suffix in (".md", ".zh.md", ".summary.md", ".en.md", ".pdf"):
            stale = d / f"{doi_prefix}{suffix}"
            try:
                if stale.exists():
                    stale.unlink()
            except OSError:  # noqa: BLE001 - 删除失败不阻塞
                logger.warning("清理旧产物失败: %s", stale)
        variants = d / "variants"
        try:
            if variants.is_dir():
                import shutil
                shutil.rmtree(variants, ignore_errors=True)
        except OSError:  # noqa: BLE001
            logger.warning("清理旧 variants/ 失败: %s", variants)

    def _post_parse_clean(self, document_json: str | Path) -> None:
        """P2-C：解析完成后统一清洗并重渲染英文版（en.md）。

        引擎的 parse-only 导出直接 render_variant 写 en.md，**不**经过
        sanitize_document/_ensure_figures——若 document.json 仍含 MinerU
        图片占位符（`<!-- image -->`），en.md 里图片行缺失、前端不显示。
        这里补上：先清洗 document.json，再按图注补齐图片行重写 en.md。
        2026-08-26：改用 render_clean（**不经过 J2 模板**——模板曾致空行失控）；
        目标文件为 en.md（与 PDF 等价的干净原始版本）；模板版 <DOI>.en.md 废弃清理。
        """
        from paperparse.core.document_builder import load_document, output_dir_name
        from paperparse.core.markdown_render import render_clean

        p = Path(document_json).resolve()
        self.sanitize_document(p)
        doc = load_document(str(p))
        doi_dir = output_dir_name(doc.metadata.doi, doc.metadata.source_pdf)
        # 2026-08-26 修复：篇目录自适应（兼容 intermediate；原 parent.parent
        # 在 library/<DOI>/document.json 场景错写 library/ 顶层 → 顶层重复产物）
        out_dir = _paper_dir(p)  # library/<DOI>/
        en_md = out_dir / "en.md"
        md = render_clean(doc)
        md = self._ensure_figures(md, doc)
        en_md.write_text(md, encoding="utf-8")
        # 模板版 <DOI>.en.md 废弃 → 清理残留
        stale = out_dir / f"{doi_dir}.en.md"
        try:
            if stale.exists():
                stale.unlink()
        except OSError:  # noqa: BLE001 - 清理失败不阻塞
            logger.warning("清理模板版 %s 失败", stale.name)
        self._remove_legacy_paper_md(out_dir)

    @staticmethod
    def _resolve_source_pdf(source_pdf: str | None, out_dir: Path,
                            input_root: Path | None = None) -> Path | None:
        """定位可拷贝的源 PDF 路径；找不到返回 None（缺 PDF 不阻塞导出，幂等）。

        T4 回归根因：P14 的 to_article_document 把 metadata.source_pdf 存为**裸文件名**
        （pdf.name），_inplace_export 原用 Path(...).resolve().exists() 相对 CWD 判断恒
        False → library/<DOI>/source.pdf 从未生成。这里三级兜底：
          1) 原值是绝对路径且存在 → 直接返回；
          2) 原值能相对解析到存在文件 → 返回；
          3) 否则按 basename 在引擎输入暂存（input/<run_id>/<原始文件名>.pdf）与规范库
             内搜索（rglob），复用原始上传 PDF——覆盖重新导入场景（新 run_id 暂存）。
        搜索失败/目录不存在一律静默跳过，不抛。
        """
        if not source_pdf:
            return None
        cand = Path(source_pdf)
        if cand.is_absolute():
            if cand.exists():
                return cand
        else:
            r = cand.resolve()
            if r.exists():
                return r
        base = cand.name
        roots: list[Path] = []
        if input_root is not None:
            roots.append(Path(input_root))
        if out_dir is not None:
            roots.append(Path(out_dir).parent)  # 规范库根 library/
        for root in roots:
            if not root.is_dir():
                continue
            try:
                for hit in root.rglob(base):
                    if hit.is_file():
                        return hit
            except OSError:  # noqa: BLE001 - 搜索失败不阻塞
                continue
        return None

    def _inplace_export(self, document_json: str | Path) -> dict:
        """就地导出：规范库 library/<DOI>/ 只保留解析产物（en.md 干净版 + source.pdf 备份）。

        T4：变体（zh/en_zh/summary）单一来源迁移到 knowledge_base/<DOI>/（combined_translate
        直写），此处**不再生成** <DOI>.md/.zh.md/.summary.md/.en.md/.pdf 与 variants/
        （旧残留由 _remove_stale_doi_files 清理，存量由 T8 迁移脚本处理）。
        document.json/images/audit 已由解析阶段就地落在 <DOI>/，无需复制。
        G5：导出前清洗 document.json 占位符；渲染后补齐缺失图片行。
        """
        import shutil
        from paperparse.core.document_builder import load_document, output_dir_name
        from paperparse.core.markdown_render import render_clean

        p = Path(document_json).resolve()
        self.sanitize_document(p)
        doc = load_document(str(p))
        doi_dir = output_dir_name(doc.metadata.doi, doc.metadata.source_pdf)
        # 2026-08-26 修复：篇目录自适应（兼容 intermediate；原 parent.parent
        # 在 library/<DOI>/document.json 场景错写 library/ 顶层）
        out_dir = _paper_dir(p)  # library/<DOI>/
        out_dir.mkdir(parents=True, exist_ok=True)

        paths: dict[str, str] = {"output_dir": str(out_dir)}
        # en_md：干净版主产物（与 PDF 等价，无 frontmatter/参考文献），缺则补生成
        en_md = out_dir / "en.md"
        if not en_md.exists():
            md = render_clean(doc)
            md = self._ensure_figures(md, doc)
            en_md.write_text(md, encoding="utf-8")
        paths["en_md"] = str(en_md)
        # 源 PDF 备份（规范名 source.pdf；旧 <DOI>.pdf 由清理删除）
        # T4 回归根因：P14 的 to_article_document 把 metadata.source_pdf 存为**裸文件名**
        # （pdf.name），原 `Path(...).resolve().exists()` 相对 CWD 判断恒 False → source.pdf
        # 被跳过复制 → library 无源 PDF → kb 经 sync_source_to_kb 也随之缺失。
        # 此处兜底：原值不可达时按 basename 在引擎输入暂存（input/<run_id>/<原始文件名>.pdf）
        # 与规范库内搜索复用原始上传 PDF，保证 library 始终有 source.pdf（幂等、不阻塞）。
        pdf_src = self._resolve_source_pdf(
            doc.metadata.source_pdf, out_dir, Path(self.settings.engine_input_root))
        if pdf_src is not None:
            pdf_dest = out_dir / "source.pdf"
            shutil.copy2(pdf_src, pdf_dest)
            paths["pdf"] = str(pdf_dest)
        img_dir = out_dir / "images"
        paths["images"] = str(img_dir) if img_dir.is_dir() else ""
        paths["document_json"] = str(p)
        audit_dir = out_dir / "audit"
        paths["audit"] = str(audit_dir) if audit_dir.is_dir() else ""
        # T4：删除旧结构带 DOI 前缀产物与 variants/；另清理引擎解析阶段残留 paper.md
        self._remove_stale_doi_files(out_dir, doi_dir)
        self._remove_legacy_paper_md(out_dir)
        return paths

    def _clean_staging(self, document_json: str | Path) -> None:
        """清理遗留引擎 export_package 的临时暂存副本（engine_out_root/<DOI>）。"""
        import shutil
        from paperparse.core.document_builder import doi_dir_name, load_document

        p = Path(document_json).resolve()
        doi_dir = ""
        try:
            doc = load_document(str(p))
            doi_dir = doi_dir_name(doc.metadata.doi) or ""
        except Exception:  # noqa: BLE001 - 读失败按父目录名兜底
            doi_dir = ""
        name = doi_dir or p.parent.name
        staging = Path(self.settings.engine_out_root).resolve() / name
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    def export(self, document_json: str | Path) -> dict:
        """导出完整产物包：就地渲染全部变体 + 拷贝源 PDF（统一规范库内，无重复目录）。"""
        return self._inplace_export(document_json)

    def ensure_source_pdf(self, document_json: str | Path,
                          source_pdf: str | Path | None = None) -> str:
        """确保 `library/<资源>/source.pdf` 存在（幂等；**不渲染任何变体**）。

        2026-09-12 用户反馈：parse / parse_compile 模式不走 `_inplace_export`
        （那是 full 模式 `export()` 的路径），于是 library 从来没有 source.pdf
        → 纳入知识库后「原文层四件」永远缺 PDF 源文件。
        优先用调用方给的权威路径（`papers.pdf_path`＝原始上传 PDF），
        否则回退 document.json 的 `metadata.source_pdf`（含 `_resolve_source_pdf` 三级兜底）。
        找不到源 PDF 时返回 ""，不抛、不阻塞任务。
        """
        p = Path(document_json).resolve()
        dest = p.parent / "source.pdf"
        try:
            if dest.is_file() and dest.stat().st_size > 0:
                return str(dest)
        except OSError:  # noqa: BLE001 - 探测失败按"需补齐"处理
            pass
        src: Path | None = Path(str(source_pdf)) if source_pdf else None
        if src is not None and not src.is_file():
            src = None
        if src is None:
            try:
                from paperparse.core.document_builder import load_document

                doc = load_document(str(p))
                src = self._resolve_source_pdf(
                    doc.metadata.source_pdf, p.parent,
                    Path(self.settings.engine_input_root))
            except Exception as e:  # noqa: BLE001 - 读文档失败按无源处理
                logger.warning("补 source.pdf 时读取 document.json 失败: %s", e)
                src = None
        if src is None:
            logger.warning("无法定位源 PDF，library 将缺 source.pdf: %s", p)
            return ""
        import shutil

        try:
            shutil.copy2(src, dest)
        except OSError as e:
            logger.warning("拷贝 source.pdf 失败: %s", e)
            return ""
        return str(dest)

    # ---------------------------------------------------------- 局部查询
    def query(self, document_json: str | Path, para_ids: list[str] | None = None,
              section: str | None = None, include: str = "en",
              limit_chars: int | None = None) -> dict:
        """局部查询段落/章节（不返回全文，limit_chars 受控）。"""
        return self._api.query_paragraphs(
            str(Path(document_json).resolve()),
            para_ids=para_ids,
            section=section,
            include=include,
            limit_chars=limit_chars or self.settings.query_limit_chars,
        )

    # ---------------------------------------------------------- 问答写回
    def qna_writeback(self, document_json: str | Path,
                      qa_pairs: list[dict], label: str = "追问与回答") -> dict:
        """把讨论问答写回 ai_summary（仅用户明确要求时调用）；产物就地导出并清理暂存。

        本地实现（不依赖 paperparse api.qna/export_package，2026-08-27 瘦身后）。
        """
        from paperparse.core.document_builder import load_document, output_dir_name, save_document

        p = Path(document_json).resolve()
        pairs = [{"question": str(x.get("question", "")).strip(),
                  "answer": str(x.get("answer", "")).strip()}
                 for x in qa_pairs if x.get("question") or x.get("answer")]
        if not pairs:
            raise ValueError("qna 需要至少一个问答对")
        doc = load_document(str(p))
        doc.ai_summary = doc.ai_summary or {}
        entries = "\n\n".join(
            "Q: %s\nA: %s" % (x["question"], x["answer"]) for x in pairs)
        cur = (doc.ai_summary.get(label) or "").strip()
        doc.ai_summary[label] = cur + ("\n\n" if cur else "") + entries
        save_document(doc, p)
        # P0-B（2026-09-12）：变体重渲染写 library（翻译真相源；kb 侧读取回退）
        doi_dir = output_dir_name(doc.metadata.doi, doc.metadata.source_pdf)
        paper_dir = _paper_dir(p)
        variants = self._write_kb_variants(doc, paper_dir)
        export = self._inplace_export(p)
        self._clean_staging(p)
        return {"label": label, "appended": len(pairs),
                "total_entries": doc.ai_summary[label].count("Q: "),
                "library_dir": str(paper_dir),
                "kb_dir": str(self._kb_dir_for_library(doi_dir)),
                "variants": variants, "export": export}

    # ---------------------------------------------------------- 文档摘要（轻量）
    def doc_summary(self, document_json: str | Path) -> dict:
        """提取元数据 + 章节索引（**不含全文**）：前端卡片 + 对话稳定前缀。

        稳定前缀要求：输出逐字节固定（同一 document.json 不随调用变化）。
        """
        from paperparse.core.document_builder import load_document
        try:
            doc = load_document(str(Path(document_json).resolve()))
        except Exception as e:  # noqa: BLE001 - 引擎 PaperError 等统一转 EngineError
            raise EngineError(f"document.json 读取失败: {e}") from e
        meta = doc.metadata

        # 章节索引：章节名 → 段落 ID 列表（保持文档顺序，固定输出）
        section_map: dict[str, list[str]] = {}
        order: list[str] = []
        translated_paras = 0
        for para in doc.paragraphs:
            sec = para.section or ""
            if sec not in section_map:
                section_map[sec] = []
                order.append(sec)
            section_map[sec].append(para.para_id)
            if para.text_zh:
                translated_paras += 1

        sections = [
            {"section": sec, "para_ids": section_map[sec], "count": len(section_map[sec])}
            for sec in order
        ]
        return {
            "title": meta.title,
            "authors": list(meta.authors),
            "abstract_preview": (meta.abstract or "")[:500],
            "keywords": list(meta.keywords),
            "doi": meta.doi,
            "journal": meta.journal,
            "year": meta.year,
            "paragraph_count": len(doc.paragraphs),
            "translated_paragraphs": translated_paras,
            "figure_count": len(doc.figures),
            "sections": sections,
            "ai_summary_keys": list((doc.ai_summary or {}).keys()),
        }
