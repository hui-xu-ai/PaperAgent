# -*- coding: utf-8 -*-
"""编译失败必须**置 failed 并退出队列**（2026-09-23 用户实例回归）。

用户现象：论文 44 翻译完成后自动编译；主模型 Key 失效 → 编译第一次调用 HTTP 401。
此后 worker 每 5 秒重试同一个条目、永不放弃，重试还各自消耗防护计数，刷出 36 条
「engine 第 N 次（上限 12，红线）」。

根因：`Compiler.process_next` 只捕 `CompileError`，LLM 层抛的
`DeepSeekError`/`TokenBudgetExceeded` 会穿透到 worker 主循环 ⇒ 任务一直留在 queued。
本文件钉：非 CompileError 的失败也要落 failed，且**第二次 process_next 拿不到它**
（队列真的空了，不是"永远重试"）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from paperkb import api
from paperkb import llm as kllm
from paperkb.config import Roots
from paperkb.doi import doi_to_dirname
from paperkb.models import PaperMeta

DOI = "10.1002/adma.202407106"


class RaisingLLM:
    """模拟 LLM 层的硬失败（如 401 未授权 / 防护红线）。"""

    def __init__(self, exc: Exception):
        self.exc = exc
        self.calls = 0

    def complete(self, prompt: str, context: str = "compile") -> str:
        self.calls += 1
        raise self.exc


@pytest.fixture()
def env(tmp_path: Path):
    roots = Roots(data_dir=tmp_path / "data", library_dir=tmp_path / "library",
                  kb_dir=tmp_path / "kb").ensure()
    api.init_kb(roots)
    store = api._need_store()          # noqa: SLF001
    store.upsert_meta(PaperMeta(doi=DOI, title="T", journal="Advanced Materials"))
    folder = roots.kb_dir / doi_to_dirname(DOI)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "document.json").write_text("{}", encoding="utf-8")
    (folder / "en.md").write_text("x", encoding="utf-8")
    yield api, roots, store, folder
    kllm.configure_llm(None)
    api._store = None                  # noqa: SLF001
    api._settings = None               # noqa: SLF001
    api._journals = None               # noqa: SLF001
    api._compiler = None               # noqa: SLF001


def test_llm_error_marks_job_failed_and_leaves_queue(env):
    """LLM 抛非 CompileError（401 一类）→ 置 failed，且不再被反复取出。"""
    _api, _roots, store, _folder = env
    kllm.configure_llm(RaisingLLM(RuntimeError("DeepSeek 调用失败（HTTP 401）")))
    api.compile_queue(DOI, "L1")

    out = api.compile_process(1)
    assert len(out) == 1 and "error" in out[0], out
    assert "401" in out[0]["error"]

    job = store.get_job(DOI, "L1")
    assert job["status"] == "failed", job
    assert "401" in (job.get("error") or "")

    # 关键：队列里没有它了 ⇒ 第二次不会再取到（旧行为是每 5s 无限重试）
    assert api.compile_process(1) == []
    assert [r for r in api.compile_status("queued")] == []


def test_failed_job_keeps_value_score(env):
    """置 failed 不能把入队时的价值分冲成 0（REPLACE 语义，历史坑）。"""
    _api, _roots, store, _folder = env
    kllm.configure_llm(RaisingLLM(RuntimeError("boom")))
    store.upsert_job(DOI, "L1", status="queued", value_score=3.5)
    api.compile_process(1)
    job = store.get_job(DOI, "L1")
    assert job["status"] == "failed", job
    assert job["value_score"] == pytest.approx(3.5)
