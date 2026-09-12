#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: tests/test_batch_runner.py
功能: T18/M4 批量执行器测试：
      - 任务队列 JSONL 加载
      - dry-run 预算预估
      - 真实批量执行（独立子进程）+ JobSummary 汇总
      - 失败隔离（坏任务不影响好任务）
对外接口: 无（测试）
版本: v1.0.0 (2026-08-18)
版本历史:
  v1.0.0 初始版本
"""
import json
from pathlib import Path

import pytest

from paperparse.middleware.batch_runner import estimate_budget, load_jobs, run_batch

PDF = Path(__file__).resolve().parent / "samples" / "10.1002_adma.202407106.pdf"


def test_load_jobs(tmp_work):
    """JSONL 任务队列加载（缺省 job_id 自动编号）"""
    f = tmp_work / "jobs.jsonl"
    f.write_text(
        '{"pdf": "a.pdf", "job_id": "j1"}\n{"pdf": "b.pdf"}\n', encoding="utf-8")
    jobs = load_jobs(f)
    assert len(jobs) == 2
    assert jobs[0]["job_id"] == "j1"
    assert jobs[1]["job_id"] == "job002"


def test_estimate_budget():
    """dry-run 预算预估：页数 × 每页 token"""
    if not PDF.exists():
        return
    budget = estimate_budget([{"job_id": "j1", "pdf": str(PDF)}])
    assert budget["jobs"] == 1
    assert budget["pages"] == 15
    assert budget["tokens"] == 15 * 2000
    assert budget["per_job"][0]["pages"] == 15


@pytest.mark.skipif(not PDF.exists(), reason="样本缺失")
def test_run_batch_real_and_isolation(tmp_work):
    """真实批量：好任务成功、坏任务失败，互不影响（失败隔离）"""
    jobs = [
        {"job_id": "good", "pdf": str(PDF)},
        {"job_id": "bad", "pdf": str(tmp_work / "no_such.pdf")},
    ]
    results = run_batch(jobs, out_dir=str(tmp_work / "out"), max_workers=2)
    by_id = {r.job_id: r for r in results}
    assert by_id["good"].status == "success"
    assert by_id["good"].md_path
    assert by_id["bad"].status == "failed"
    assert by_id["bad"].error_code
    # 每任务独立子目录 + 日志
    assert (tmp_work / "out" / "good" / "job.log").exists()
