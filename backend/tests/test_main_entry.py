# -*- coding: utf-8 -*-
"""main 入口完整性测试（V13：弥补 pytest 不覆盖 __main__ 路径的盲区）。

曾漏检：main.py 引用 logger 未定义、绝对导入 memory_trimmer 找不到模块、
trim 调用放在阻塞的 uvicorn.run 之后永不执行。
"""
from __future__ import annotations


def test_main_module_importable():
    """main 模块可导入（无语法/导入错误），且 logger 已定义。"""
    import app.main as m
    assert hasattr(m, "logger"), "main 未定义 logger（曾为 NameError 隐患）"
    assert hasattr(m, "start_periodic_trim") and hasattr(m, "delayed_trim")
    assert hasattr(m, "app")


def test_memory_trimmer_module():
    """memory_trimmer 提供 trim/delayed/periodic 三个入口。"""
    from app.memory_trimmer import trim_memory, delayed_trim, start_periodic_trim
    assert callable(trim_memory) and callable(delayed_trim) and callable(start_periodic_trim)


def test_guard_reset_context():
    """TokenGuard.reset_context 按任务重置计数（跨论文累计修复）。"""
    from app.services.llm_service import TokenGuard

    g = TokenGuard()
    g.begin_call("engine", 100)
    g.begin_call("engine", 100)
    assert g._calls["engine"]["count"] == 2
    g.reset_context("engine")
    assert "engine" not in g._calls
    g.begin_call("engine", 100)  # 重置后可继续
    assert g._calls["engine"]["count"] == 1
