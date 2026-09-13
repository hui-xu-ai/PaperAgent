# -*- coding: utf-8 -*-
"""后台任务流水线（T05）：解析 → 翻译+总结 → 导出。

- **并发/串行按供应商 capability 判定**：仅支持并发的供应商（如 DeepSeek）允许
  N 个 worker 并行处理多论文；智谱 GLM 与未知供应商 → 串行（1 worker，保守，
  避免 MinerU/LLM 限流与额度竞争）。
- 任务状态存 SQLite，SSE 向前端推送进度
- **翻译全文不进对话历史**：流水线只更新任务/论文状态与导出路径
"""
from __future__ import annotations

import logging
import queue
import threading
import time

from ..config import Settings
from .engine_service import EngineService, EngineError, TaskCancelled, parse_source_label
from .event_bus import EventBus
from .store import Store

logger = logging.getLogger(__name__)

# 流水线阶段进度（粗粒度：解析 5-40，翻译 40-90，导出 90-100）
STAGES = [
    (5, "准备解析"),
    # P12：双通道解析（mineru-v4 主 + PaddleOCR-VL-1.6 辅 + AI 仲裁），失败自动降级单通道
    (20, "双通道解析中（MinerU 精准 + PaddleOCR-VL + AI 仲裁，约 2-6 分钟）"),
    (40, "解析完成，开始 AI 翻译+总结"),
    (70, "AI 翻译+总结中（全文一次性处理）"),
    (90, "回填渲染，导出备份包"),
    (100, "完成"),
]

# parse_compile 完成文案：按 _assemble_kb 回报的**真实** compile 状态生成
# （旧实现恒定写"已纳入知识库并编译 L1"，实际被 done 幂等挡掉时是"说了没做"）。
PARSE_COMPILE_TEXT = {
    "queued": "已入队 L1 编译（后台编译中）",
    "skipped_done": "已在知识库（跳过重复编译）",
    "skipped_no_doc": "⚠ 未找到解析产物 document.json，未入队编译",
    "disabled": "⚠ 自动编译已关闭（设置中心-解析 可开启），本次未入队",
    "skipped_no_rid": "⚠ 未能为该文献生成标识，未入队编译",
    "error": "⚠ 编译入队失败（详见日志）",
}


def parse_compile_done_msg(parse_label: str, kb_status: dict | None) -> str:
    """parse_compile 任务完成文案（如实反映编译入队结果）。"""
    st = str((kb_status or {}).get("compile") or "").strip()
    text = PARSE_COMPILE_TEXT.get(st, "⚠ 编译状态未知（详见日志）")
    return f"解析+编译完成（来源：{parse_label}，未翻译，{text}）"


class TaskManager:
    """后台任务管理器：单 worker 串行执行流水线。"""

    def __init__(self, settings: Settings, store: Store, engine: EngineService,
                 event_bus: EventBus | None = None):
        self.settings = settings
        self.store = store
        self.engine = engine
        self.event_bus = event_bus
        self._queue: queue.Queue[int] = queue.Queue()
        self._cancel_evt = threading.Event()   # P15：当前解析任务取消标志（官方云队列等待可取消）
        self._recover_orphans()
        # T（并发）：按**激活供应商并发能力**决定并行 worker 数。仅支持并发的供应商
        # （如 DeepSeek）允许多论文并行；智谱 GLM 与未知供应商 → 串行（1，保守）。
        n = self._desired_workers()
        self._workers: list[threading.Thread] = []
        for i in range(n):
            t = threading.Thread(target=self._worker_loop,
                                 name=f"pipeline-worker-{i}", daemon=True)
            t.start()
            self._workers.append(t)
        logger.info("TaskManager 已启动（并发 worker=%d，按供应商 capability 判定）", n)

    def _desired_workers(self) -> int:
        """按激活供应商并发能力决定并行 worker 数（无法判定 → 1 串行，保守）。"""
        try:
            from .container import get_settings_service
            return get_settings_service().get_pipeline_workers()
        except Exception:  # noqa: BLE001 - 判定失败按串行兜底，绝不并发误伤限流
            logger.warning("并发判定失败，回退串行（worker=1）")
            return 1

    def _recover_orphans(self) -> None:
        """服务重启后恢复未完成任务（running/pending → 重新入队），避免死任务。"""
        recovered = 0
        for paper in self.store.list_papers(limit=500):
            latest = self.store.latest_task(paper["id"], "pipeline")
            if latest and latest["state"] in ("pending", "running"):
                self.store.update_task(latest["id"], state="pending", progress=0,
                                       message="服务重启，重新排队")
                self._queue.put(paper["id"])
                recovered += 1
        if recovered:
            logger.info("恢复 %d 个未完成任务", recovered)

    # ---------------------------------------------------------- 提交
    def submit_pipeline(self, paper_id: int) -> None:
        """提交论文流水线任务（幂等：已排队/进行中不重复提交）。"""
        latest = self.store.latest_task(paper_id, "pipeline")
        if latest and latest["state"] in ("pending", "running"):
            return
        self.store.create_task(paper_id, "pipeline")
        self._queue.put(paper_id)

    # ---------------------------------------------------------- P15 取消
    def running_count(self) -> int:
        """当前有任务的 worker 数（优雅关停时展示/落盘用）。"""
        try:
            return sum(1 for t in self._workers if t.is_alive())
        except Exception:  # noqa: BLE001
            return 0

    def active_tasks(self) -> int:
        """DB 里 pending/running 的任务数（关停时判断"是否还有在跑的活"）。"""
        try:
            return sum(1 for p in self.store.list_papers(limit=500)
                       if (self.store.latest_task(p["id"], "pipeline") or {}).get("state")
                       in ("pending", "running"))
        except Exception:  # noqa: BLE001
            return 0

    def cancel_current(self) -> str:
        """请求取消**当前正在解析**的任务（官方云队列等待期间用户可随时取消）。

        语义：置取消标志 → 解析链在下一个取消检查点（官方云队列等待每轮）抛
        TaskCancelled → 任务标记 failed（message=已取消，用户可见），**不降级、
        不重试**。取消标志在下一个任务开始时自动清除。
        """
        if not self._cancel_evt.is_set():
            self._cancel_evt.set()
            return "ok"
        return "已请求取消"

    # ---------------------------------------------------------- worker
    def _worker_loop(self) -> None:
        while True:
            paper_id = self._queue.get()
            try:
                self._run_pipeline(paper_id)
            except Exception as e:  # noqa: BLE001 - 兜底：**必须 fail，绝不静默卡死**
                logger.exception("流水线异常 paper_id=%s", paper_id)
                # 修复进度条卡死根因：兜底异常统一标记失败 + 事件告警
                self._fail(paper_id, f"内部错误（未预期异常）: {e}")
            finally:
                self._queue.task_done()
                # P12F 批量限速：队列还有任务时，篇间间隔（防 Mineru/PaddleOCR/AI 限流）
                if not self._queue.empty():
                    time.sleep(self._parse_interval_sec())

    def _set_task(self, paper_id: int, state: str, progress: int,
                  message: str, error: str = "") -> None:
        task = self.store.latest_task(paper_id, "pipeline")
        if task:
            self.store.update_task(task["id"], state=state, progress=progress,
                                   message=message, error=error)
        logger.info("paper=%s %s%% %s", paper_id, progress, message)
        if self.event_bus:
            self.event_bus.publish(
                "info", "task", "progress",
                f"论文[{paper_id}] {message}",
                {"paper_id": paper_id, "state": state, "progress": progress})

    def _run_pipeline(self, paper_id: int) -> None:
        paper = self.store.get_paper(paper_id)
        if not paper:
            logger.warning("论文不存在 paper_id=%s，跳过", paper_id)
            return
        pdf_path = paper["pdf_path"]
        self._set_task(paper_id, "running", 5, STAGES[0][1])
        self.store.update_paper(paper_id, status="parsing")

        # 1) 解析（engine_service 内部沿降级链：v4 精准 → v1 免费 → pymupdf 本地）
        try:
            self._set_task(paper_id, "running", 20, STAGES[1][1])
            # P15：取消/等待回调——官方云队列繁忙时等待重试，状态实时可见，用户可取消
            self._cancel_evt.clear()
            result = self.engine.parse_pdf(
                pdf_path,
                cancel_check=lambda: self._cancel_evt.is_set(),
                on_wait=lambda sec, at: self._set_task(
                    paper_id, "running", 20,
                    f"官方云队列繁忙，等待重试中（已等待 {sec}s，第 {at} 次，可取消）"))
        except TaskCancelled as e:
            self._fail(paper_id, f"已取消：{e}")
            self.store.update_paper(paper_id, status="failed",
                                    error=f"已取消：{e}")
            return
        except EngineError as e:
            self._fail(paper_id, f"解析失败（全部通道）: {e}")
            return
        doc_json = result["document_json"]
        # P2-B：解析来源透明化——无论成功走哪个通道，都落库 + 事件上报（不再只报降级）
        parse_src = str(result.get("parse_source") or "").strip()
        parse_label = parse_source_label(parse_src)
        self.store.update_paper(paper_id, doc_json=doc_json, status="translating",
                                parse_source=parse_src)
        warns = result.get("warnings") or []
        if "pymupdf" in parse_src.lower():
            warn_msg = (f"⚠ 已降级本地 pymupdf 解析（低精度）。云端失败原因："
                        + ("；".join(warns) if warns else "未知"))
            logger.warning("paper=%s %s", paper_id, warn_msg)
            if self.event_bus:
                self.event_bus.publish("warning", "task", "parse_degraded",
                                       f"论文[{paper_id}] {warn_msg}",
                                       {"paper_id": paper_id, "parse_source": parse_src})
        else:
            # P12/P15：双通道/p14 管线的提示信息（如辅通道异常/AI 仲裁失败）随事件透传
            extra = ""
            if parse_src in ("dual", "p14") and warns:
                extra = "（" + "；".join(warns) + "）"
            if self.event_bus:
                self.event_bus.publish(
                    "info", "task", "parse_done",
                    f"论文[{paper_id}] 解析完成，来源：{parse_label}{extra}",
                    {"paper_id": paper_id, "parse_source": parse_src,
                     "parse_label": parse_label, "warnings": warns})
        self._set_task(paper_id, "running", 40, f"解析完成（{parse_label}），开始 AI 翻译+总结")

        # V11：仅解析模式（mode=parse）→ 跳过翻译/总结/导出
        # P5 点1：mode=parse_compile → 解析 + 纳入知识库 + 编译L1入队，跳过翻译/导出
        mode = paper.get("pipeline_mode") or "full"
        if mode in ("parse", "parse_compile"):
            # 2026-09-12 用户反馈：这两条分支不调 `_inplace_export`（只有 full 的 export() 会），
            # 于是 library 从来没有 source.pdf → 纳入知识库后「原文层四件」永远缺 PDF 源文件。
            # 这里用 papers.pdf_path（原始上传 PDF，权威）幂等补齐，不渲染任何变体。
            self._ensure_library_source_pdf(doc_json, paper_id)
            self._clean_upload_staging(paper)
            if mode == "parse_compile":
                self._write_doi_md5_map(doc_json, paper_id)  # 先登记目录↔DOI/md5 映射
                kb_status = self._assemble_kb(doc_json, paper_id)  # kb 登记 + 自动编译 L1 入队（不翻译）
                done_msg = parse_compile_done_msg(parse_label, kb_status)
            else:
                kb_status = None
                done_msg = f"解析完成（来源：{parse_label}，未翻译，可在阅读器查看英文原文）"
            self.store.update_paper(paper_id, status="parsed", error="")
            self._set_task(paper_id, "done", 45, done_msg)
            if self.event_bus:
                payload = {"paper_id": paper_id, "parse_only": True,
                           "parse_compile": mode == "parse_compile",
                           "parse_source": parse_src}
                # 向后兼容：仅新增字段，不改既有字段语义
                if kb_status is not None:
                    payload["compile_status"] = str(kb_status.get("compile") or "")
                self.event_bus.publish(
                    "info", "task", "task_done",
                    f"论文[{paper_id}] {done_msg}", payload)
            return

        # P12F 复核门控：有待复核项且策略=等待 → 挂起「待复核」，不启动翻译（省 token）
        if self._translate_gate() == "wait":
            pending = self._pending_review(paper)
            if pending > 0:
                self.store.update_paper(paper_id, status="waiting_review", error="")
                self._set_task(paper_id, "waiting_review", 45,
                               f"⏸ 待复核：{pending} 个差异点"
                               f"（复核完成后在任务面板『开始翻译』确认；也可强制翻译）")
                if self.event_bus:
                    self.event_bus.publish(
                        "info", "task", "waiting_review",
                        f"论文[{paper_id}] ⏸ 待复核：{pending} 个差异点"
                        f"（复核完成后在任务面板确认翻译）",
                        {"paper_id": paper_id, "pending": pending})
                return

        # 2) AI 翻译+总结（combined 一次调用，全文只进这一次上下文）
        self._translate_and_export(paper_id, paper, doc_json, parse_src, parse_label)

    def _clean_upload_staging(self, paper: dict) -> None:
        """删除上传暂存副本（`work/upload/<run_id>/`）——PDF 的长期归属只有 library/kb。

        2026-09-12（用户拍板 · 工业级数据布局）：旧行为把上传 PDF 复制到 `input/<run_id>/`
        并**永久留存**（实测 7 份冗余 37MB）。现在暂存落在 `work/`（可清理区），
        解析产物落盘（library/<资源>/source.pdf）后即删除暂存目录；删除失败不影响任务。
        """
        import shutil
        from pathlib import Path

        pdf_path = (paper or {}).get("pdf_path") or ""
        if not pdf_path:
            return
        try:
            stage = Path(pdf_path).resolve().parent
            root = Path(self.settings.engine_input_root).resolve()
            if stage != root and stage.is_relative_to(root) and stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
                logger.info("已清理上传暂存: %s", stage)
        except Exception as e:  # noqa: BLE001 - 清理失败不阻塞流水线
            logger.warning("清理上传暂存失败: %s", e)

    def _try_conversation_flow(self, paper: dict, doc_json: str) -> bool:
        """非 DeepSeek 官方：用**对话式一次流转**替代"单发编译 + 单发翻译"。

        判定链（任一不满足 → 返回 False，走既有 `combined_translate`）：
          1. env `PAPERAGENT_CONVO_FLOW` 未关；
          2. 当前激活供应商 id ≠ `deepseek`（DeepSeek 官方前缀缓存跨请求有效，保持原方案）；
          3. 该篇尚未编译出 `_note.md`（已编译过 → 翻译阶段无需再编译）；
          4. paperkb 适配器支持 `chat_messages`；调用成功且译文覆盖达标。
        成功时把 L1(+L2) 的编译任务标记为 done（**避免后台 worker 再单发编译一次**）。
        """
        import os

        if (os.getenv("PAPERAGENT_CONVO_FLOW") or "1").strip().lower() in (
                "0", "false", "no", "off"):
            return False
        try:
            from .container import get_settings_service, get_kbmeta

            provider = get_settings_service().get_active_provider(masked=False) or {}
            if str(provider.get("id") or "") == "deepseek":
                return False
            key = paper.get("doi") or ""
            if not key:
                return False
            kb = get_kbmeta()
            _c = kb._need_compiler()           # noqa: SLF001 - 复用编译器的键归一化与产物路径
            if _c._note_path(key).exists():    # noqa: SLF001
                return False                   # 已编译过 → 交给既有翻译路径
            vlevel = str(((kb.value_score(key) or {}).get("level")) or "L1")
            levels = ("L1", "L2") if vlevel in ("L2", "L3") else ("L1",)
            logger.info("对话式一次流转（任务流水线）: key=%s levels=%s provider=%s",
                        key, levels, provider.get("name"))
            res = kb.conversation_compile(key, levels=levels, translate=True)
            logger.info("对话式完成: %s", {k: res.get(k) for k in
                                          ("levels", "translated", "targets",
                                           "coverage", "calls")})
            return True
        except Exception as e:  # noqa: BLE001 - 任何异常都回退既有路径（用户可见的失败信息由原路径给出）
            from paperkb.convo import ConvoFallback

            if isinstance(e, ConvoFallback):
                logger.warning("对话式回退既有翻译路径: %s", e)
            else:
                logger.warning("对话式异常（回退既有翻译路径）：%s", e)
            return False

    def _translate_and_export(self, paper_id: int, paper: dict, doc_json: str,
                              parse_src: str, parse_label: str) -> None:
        """翻译+导出段（_run_pipeline 与复核门控续跑共用；翻译输入=复核后最终 document）。"""
        #    前置检查：LLM 未配置时立即失败（防引擎 FakeAI 产生假翻译）
        from .container import llm_ready
        if not llm_ready():
            self._fail(paper_id, "未配置 LLM API Key，无法翻译。"
                                "请在设置中心-模型 配置供应商后重试。")
            return
        # V13：翻译前按任务重置 engine 防护计数（防跨论文累计误拦；
        # 单任务内 12 次/800k 字符上限仍有效防死循环）
        try:
            from .container import get_guard
            get_guard().reset_context("engine")
        except Exception:  # noqa: BLE001 - 重置失败不阻塞（按累计限制兜底）
            pass
        try:
            self._set_task(paper_id, "running", 70, STAGES[3][1])
            # T06：模板 = 每篇覆盖（papers.md_template）→ 全局默认（settings）→ 引擎默认
            tpl = (paper.get("md_template") or "").strip()
            if not tpl:
                try:
                    from .container import get_settings_service
                    tpl = get_settings_service().get_md_template()
                except Exception:  # noqa: BLE001
                    tpl = ""
            # 2026-09-13（用户决策）：**非 DeepSeek 官方**走"对话式一次流转"——
            # 编译（L1 或 L1+L2 写在同一条 user）+ 翻译放在同一条对话里，请求数 3~4 → 2。
            # 失败/覆盖不足 → 回退既有单发路径（`combined_translate` 原样保留）。
            if not self._try_conversation_flow(paper, doc_json):
                self.engine.combined_translate(doc_json, template=tpl or None)
        except EngineError as e:
            self._fail(paper_id, f"翻译+总结失败: {e}")
            return
        except Exception as e:  # noqa: BLE001 - 含 TokenBudgetExceeded（红线拦截）
            from .llm_service import TokenBudgetExceeded
            if isinstance(e, TokenBudgetExceeded):
                self._fail(paper_id, f"⚠ Token 防护拦截: {e}")
            else:
                self._fail(paper_id, f"翻译+总结异常: {e}")
            return

        # 3) 导出备份包
        try:
            self._set_task(paper_id, "running", 90, STAGES[4][1])
            export = self.engine.export(doc_json)
        except EngineError as e:
            self._fail(paper_id, f"导出失败: {e}")
            return

        self.store.update_paper(paper_id, status="translated", error="")
        self._set_task(paper_id, "done", 100, STAGES[5][1])
        # G10：翻译完成后自动为该文献建默认会话（每篇文献一个独立 paper 会话，隔离上下文）
        self._ensure_paper_session(paper_id)
        # M5-C：完整流水线完成 → 自动纳入知识库原文层四件（冻结原则，不覆盖已有 kb）
        self._write_doi_md5_map(doc_json, paper_id)  # 先登记目录↔DOI/md5 映射
        self._assemble_kb(doc_json, paper_id)
        if self.event_bus:
            self.event_bus.publish("info", "task", "task_done",
                                   f"论文[{paper_id}] 翻译流水线完成（解析来源：{parse_label}）",
                                   {"paper_id": paper_id, "export": str(export),
                                    "parse_source": parse_src, "parse_label": parse_label})
        logger.info("论文翻译流水线完成 paper_id=%s export=%s", paper_id, export)

    def ensure_kb_assembled(self, doc_json: str, paper_id: int = 0) -> None:
        """公开包装：确保该篇已纳入 kb + 自动编译入队（A5：retry_export 等旁路分支补调用）。"""
        self._assemble_kb(doc_json, paper_id)

    def _assemble_kb(self, doc_json: str, paper_id: int = 0) -> dict:
        """解析完成后的知识库侧登记（**不再复制文件**；2026-09-11 用户模型）。

        用户模型：**PDF 解析后未编译的不算知识库内容，文献留在 library**，只有用户
        选择编译时才纳入知识库。因此这里：
        - 不再调用 source_sync 复制原文层四件（旧行为：解析即复制 → kb 存的是翻译前
          的冻结副本，导致编译读到过期正文，实际发生过的 cej 少 87 段译文即此因）；
        - 只登记目录↔DOI/md5 映射（供无 DOI 文献与目录定位）；
        - auto_compile 开时入队 L1 —— 真正的"纳入 kb"由编译入口（paperkb
          Compiler.compile 前的 ensure）在编译那一刻完成，拿到的是最新正文。
        「仅解析」模式不触发（与知识库无关，用户决策）；失败仅告警不阻塞任务。

        **P0-B step4（2026-09-12）：无 DOI 文献同样能编译**——旧实现在这里对无 DOI
        直接 return，于是"没有 DOI = 永远进不了知识库/编译"。现在改为按**内容指纹
        RID（`nd-<pdf_md5 前12>`）**登记元数据并入队；编译链的键解析（paperkb.resource）
        已支持 RID/目录名/md5 目录。

        返回值（**新增，向后兼容**：既有调用方忽略返回值即可）：短状态 dict
        `{"key": 资源键, "compile": queued|skipped_done|skipped_no_doc|disabled|
        skipped_no_rid|error, "result": compile_queue 原始返回}` ——
        `_run_pipeline` 的 parse_compile 分支据此生成**如实**的完成文案（不再恒定
        写"已编译 L1"）。
        """
        status: dict = {"key": "", "compile": "", "result": None}
        try:
            import json
            from pathlib import Path

            data = json.loads(Path(doc_json).read_text(encoding="utf-8"))
            meta = data.get("metadata") or {}
            doi = str(meta.get("doi") or "").strip()
            from .kbmeta_service import get_kbmeta

            key = doi
            if not doi:
                # 无 DOI：用内容指纹做身份（同一 PDF 重复解析得到同一 RID）
                pdf_md5 = ""
                if paper_id:
                    pdf_md5 = str((self.store.get_paper(paper_id) or {}).get("pdf_md5") or "")
                rid = get_kbmeta().ensure_paper_registered(
                    doc_json, paper_id=paper_id, pdf_md5=pdf_md5)
                key = rid or ""
                if not key:
                    logger.info("assemble 跳过：无法为无 DOI 文献生成 RID（%s）", doc_json)
                    status["compile"] = "skipped_no_rid"
                    return status
                logger.info("无 DOI 文献按内容指纹登记: rid=%s（%s）", key, doc_json)
            else:
                # 2026-09-12：带 DOI 的 PDF 直导此前**完全不写 papers_meta** ⇒ 价值分算不出来
                # ⇒ 自动升级链对它恒判 L1（见 compile_worker._maybe_upgrade）。
                # 这里按 DOI 从公开源（Crossref/OpenAlex，免费无 Key）临时补全元数据，
                # **只补空字段**：bib 一到即被权威值覆盖；失败不阻塞任务。
                self._ensure_meta_for_doi(doi, paper_id)
            status["key"] = key

            logger.info("解析完成：文献留在 library，等待用户选择编译（不自动纳入 kb）")
            # Q5：自动编译 L1 入队（默认开；done 且产物在 → 幂等跳过；失败不阻塞）
            try:
                from .container import get_settings_service
                if not get_settings_service().get_auto_compile():
                    status["compile"] = "disabled"
                    logger.info("自动编译已关闭：未入队 L1（key=%s）", key)
                else:
                    q = get_kbmeta().compile_queue(key, "L1")
                    status["compile"] = str((q or {}).get("status") or "error")
                    status["result"] = q
                    logger.info("自动编译入队: key=%s queued=%s", key, q)
            except Exception as e2:  # noqa: BLE001
                status["compile"] = "error"
                logger.warning("自动编译入队失败（不影响任务）: %s", e2)
        except Exception as e:  # noqa: BLE001 - assemble 失败不阻塞任务完成
            status["compile"] = status["compile"] or "error"
            logger.warning("知识库登记失败（不影响任务）: %s", e)
        return status

    def _ensure_library_source_pdf(self, doc_json: str, paper_id: int) -> None:
        """幂等补齐 `library/<资源>/source.pdf`（用户反馈 2026-09-12）。

        parse / parse_compile 模式不经过 `engine.export()`（= `_inplace_export`），
        而那是唯一写 library/source.pdf 的地方 → 知识库「原文层四件」永远缺 PDF。
        权威来源 = `papers.pdf_path`（原始上传 PDF，`input/<run_id>/<原名>.pdf`）；
        取不到时由 `engine.ensure_source_pdf` 回退 document.json 的三级兜底搜索。
        失败只告警，不阻塞解析任务。
        """
        try:
            paper = self.store.get_paper(paper_id) or {}
            src = str(paper.get("pdf_path") or "")
            out = self.engine.ensure_source_pdf(doc_json, src or None)
            if out:
                logger.info("已确保 library 源 PDF: %s", out)
            else:
                logger.warning("library 源 PDF 补齐失败（paper=%s）", paper_id)
        except Exception as e:  # noqa: BLE001 - 补齐失败不影响解析结果
            logger.warning("确保 library 源 PDF 异常（不影响任务）: %s", e)

    def _ensure_meta_for_doi(self, doi: str, paper_id: int) -> None:
        """DOI 文献：`papers_meta` 还没有行时，按 DOI 从公开源临时补全元数据。

        为什么需要（用户提问 2026-09-12）：自动编译层级**严格依赖 `papers_meta`**
        （`value_score_for` 无行即 None → 恒判 L1），而 PDF 直导只走 `doi_md5_map`、
        从不登记元数据 ⇒ 这类文献永远拿不到 L2/L3。补全后价值分可算，自动升级链才生效。
        已有行（多为 bib 权威数据）则跳过；失败只告警。
        """
        try:
            from .kbmeta_service import get_kbmeta

            kb = get_kbmeta()
            if kb.get_paper_meta(doi):
                return
            from .doi_meta import enrich_paper_meta

            r = enrich_paper_meta(doi, paper_id=paper_id or None)
            logger.info("DOI 元数据临时补全: doi=%s → ok=%s source=%s filled=%s level=%s",
                        doi, r.get("ok"), r.get("source"), r.get("filled"), r.get("level"))
        except Exception as e:  # noqa: BLE001 - 补全失败不影响解析/编译
            logger.warning("DOI 元数据补全失败（不影响任务）: %s", e)

    def ensure_library_source_pdf_for_key(self, key: str) -> str:
        """按资源键（DOI / RID / 目录名）给 library 资源目录补 `source.pdf`（幂等）。

        用户反馈 2026-09-12：**修复前解析的**文献 library 里根本没有 source.pdf
        （parse/parse_compile 不走 `_inplace_export`）→ 单纯重跑 `source/sync`
        （从 library 复制）永远补不出 PDF 源文件。故「重新同步」前先调这里补齐。
        只补缺、不覆盖；失败返回 ""，不抛。
        """
        try:
            from pathlib import Path

            from paperkb import api as kbapi
            from paperkb.doi import doi_to_dirname

            from .container import get_roots

            lib = Path(get_roots().library_dir)
            names: set[str] = set()
            if key:
                names.add(key)
                names.add(doi_to_dirname(key))
            try:
                row = kbapi.get_doi_md5_map(key) or {}
                if row.get("key"):
                    names.add(str(row["key"]))
            except Exception:  # noqa: BLE001 - 映射表不可用不影响主流程
                pass
            names.discard("")
            # 优先用 papers 表（可拿到权威的 pdf_path）
            for p in self.store.list_papers(limit=1000):
                doc_json = str(p.get("doc_json") or "")
                if not doc_json:
                    continue
                if Path(doc_json).parent.name in names and Path(doc_json).is_file():
                    return self._ensure_library_source_pdf(doc_json, int(p["id"]))
            # 兜底：只用 document.json（pdf_path 缺失时靠 engine 的 basename 三级搜索）
            for name in names:
                doc = lib / name / "document.json"
                if doc.is_file():
                    return self._ensure_library_source_pdf(str(doc), 0)
        except Exception as e:  # noqa: BLE001 - 补齐失败不阻塞同步
            logger.warning("按资源键补 library source.pdf 失败 key=%s: %s", key, e)
        return ""

    def _write_doi_md5_map(self, doc_json: str, paper_id: int) -> None:
        """T6：解析完成后把 (目录名, DOI, pdf_md5) 写入 paperkb.doi_md5_map。

        未来无 DOI 文献以 md5(PDF 全文) 命名目录（T5），kb 索引靠此表把 md5 目录
        反查回 DOI（无则用 md5 作标识）。paper 数据来源 = backend store
        （papers.pdf_md5，G11 权威）+ document.json metadata（doi/source_pdf），
        目录名走 output_dir_name 统一命名规则（与引擎实际落盘目录一致）。
        失败仅告警不阻塞任务。
        """
        try:
            import hashlib
            import json
            from pathlib import Path

            from paperparse.core.document_builder import output_dir_name

            data = json.loads(Path(doc_json).read_text(encoding="utf-8"))
            meta = data.get("metadata") or {}
            doi = str(meta.get("doi") or "").strip()
            src = str(meta.get("source_pdf") or "")
            paper = self.store.get_paper(paper_id)
            pdf_md5 = str((paper or {}).get("pdf_md5") or "")
            if not pdf_md5 and src:
                try:
                    pdf_md5 = hashlib.md5(Path(src).read_bytes()).hexdigest()
                except OSError:
                    pdf_md5 = ""
            dirname = output_dir_name(doi or None, src or None)
            if not dirname:
                return
            from .kbmeta_service import get_kbmeta

            get_kbmeta().set_doi_md5_map(dirname, doi, pdf_md5, paper_id)
        except Exception as e:  # noqa: BLE001 - 映射写失败不阻塞任务
            logger.warning("doi_md5_map 写入失败（不影响任务）: %s", e)

    def _ensure_paper_session(self, paper_id: int) -> None:
        """G10：若该文献还没有 paper 会话则创建一个（懒创建，幂等）。"""
        try:
            sessions = self.store.list_sessions(paper_id) or []
            if any(s.get("kind") == "paper" for s in sessions):
                return
            from .container import get_chat
            chat = get_chat()
            if chat is not None:
                chat.create_session(paper_id, kind="paper")
                logger.info("已为文献 %s 自动创建默认会话", paper_id)
        except Exception as e:  # noqa: BLE001 - 会话创建失败不阻塞
            logger.warning("自动创建文献会话失败 paper=%s: %s", paper_id, e)

    # ---------------------------------------------------------- P12F 复核门控续跑
    def continue_translate(self, paper_id: int, force: bool = False) -> str:
        """复核门控续跑：waiting_review/parsed/failed 文献 → 复核完（pending==0）
        或 force → 翻译+导出。翻译输入=复核后最新 document（不满意可重选后再触发）。
        返回状态消息（ok / 原因），幂等（进行中任务不重复启动）。"""
        paper = self.store.get_paper(paper_id)
        if not paper:
            return f"论文不存在: {paper_id}"
        latest = self.store.latest_task(paper_id, "pipeline")
        if latest and latest["state"] == "running":
            return "该文献任务进行中（请勿重复触发）"
        if not paper.get("doc_json"):
            return "文档未就绪（缺少 document.json），请重新解析"
        from pathlib import Path
        if not Path(str(paper["doc_json"])).exists():
            return "文档文件缺失，请重新解析"
        pending = self._pending_review(paper)
        if pending > 0 and not force:
            return f"仍有 {pending} 个待复核项，请先在复核页处理（或强制翻译）"
        from .container import llm_ready
        if not llm_ready():
            self._fail(paper_id, "未配置 LLM API Key，无法翻译。"
                                "请在设置中心-模型 配置供应商后重试。")
            return "LLM 未配置"
        parse_src = str(paper.get("parse_source") or "dual")
        parse_label = parse_source_label(parse_src)
        self.store.update_paper(paper_id, status="translating", error="")
        msg = "复核完成，开始 AI 翻译+总结" if pending == 0 else "强制翻译（跳过待复核项）"
        self._set_task(paper_id, "running", 40, msg)
        try:
            self._translate_and_export(paper_id, paper, str(paper["doc_json"]),
                                       parse_src, parse_label)
        except Exception as e:  # noqa: BLE001 - 兜底 fail，绝不静默
            logger.exception("续跑翻译异常 paper_id=%s", paper_id)
            self._fail(paper_id, f"内部错误（未预期异常）: {e}")
        return "ok"

    def translate_ready_all(self, force: bool = False) -> dict:
        """批量：扫描 waiting_review 文献 → 复核完（pending==0）或 force 的
        后台线程**串行**翻译（篇间间隔同解析，防 AI 限流）。幂等：进行中跳过。"""
        ready = []
        for p in self.store.list_papers(limit=500):
            if p.get("status") != "waiting_review":
                continue
            pending = self._pending_review(p)
            if force or pending == 0:
                ready.append(p["id"])
        if not ready:
            return {"started": 0, "ready": [], "skipped_pending": []}
        threading.Thread(target=self._translate_batch_worker,
                         args=(ready, force), daemon=True).start()
        return {"started": len(ready), "ready": ready}

    def _translate_batch_worker(self, paper_ids: list[int], force: bool = False) -> None:
        interval = self._parse_interval_sec()
        for i, pid in enumerate(paper_ids):
            try:
                self.continue_translate(pid, force=force)
            except Exception as e:  # noqa: BLE001 - 单篇失败不阻塞批次
                logger.exception("批量翻译单篇异常 paper_id=%s", pid)
            if i < len(paper_ids) - 1:
                time.sleep(interval)

    def review_queue(self) -> dict:
        """待复核队列汇总（任务面板汇总条）：挂起文献 + 待处理/已就绪数。"""
        waiting, ready_count, pending_total = [], 0, 0
        for p in self.store.list_papers(limit=500):
            if p.get("status") != "waiting_review":
                continue
            pending = self._pending_review(p)
            waiting.append({"paper_id": p["id"],
                            "filename": p.get("filename", ""),
                            "title": p.get("title", ""),
                            "pending": pending})
            pending_total += pending
            if pending == 0:
                ready_count += 1
        return {"waiting": waiting, "ready_count": ready_count,
                "pending_total": pending_total}

    # ---------------------------------------------------------- P12F 辅助
    def _pending_review(self, paper: dict) -> int:
        try:
            from .container import get_review
            rv = get_review()
            return rv.pending_review_count(paper) if rv else 0
        except Exception:  # noqa: BLE001 - 门控失败按 0 处理（不阻塞主流程）
            logger.warning("复核待处理计数失败（按 0 处理）", exc_info=True)
            return 0

    def _translate_gate(self) -> str:
        try:
            from .container import get_settings_service
            return str(get_settings_service().get_parse().get("translate_gate", "wait"))
        except Exception:  # noqa: BLE001
            return "wait"

    def _parse_interval_sec(self) -> int:
        try:
            from .container import get_settings_service
            return int(get_settings_service().get_parse().get("parse_interval_sec", 8) or 8)
        except Exception:  # noqa: BLE001
            return 8

    def _fail(self, paper_id: int, error: str) -> None:
        self.store.update_paper(paper_id, status="failed", error=error)
        self._set_task(paper_id, "failed", -1, "失败", error=error)
        if self.event_bus:
            self.event_bus.publish("error", "task", "task_failed",
                                   f"论文[{paper_id}] 任务失败: {error}",
                                   {"paper_id": paper_id})

    # ---------------------------------------------------------- SSE 进度
    def task_stream(self, paper_id: int, interval: float = 1.0,
                    max_seconds: float = 3600) -> str:
        """生成 SSE 事件文本（服务端轮询最新任务状态，直至结束）。"""
        started = time.time()
        last_progress = -1
        while time.time() - started < max_seconds:
            task = self.store.latest_task(paper_id, "pipeline")
            if task is None:
                yield _sse("state", {"state": "pending", "progress": 0, "message": "排队中"})
            else:
                cur = task["progress"]
                if cur != last_progress or task["state"] in ("done", "failed"):
                    yield _sse("state", task)
                    last_progress = cur
                if task["state"] in ("done", "failed"):
                    return
            time.sleep(interval)
        yield _sse("error", {"message": "进度订阅超时"})


def _sse(event: str, data: dict) -> str:
    import json
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
