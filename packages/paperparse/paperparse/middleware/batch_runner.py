#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: skill/src/paperparse/middleware/batch_runner.py
功能: T18/M4 批量执行器：任务队列（JSONL）→ 每任务**独立子进程**
      （python -m paperparse run --json，日志/审计独立）→ 主进程只汇总
      JobSummary（紧凑摘要，绝不含文献全文——上下文隔离）；失败隔离；
      支持 --dry-run 预算预估（页数 × 每页 token 估算）。
对外接口: run_batch / estimate_budget / load_jobs
版本: v1.0.0 (2026-08-18)
版本历史:
  v1.0.0 初始版本（T18/M4）
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from paperparse.middleware.schema import JobSummary

__all__ = ["run_batch", "estimate_budget", "load_jobs"]

TOKENS_PER_PAGE = 2000      # token 预算估算：每页约 2000 token（含公式/图注开销）
MAX_WORKERS = 4             # 默认并发上限
SRC_DIR = str(Path(__file__).resolve().parents[2])   # skill/src（包未安装时注入 PYTHONPATH）


def load_jobs(path: str | Path) -> list[dict]:
    """[全局] 读取任务队列（JSONL：每行一个任务 dict）

    参数:
        path: JSONL 文件路径
    返回:
        任务列表 [{"job_id","pdf","parser"?,"calibration_md"?,"template"?,"dpi"?}]
    报错:
        FileNotFoundError: 文件不存在
    """
    p = Path(path)
    jobs: list[dict] = []
    with open(p, "r", encoding="utf-8-sig") as f:   # 兼容 PowerShell 写出的 BOM
        for line in f:
            line = line.strip()
            if not line:
                continue
            job = json.loads(line)
            job.setdefault("job_id", "job%03d" % (len(jobs) + 1))
            jobs.append(job)
    return jobs


def _job_env() -> dict:
    """[局部] 子进程环境：注入 skill/src 到 PYTHONPATH（包未装入 site-packages 时）"""
    env = dict(os.environ)
    prev = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = SRC_DIR + (os.pathsep + prev if prev else "")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run_one(job: dict, out_dir: Path, job_out: Path) -> JobSummary:
    """[局部] 单任务：独立子进程执行，返回紧凑摘要（失败隔离）"""
    pdf = str(job["pdf"])
    cmd = [sys.executable, "-m", "paperparse", "run", pdf, "--json",
           "--out-dir", str(job_out)]
    if job.get("parser"):
        cmd += ["--parser", str(job["parser"])]
    if job.get("calibration_md"):
        cmd += ["--calibration-md", str(job["calibration_md"])]
    if job.get("template"):
        cmd += ["--template", str(job["template"])]
    log = job_out / "job.log"
    try:
        with open(log, "w", encoding="utf-8") as lf:
            proc = subprocess.run(cmd, env=_job_env(), stdout=lf, stderr=subprocess.STDOUT,
                                  timeout=3600)
        data = {}
        if log.exists():
            text = log.read_text(encoding="utf-8", errors="replace")
            s, e = text.find("{"), text.rfind("}")
            if s != -1 and e > s:
                try:
                    data = json.loads(text[s: e + 1])
                except Exception:
                    data = {}
        status = data.get("status") or ("failed" if proc.returncode != 0 else "success")
        if status == "success" and data.get("error"):
            status = "failed"
        md_path = (data.get("outputs") or {}).get("md") or ""
        return JobSummary(
            job_id=job.get("job_id", ""),
            doi=(data.get("doi") or ""),
            status=status if status in ("success", "partial", "failed") else "failed",
            warnings_count=len(data.get("warnings") or []),
            tokens=int((data.get("stats") or {}).get("tokens") or 0),
            md_path=md_path,
            error_code=(data.get("error") or {}).get("code"),
        )
    except Exception as exc:                     # 子进程崩溃/超时 → 失败隔离
        return JobSummary(job_id=job.get("job_id", ""), status="failed",
                          error_code="PAPER-0601",
                          md_path=str(log))


def estimate_budget(jobs: list[dict]) -> dict:
    """[全局] --dry-run 预算预估：页数 × 每页 token 估算（不实际转换）

    参数:
        jobs: 任务列表
    返回:
        {"jobs": n, "pages": total, "tokens": total_tokens, "per_job": [...]}
    """
    import pymupdf
    per_job: list[dict] = []
    total_pages = 0
    for job in jobs:
        pages = 0
        try:
            with pymupdf.open(str(job["pdf"])) as doc:
                pages = doc.page_count
        except Exception:
            pages = 0
        total_pages += pages
        per_job.append({"job_id": job.get("job_id", ""), "pdf": str(job["pdf"]),
                        "pages": pages, "tokens": pages * TOKENS_PER_PAGE})
    return {"jobs": len(jobs), "pages": total_pages,
            "tokens": total_pages * TOKENS_PER_PAGE, "per_job": per_job}


def run_batch(jobs: list[dict], out_dir: str | Path,
              max_workers: int = MAX_WORKERS,
              dry_run: bool = False) -> list[JobSummary]:
    """[全局] 批量执行：任务队列 → 独立子进程转换 → JobSummary 汇总

    参数:
        jobs: 任务列表（见 load_jobs）
        out_dir: 输出根目录（每任务独立子目录 <out_dir>/<job_id>/）
        max_workers: 并发上限（默认 4；1=串行）
        dry_run: True=仅预算预估（不执行）
    返回:
        JobSummary 列表（与 jobs 同序；失败任务 status=failed + error_code）
    """
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    if dry_run:
        return []

    results: list[JobSummary] = [None] * len(jobs)   # type: ignore[list-item]

    def _go(idx: int, job: dict) -> None:
        job_out = root / (job.get("job_id") or "job%03d" % (idx + 1))
        job_out.mkdir(parents=True, exist_ok=True)
        results[idx] = _run_one(job, root, job_out)

    if max_workers <= 1:
        for idx, job in enumerate(jobs):
            _go(idx, job)
    else:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(jobs) or 1)) as pool:
            for idx, job in enumerate(jobs):
                pool.submit(_go, idx, job)
    return [r for r in results if r is not None]
