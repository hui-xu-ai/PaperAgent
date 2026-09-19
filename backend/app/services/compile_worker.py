# -*- coding: utf-8 -*-
"""后台编译 worker（A2）：L1 全自动 + L2/L3 按价值分自动升级。

- 单 daemon 线程轮询 compile_jobs 队列：有 queued → 执行器处理 1 项；
  处理成功后按价值分自动升级（L1→L2→L3 价值链）
- 队列空 → 轮询等待；LLM 未配置（无 API key）→ 空转等待，不崩溃、
  不把任务误置 failed（paperkb 未注入 LLM 时 get_llm 会抛，故先闸门）
- 执行器/查询全部构造注入（callable，可单测，不直接 import container）
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)


def _kbmeta():
    from .kbmeta_service import get_kbmeta

    return get_kbmeta()


def _default_executor():
    return _kbmeta().compile_process(1)


def _default_status(status):
    return _kbmeta().compile_status(status)


def _default_value(doi):
    return _kbmeta().value_score(doi)


def _default_queue(doi, level):
    return _kbmeta().compile_queue(doi, level)


def _default_llm_ready() -> bool:
    """paperkb LLM 已注入？（compile 实际可跑的信号；未配置 → False 空转）。"""
    try:
        from paperkb.llm import llm_ready as paperkb_llm_ready

        return bool(paperkb_llm_ready())
    except Exception:  # noqa: BLE001 - 探测失败按不可用处理
        return False


class CompileWorker:
    """后台编译 worker：单 daemon 线程串行处理编译队列。

    依赖注入（均为 callable，便于测试 fakes）：
    - executor(): 处理 1 个队列项，返回 compile_process(1) 结果列表
      （每项 {"doi","level","result"|"error"}）
    - status_fn(status): compile_status(status)，worker 用 "queued" 取队列
    - value_fn(doi): value_score_for(doi) 价值分 dict（含 level）
    - queue_fn(doi, level): compile_queue 入队（自动升级用）
    - llm_ready(): LLM 可用性；False → 空转等待
    - poll_interval: 队列空时轮询间隔（秒，默认 5）
    """

    def __init__(self, executor=None, status_fn=None, value_fn=None,
                 queue_fn=None, llm_ready=None, poll_interval: float = 5.0,
                 notify=None):
        self._executor = executor or _default_executor
        self._status_fn = status_fn or _default_status
        self._value_fn = value_fn or _default_value
        self._queue_fn = queue_fn or _default_queue
        self._llm_ready = llm_ready or _default_llm_ready
        # notify(item)：编译项收尾通知（container 接事件总线 → 前端刷新「编译结果标签页」）。
        # 2026-09-12 修复：此前**编译完成不发任何事件**，前端只能等 15s 轮询，
        # 阅读器标签页因此长期看不到刚编译出的 `_note.md`（用户实测报障）。
        self._notify = notify or (lambda item: None)
        self.poll_interval = max(0.5, float(poll_interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------- 生命周期
    def start(self) -> None:
        """启动 daemon 后台线程（幂等：已运行则忽略）。"""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="compile-worker",
                                        daemon=True)
        self._thread.start()
        logger.info("CompileWorker 已启动（poll=%ss）", self.poll_interval)

    def stop(self, timeout: float = 2.0) -> None:
        """请求停止：置停止标志；daemon 线程在下次循环检查点退出。

        timeout 内 join（若正卡在 LLM 调用则最多等 timeout，daemon 兜底退出）。
        """
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ---------------------------------------------------------- 核心
    def process_one(self) -> dict | None:
        """处理队列最高优先级 1 项（手动触发/测试/worker 循环共用）。

        返回：
        - None：LLM 不可用或队列空（空转，无副作用）
        - dict：executor 返回的该项结果（{"doi","level","result"|"error"}）
        """
        if not self._llm_ready():
            logger.debug("编译 worker 空转：LLM 未配置")
            return None
        queued = self._status_fn("queued") or []
        if not queued:
            return None
        results = self._executor() or []
        if not results:
            # 执行器无返回（队列被并发处理/单测缺省）——不算空转也不升级
            logger.warning("编译执行器无返回（队列可能已被处理）")
            return None
        first = results[0]
        self._maybe_upgrade(first)
        try:
            self._notify(first)          # 编译项收尾通知（container 接事件总线 → 前端刷新标签页）
        except Exception:  # noqa: BLE001 - 通知失败不影响 worker 主循环
            logger.warning("编译项通知失败: %s", first, exc_info=True)
        return first

    def _maybe_upgrade(self, r: dict) -> None:
        """自动升级链：L1 完成且价值分 ≥L2 → 入队 L2；L2 完成且价值分 L3 → 入队 L3。

        2026-09-19 修复：L1+L2 合并编译时 `l2_written=True`，跳过 L2 入队避免冗余。
        失败项（含 "error"）不升级——paperkb 已置 failed，此处只记日志，
        不无限重试；升级失败（如 value 查不到）只告警不阻塞。
        """
        if not isinstance(r, dict) or "error" in r:
            logger.warning("编译项失败（paperkb 已置 failed）: %s", r)
            return
        doi = r.get("doi")
        level = r.get("level")
        if not doi or not level:
            return
        try:
            value = self._value_fn(doi) or {}
            vlevel = str(value.get("level") or "L1")
            if level == "L1" and vlevel in ("L2", "L3"):
                if r.get("l2_written"):
                    logger.info("编译自动升级: %s L1 已含 L2（合并编译），跳过 L2 入队", doi)
                else:
                    self._queue_fn(doi, "L2")
                    logger.info("编译自动升级: %s L1→L2（价值分=%s）", doi, vlevel)
            elif level == "L2" and vlevel == "L3":
                self._queue_fn(doi, "L3")
                logger.info("编译自动升级: %s L2→L3（价值分=%s）", doi, vlevel)
        except Exception as e:  # noqa: BLE001 - 升级失败不阻塞 worker
            logger.warning("编译自动升级失败 doi=%s: %s", doi, e)

    # ---------------------------------------------------------- 循环
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.process_one() is not None:
                    continue  # 刚处理完一项 → 立即检查队列（自动升级项也在此被处理）
            except Exception as e:  # noqa: BLE001 - 单轮异常不杀线程
                logger.exception("编译 worker 循环异常（继续运行）: %s", e)
            self._stop.wait(self.poll_interval)
