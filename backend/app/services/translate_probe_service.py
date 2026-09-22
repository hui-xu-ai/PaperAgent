# -*- coding: utf-8 -*-
"""翻译批次「安全上限」探测服务（后台线程 + 进度 + 结果落设置）。

2026-09-22 用户要求："生成一个代码，让用户点击之后，自动测试一下模型的安全能力，然后自动填写"
+ "实际填写值不能按照测试极限来填，按经验设一个安全系数"。

职责边界（算法在 paperkb，接线在这里）：
- **选模型**：优先当前激活的**翻译专用模型**，没有就回落**主模型**（与线上翻译路由同序）；
- **建独立实例**：探测走 `build_ai(...)` 新建一个客户端，**不复用线上翻译实例**——探测要读写
  `last_finish_reason`，共用实例会与正在进行的翻译互相踩状态；
- **后台跑**：最长 5 档真译（可能几分钟），故放线程 + 进度轮询；结果落 settings 供界面回显；
- **只给建议**：本服务**不写** `translate_batch_chars`，是否采纳由用户在界面上点「写入」。

⚠️ 探测会真实消耗 token（最多 5 次翻译调用，输入合计约 0.9-12 万字符），界面必须事先说明。
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

#: 单次探测请求超时：顶档（4.8 万字符源文 → 数万字符译文）生成很慢，90s 不够用。
PROBE_TIMEOUT_SEC = 300
PROBE_MAX_RETRIES = 1


class TranslateProbeService:
    """探测任务的后台执行 + 进度查询（单任务；全局一份状态）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state: dict = {
            "task_id": "", "status": "idle", "current": 0, "total": 0,
            "phase": "尚未探测", "model": "", "via": "", "elapsed": 0,
            "result": None, "error": None,
        }

    # ---------------------------------------------------------- 状态
    def progress(self) -> dict:
        with self._lock:
            return dict(self._state)

    def _set(self, **kw) -> None:
        with self._lock:
            self._state.update(kw)

    # ---------------------------------------------------------- 启动
    def start(self) -> dict:
        """启动探测（已在跑则直接返回当前状态，不重复起线程）。"""
        with self._lock:
            if self._state["status"] == "running":
                return {"ok": True, "already_running": True,
                        "task_id": self._state["task_id"], **dict(self._state)}
            task_id = "probe_%d" % int(time.time() * 1000)
            self._state.update({
                "task_id": task_id, "status": "running", "current": 0, "total": 0,
                "phase": "准备语料…", "model": "", "via": "", "elapsed": 0,
                "result": None, "error": None,
            })
        self._thread = threading.Thread(target=self._run, name="translate-probe",
                                        daemon=True)
        self._thread.start()
        return {"ok": True, "already_running": False, "task_id": task_id}

    # ---------------------------------------------------------- 执行
    def _resolve_provider(self) -> tuple[dict, str]:
        """选被测模型：激活的翻译专用模型 → 主模型（与线上翻译路由同序）。"""
        from . import container
        svc = container.get_settings_service()
        pool = [p for p in svc.get_enabled_translation_providers(masked=False)
                if p.get("api_key")]
        if pool:
            return pool[0], "翻译专用模型"
        main = svc.get_active_provider(masked=False) or {}
        if main.get("api_key") and main.get("base_url") and main.get("model"):
            return main, "主模型"
        raise RuntimeError("没有可用的模型：请先在「模型」页激活一个主供应商，"
                           "或添加并激活一个翻译专用模型")

    def _run(self) -> None:
        t0 = time.time()
        try:
            provider, via = self._resolve_provider()
            from . import container
            from .llm_service import build_ai
            try:
                guard = container.get_guard()      # 计入用量（探测是真实开销）
            except Exception:  # noqa: BLE001 - 无防护（CLI/单测）也能探测
                guard = None
            ai = build_ai(provider, guard, timeout_sec=PROBE_TIMEOUT_SEC,
                          max_retries=PROBE_MAX_RETRIES)
            self._set(via=via, model=ai.model,
                      phase="正在探测（每档一次真实翻译调用）…")
            logger.info("翻译上限探测开始：via=%s model=%s max_tokens=%s",
                        via, ai.model, ai.max_tokens)

            def cb(current: int, total: int, phase: str) -> None:
                self._set(current=current, total=total, phase=phase,
                          elapsed=round(time.time() - t0, 1))

            result = container.get_kbapi().probe_translate_batch(ai, progress_cb=cb)
            result["via"] = via
            result["provider_name"] = provider.get("name") or result.get("provider_name") or ""
            result["elapsed"] = round(time.time() - t0, 1)
            try:
                container.get_settings_service().save_translate_probe_result(result)
            except Exception as e:  # noqa: BLE001 - 存不下不影响本次结果回显
                logger.warning("探测结果落盘失败（不影响本次结果）: %s", e)
            self._set(status="done", result=result, phase="完成",
                      elapsed=round(time.time() - t0, 1))
            logger.info("翻译上限探测完成：模型=%s 最高通过=%s 推荐值=%s 用时=%.1fs",
                        result.get("model"), result.get("highest_pass"),
                        result.get("recommended"), result["elapsed"])
        except Exception as e:  # noqa: BLE001 - 失败要如实回显给用户
            logger.exception("翻译上限探测失败")
            self._set(status="error", error=str(e), phase="失败",
                      elapsed=round(time.time() - t0, 1))


_service: TranslateProbeService | None = None


def get_translate_probe_service() -> TranslateProbeService:
    """模块级单例（container 访问器转调；无需注入依赖）。"""
    global _service
    if _service is None:
        _service = TranslateProbeService()
    return _service
