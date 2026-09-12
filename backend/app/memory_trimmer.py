# memory_trimmer.py

"""
内存自清理模块（含定时清理线程）
提供：
  - trim_memory()            释放当前进程物理内存工作集
  - delayed_trim()           在 delay 秒后于后台线程中执行一次 trim_memory()，避免阻塞主线程，消除对启动速度的影响。
  - start_periodic_trim()   启动后台线程，每 interval 秒自动清理一次
"""
import ctypes
import gc
import threading
import time
from ctypes import wintypes

def trim_memory() -> None:
    """释放当前进程可回收的物理内存（工作集），效果等同安全软件的内存清理"""
    try:
        h_process = ctypes.windll.kernel32.GetCurrentProcess()
        ctypes.windll.kernel32.SetPriorityClass(h_process, 0x00004000)  # BELOW_NORMAL_PRIORITY_CLASS
        time.sleep(0.5)
        gc.collect()
        time.sleep(0.3)

        psapi = ctypes.windll.psapi
        psapi.EmptyWorkingSet.argtypes = [wintypes.HANDLE]
        psapi.EmptyWorkingSet.restype = wintypes.BOOL
        if psapi.EmptyWorkingSet(h_process):
            return
        SIZE_T = ctypes.c_size_t
        SIZE_MAX = ctypes.c_size_t(2 ** (ctypes.sizeof(SIZE_T) * 8) - 1)
        ctypes.windll.kernel32.SetProcessWorkingSetSize(h_process, SIZE_MAX, SIZE_MAX)
    except Exception:
        pass

def delayed_trim(delay: float = 3.0) -> None:
    """
    在 delay 秒后于后台线程中执行一次 trim_memory()。
    避免阻塞主线程，消除对启动速度的影响。
    """
    def _delayed():
        time.sleep(delay)
        trim_memory()
    threading.Thread(target=_delayed, daemon=True).start()

def start_periodic_trim(interval: int = 600) -> None:
    """
    启动一个后台守护线程，每隔 interval 秒自动调用 trim_memory()
    通常只需在程序入口调用一次。
    """
    def _loop():
        while True:
            time.sleep(interval)
            trim_memory()
    t = threading.Thread(target=_loop, daemon=True)
    t.start()
