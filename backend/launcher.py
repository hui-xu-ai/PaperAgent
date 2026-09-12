# -*- coding: utf-8 -*-
"""PyInstaller 入口（绝对导入，避免顶层脚本相对导入报错）。

开发模式仍用 `python -m app.main`；打包产物走本文件。

打包版纪律（2026-09-12 实测加固）：`console=False` 时**任何导入期异常都是"双击没反应"**，
所以 import 也必须放进 try 里，崩了要写 `logs/crash.log` + 弹窗（实测：漏打包 paperkb
时进程静默消失，用户无从排障）。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # backend/ 入 path

# PyInstaller 以 --noconsole/--windowed 打包时，sys.stdout/sys.stderr 为 None，
# uvicorn 的 DefaultFormatter 初始化会调用 sys.stdout.isatty() 而崩溃。
# 必须在 import app.main / run() 之前先兜底。
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")


def _crash_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _report_crash() -> str:
    """写崩溃日志 + 弹窗；返回日志路径（供测试断言）。"""
    import traceback

    text = traceback.format_exc()
    base = _crash_dir()
    log_path = base / "logs" / "crash.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(text, encoding="utf-8")
    except OSError:
        pass
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            None,
            f"PaperAgent 启动失败：\n\n{text[-1200:]}\n\n日志：{log_path}",
            "PaperAgent 启动失败", 0x10)
    except Exception:  # noqa: BLE001 - 弹窗失败也不能再崩
        pass
    return str(log_path)


def main() -> None:
    try:
        from app.main import run          # noqa: PLC0415 - 必须包在 try 内（启动期崩溃要可见）

        run()
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001 - 兜底：任何启动期异常都要可见
        _report_crash()
        raise


if __name__ == "__main__":
    main()
