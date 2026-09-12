#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: tests/conftest.py
功能: pytest 全局配置：包路径注入 + 样本大小守卫（D9 纪律）+ 沙箱兼容临时目录 tmp_work
对外接口: fixtures: samples_dir / sample_head / tmp_work
版本: v1.0.1 (2025-xx-xx)
版本历史:
  v1.0.2 tmp_work 改为"用完即清"（删除失败容忍）——旧版只创建不删除，跑一次全量测试长 7.5GB
  v1.0.1 新增 tmp_work（沙箱拒绝 pytest tmp_path 的 rmtree 清理，改为只创建不删除）
  v1.0.0 初始版本
"""
import sys
import uuid
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "paperparse"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# tests 作为包（skill/ 入 path），支持 test_*.py 间跨模块导入（如 from tests.test_layout import ...）
TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))

SAMPLES_DIR = Path(__file__).resolve().parent / "samples"
MAX_SAMPLE_READ_BYTES = 4096
MAX_SAMPLE_TOTAL_BYTES = 2 * 1024 * 1024
TMP_WORK_ROOT = Path(__file__).resolve().parents[1] / "work" / "pytest-tmp2"


@pytest.fixture
def tmp_work():
    """[全局] 每测试临时目录（work/pytest-tmp2/<uuid>）；**用完即清**。

    历史：v1.0.1 起"只创建不删除"——因为当时沙箱拒绝 pytest 自带 tmp_path 的
    rmtree。副作用是跑一次全量测试就长 7.5GB（2026-09-11 清理实测）。
    现在改为：测试结束尝试删除；**删除失败也绝不 fail 测试**（沙箱/占用都容忍），
    最坏退化为旧行为，不会污染测试结果。
    """
    import shutil

    d = TMP_WORK_ROOT / uuid.uuid4().hex[:12]
    d.mkdir(parents=True, exist_ok=True)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def read_sample_head(name: str, max_bytes: int = MAX_SAMPLE_READ_BYTES) -> str:
    """[全局] 抽样读取样本头部（禁止全文读入测试上下文）

    参数:
        name: 样本文件名（tests/samples/ 下）
        max_bytes: 单次最多读取字节数（默认 4KB）
    返回:
        样本头部文本
    报错:
        AssertionError: 样本超过大小守卫上限
    """
    p = SAMPLES_DIR / name
    if not p.exists():
        pytest.skip(f"样本缺失: {name}")
    size = p.stat().st_size
    assert size <= MAX_SAMPLE_TOTAL_BYTES, (
        f"样本过大（{size} bytes），禁止整读（D9 纪律）；只允许 read_sample_head 抽样"
    )
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        return f.read(max_bytes)


@pytest.fixture
def samples_dir() -> Path:
    """[全局] 样本目录路径"""
    return SAMPLES_DIR


@pytest.fixture
def sample_head():
    """[全局] 抽样读取函数（见 read_sample_head）"""
    return read_sample_head
