# -*- coding: utf-8 -*-
"""桌面外壳（Windows）：原生窗口 + 系统托盘 + 单实例/退出语义。

用户模型（2026-09-12 发布轮拍板）：
- 启动即弹出**完全最大化**的原生窗口（WebView2 承载前端，无地址栏/标签栏/浏览器痕迹）；
- 关窗（X）= **隐藏到托盘**，后端与在跑任务继续；
- 只有托盘「退出」/ 命令行 `--quit` 才**完全注销端口与后台进程**（优雅关停 + 宽限窗兜底）。

为什么不是「浏览器 --app 窗口」：Chromium `--app` 仍是浏览器进程（受浏览器策略/扩展/主题影响），
且 `--start-maximized` 常被忽略。pywebview+WebView2 是真正的原生窗口，且运行时本机已具备。

本模块**不 import app.main**（避免循环导入）；关停动作由调用方以回调传入。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

WINDOW_TITLE = "PaperAgent · 文献 AI 阅读"
HEALTH_TIMEOUT = 45.0                 # 秒：启动时等后端健康的最长时间
GRACE_WAIT = float(os.environ.get("PAPERAGENT_GRACE_SECONDS", "30"))


# ------------------------------------------------------------------ 模式判定

def is_desktop_requested(argv: list[str] | None = None) -> bool:
    """是否启用桌面壳：打包版默认启用；开发态需 `--desktop` 或 `PAPERAGENT_DESKTOP=1`。"""
    args = list(sys.argv[1:] if argv is None else argv)
    if "--no-desktop" in args:
        return False
    if "--desktop" in args:
        return True
    if os.environ.get("PAPERAGENT_DESKTOP", "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    return bool(getattr(sys, "frozen", False))


def webview2_available() -> bool:
    """WebView2 运行时是否可用（注册表 + 目录双通道）。缺失则退回浏览器窗口。"""
    try:
        import winreg
        key = (r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"
               r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}")
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(root, key) as k:
                    if winreg.QueryValueEx(k, "pv")[0]:
                        return True
            except OSError:
                continue
    except Exception:  # noqa: BLE001 - 非 Windows / 无权限
        pass
    for p in (r"C:\Program Files (x86)\Microsoft\EdgeWebView\Application",
              r"C:\Program Files\Microsoft\EdgeWebView\Application"):
        try:
            if any(Path(p).glob("*/msedgewebview2.exe")):
                return True
        except OSError:
            continue
    return False


# ------------------------------------------------------------------ 图标（单一来源）

def icon_candidates() -> list[Path]:
    """托盘/exe 图标候选路径（**同一 icon.ico**，用户可直接替换）。

    顺序：打包资源（_MEIPASS）→ exe 同目录 → 开发态 assets/。
    注意 PyInstaller 6 onedir 的 datas 落在 `_internal/`，所以"exe 同目录"只是用户替换用的兜底。
    """
    out: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        out.append(Path(meipass) / "icon.ico")
    if getattr(sys, "frozen", False):
        out.append(Path(sys.executable).resolve().parent / "icon.ico")
    out.append(Path(__file__).resolve().parents[2] / "assets" / "icon.ico")
    return out


def load_icon_image(size: int = 64):
    """加载托盘图标（PIL Image）。全部候选失败 → 手绘兜底（不让托盘消失）。"""
    from PIL import Image, ImageDraw

    for p in icon_candidates():
        try:
            if p.exists():
                with Image.open(p) as im:
                    logger.info("托盘图标来源: %s", p)
                    return im.convert("RGBA").resize((size, size), Image.LANCZOS)
        except Exception as e:  # noqa: BLE001 - 损坏则试下一个
            logger.warning("图标不可用 %s: %s", p, e)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, size - 3, size - 3], radius=12, fill=(37, 99, 235, 255))
    d.text((size // 3, size // 5), "P", fill="white", font=None)
    d.rectangle([size // 3, size // 2, size * 2 // 3, size * 2 // 3], fill="white")
    logger.warning("托盘图标回退手绘（icon.ico 未找到）")
    return img


# ------------------------------------------------------------------ HTTP 小工具

def http_json(url: str, method: str = "GET", timeout: float = 2.0) -> dict | None:
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - 固定本机地址
            return json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def wait_backend_ready(base_url: str, timeout: float = HEALTH_TIMEOUT) -> bool:
    """轮询 /api/health 直到后端就绪（窗口要在后端活着时再开）。"""
    deadline = time.time() + timeout
    url = base_url.rstrip("/") + "/api/health"
    while time.time() < deadline:
        if (http_json(url, timeout=1.5) or {}).get("status") == "ok":
            return True
        time.sleep(0.4)
    return False


def wait_backend_or_dead(base_url: str, thread, timeout: float = HEALTH_TIMEOUT) -> tuple[bool, bool]:
    """等后端就绪；同时盯着服务线程——**线程已死**说明启动失败（如端口被占）。

    返回 `(就绪?, 线程已死?)`。桌面壳里后端跑在后台线程，`sys.exit()` 不会传到主线程，
    不盯线程就会"窗口开了、后端其实已经死了"（实测风险点）。
    """
    deadline = time.time() + timeout
    url = base_url.rstrip("/") + "/api/health"
    while time.time() < deadline:
        if (http_json(url, timeout=1.5) or {}).get("status") == "ok":
            return True, False
        if thread is not None and not thread.is_alive():
            return False, True
        time.sleep(0.4)
    return False, bool(thread is not None and not thread.is_alive())


def running_instance(base_url: str) -> dict | None:
    """若已有本机实例在跑，返回其 /api/health 内容，否则 None。"""
    h = http_json(base_url.rstrip("/") + "/api/health", timeout=1.5)
    return h if h and h.get("status") == "ok" else None


def signal_instance(base_url: str, action: str) -> bool:
    """让已在跑的实例做动作：action ∈ {show, quit}（仅本机，走 /api/desktop/*）。"""
    r = signal_instance_ex(base_url, action)
    return bool(r and r.get("status") == "ok")


def signal_instance_ex(base_url: str, action: str) -> dict | None:
    """同 `signal_instance`，但返回完整应答（供单实例闸门判断"到底能不能唤醒窗口"）。

    实测（2026-09-12）：占端口的可能是**开发/后台模式**实例（无窗口、无本端点 ⇒ 404），
    此时必须让用户**看得见**，不能静默退出。
    """
    return http_json(f"{base_url.rstrip('/')}/api/desktop/{action}", method="POST", timeout=3.0)


# ------------------------------------------------------------------ 浏览器回退（旧路径）

def open_app_window(url: str) -> None:
    """回退路径：Edge/Chrome `--app=` 干净窗口（无地址栏/标签栏）+ 强制最大化。

    仅在 WebView2 不可用/原生窗口起不来时使用。找不到 Chromium 则退回默认浏览器。
    """
    import shutil

    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    exe = (shutil.which("msedge.exe") or shutil.which("chrome.exe")
           or shutil.which("edge.exe"))
    if not exe:
        exe = next((p for p in candidates if Path(p).exists()), None)
    if exe:
        try:
            subprocess.Popen([exe, "--app=" + url, "--start-maximized"], close_fds=True)  # noqa: S603
            logger.info("回退：以浏览器 app 窗口模式打开: %s", exe)
            threading.Thread(target=_maximize_browser_window,
                             args=("PaperAgent", 6.0), daemon=True).start()
            return
        except Exception as e:  # noqa: BLE001
            logger.warning("app 窗口打开失败，回退默认浏览器: %s", e)
    import webbrowser
    webbrowser.open(url)


def _maximize_browser_window(title_substr: str, timeout: float = 6.0) -> None:
    """按窗口标题子串找顶层窗口并最大化（Chromium 常忽略 --start-maximized）。"""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    hit: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        if hit:
            return False
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if title_substr in buf.value:
            user32.ShowWindow(hwnd, 3)          # SW_MAXIMIZE
            user32.SetForegroundWindow(hwnd)
            hit.append(hwnd)
            return False
        return True

    deadline = time.time() + timeout
    while time.time() < deadline:
        user32.EnumWindows(_cb, 0)
        if hit:
            return
        time.sleep(0.4)


def fatal_dialog(message: str) -> None:
    """打包版无控制台：致命错误必须弹窗（否则"双击没反应"无从排障）。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "PaperAgent 启动失败", 0x10)
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ 托盘

def build_tray(*, on_show, on_quit, on_open_browser, tooltip: str = WINDOW_TITLE):
    """建系统托盘（pystray，run_detached）。失败返回 None（不阻塞服务）。"""
    try:
        import pystray

        menu = pystray.Menu(
            pystray.MenuItem("🖥 显示主窗口", lambda *_: on_show(), default=True),
            pystray.MenuItem("🌐 在浏览器中打开", lambda *_: on_open_browser()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("⏻ 退出 PaperAgent", lambda *_: on_quit()),
        )
        icon = pystray.Icon("paperagent", load_icon_image(), tooltip, menu)
        icon.run_detached()
        logger.info("系统托盘已启动（图标来源见上一条日志）")
        return icon
    except Exception as e:  # noqa: BLE001 - 托盘失败不阻塞服务
        logger.warning("系统托盘启动失败（忽略）: %s", e)
        return None


def _balloon(icon, title: str, text: str) -> None:
    try:
        if icon is not None:
            icon.notify(text, title)
    except Exception:  # noqa: BLE001 - 通知失败无关紧要
        pass


# ------------------------------------------------------------------ 桌面壳主流程

_active_shell: "Shell | None" = None


def active_shell() -> "Shell | None":
    """当前桌面壳（无壳 → None；`/api/health` 与单实例闸门据此判断"有没有窗口可唤起"）。"""
    return _active_shell


def show_window() -> bool:
    """唤起主窗口（供 /api/desktop/show 与第二个实例调用）；无桌面壳返回 False。"""
    s = _active_shell
    if s is None:
        return False
    s.show()
    return True


class Shell:
    """窗口 + 托盘的协调者（关窗=隐藏；退出=唯一真正的结束路径）。"""

    def __init__(self, url: str, request_quit, on_teardown_done=None) -> None:
        self.url = url
        self.request_quit = request_quit
        self.on_teardown_done = on_teardown_done
        self.win = None
        self.icon = None
        self.quitting = False

    # -- 动作
    def show(self, *_a) -> None:
        w = self.win
        if w is None:
            return
        try:
            w.show()
            w.restore()
            w.maximize()          # 保证恢复时仍是最大化
        except Exception as e:  # noqa: BLE001
            logger.warning("显示窗口失败: %s", e)

    def open_browser(self, *_a) -> None:
        open_app_window(self.url)

    def quit(self, *_a) -> None:
        """唯一真正退出：优雅关停后端 → 停托盘 → 销毁窗口 → **报告拆卸完成** → 主线程返回。

        2026-09-12 实测（打包版）：进程退出（watchdog 的 `os._exit`）可能发生在
        `self.win.destroy()` 的 **WebView2/COM 拆卸途中** ⇒ 终止卡在内核态，进程停在
        "半死"态（占住端口、`taskkill` 都杀不掉）。所以这里在窗口销毁**之后**显式回报，
        让 watchdog 等拆卸完成再退（另见 `main._release_listen_sockets`：端口会先被释放）。
        """
        if self.quitting:
            return
        self.quitting = True
        logger.info("收到退出动作：优雅关停 PaperAgent（端口将由进程退出释放）…")
        try:
            self.request_quit("desktop quit")
        except Exception:  # noqa: BLE001
            logger.exception("请求优雅关停失败")
        try:
            if self.icon is not None:
                self.icon.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.win is not None:
                self.win.destroy()
        except Exception:  # noqa: BLE001
            logger.exception("销毁窗口失败")
        finally:
            try:
                if self.on_teardown_done is not None:
                    self.on_teardown_done()
            except Exception:  # noqa: BLE001
                logger.debug("拆卸完成回报失败（不影响退出）", exc_info=True)

    # -- 事件
    def on_closing(self) -> bool:
        """关窗（X）= 隐藏到托盘（后端与任务继续）。返回 False 取消关闭。"""
        if self.quitting:
            return True
        try:
            if self.win is not None:
                self.win.hide()
        except Exception as e:  # noqa: BLE001
            logger.warning("隐藏窗口失败: %s", e)
        _balloon(self.icon, "PaperAgent 仍在后台运行",
                 "窗口已隐藏到托盘：点托盘图标可重新打开，选「退出 PaperAgent」才会完全结束。")
        logger.info("窗口关闭 → 隐藏到托盘（后端继续运行）")
        return False


def native_window_supported() -> bool:
    """原生窗口是否可用（WebView2 + pywebview）。不可用则由调用方走浏览器回退。"""
    if not webview2_available():
        logger.warning("WebView2 运行时不可用 → 回退浏览器 app 窗口")
        return False
    try:
        import webview  # noqa: F401
    except Exception as e:  # noqa: BLE001
        logger.warning("pywebview 不可用（%s）→ 回退浏览器 app 窗口", e)
        return False
    return True


def run(server, url: str, request_quit, storage_dir: Path | None = None,
        on_teardown_done=None) -> None:
    """主线程跑 GUI：后端 uvicorn 在后台线程；返回时后端已请求关停。

    窗口起不来时**不重复起后端**：保留托盘、回退浏览器窗口，主线程阻塞到后端退出。
    `on_teardown_done`：托盘 + 窗口**真的拆完**时回调（供 `main` 的 watchdog 等它，
    避免在 WebView2 拆卸途中终止进程 —— 那会留下占端口的"半死"进程）。
    """
    import webview

    global _active_shell
    shell = Shell(url, request_quit, on_teardown_done=on_teardown_done)
    _active_shell = shell
    t = threading.Thread(target=server.run, name="uvicorn-server", daemon=True)
    t.start()
    ready, died = wait_backend_or_dead(url, t)
    if ready:
        logger.info("后端就绪，打开原生窗口（最大化）: %s", url)
    elif died:
        msg = (f"PaperAgent 后端启动失败（端口 {url} 可能已被占用）。\n\n"
               f"请先退出已在运行的 PaperAgent（托盘图标 → 退出，或 PaperAgent.exe --quit），"
               f"或结束占用该端口的程序后重试。\n\n日志：logs\\paperagent.log")
        logger.error("后端线程已退出，判定为启动失败（端口占用等），不打开窗口")
        fatal_dialog(msg)
        try:
            request_quit("backend start failed")
        except Exception:  # noqa: BLE001
            pass
        return
    else:
        logger.warning("后端健康检查 %.0fs 未就绪，仍打开窗口（页面会自行重试）", HEALTH_TIMEOUT)

    kwargs: dict = {}
    if storage_dir is not None:
        try:
            storage_dir.mkdir(parents=True, exist_ok=True)
            kwargs["storage_path"] = str(storage_dir)
        except OSError as e:
            logger.warning("界面存储目录不可用（%s），界面偏好将不持久", e)

    win = webview.create_window(
        WINDOW_TITLE, url, maximized=True, min_size=(1024, 640),
        text_select=True, confirm_close=False, background_color="#ffffff",
    )
    shell.win = win
    win.events.closing += shell.on_closing
    shell.icon = build_tray(on_show=shell.show, on_quit=shell.quit,
                            on_open_browser=shell.open_browser)

    try:
        webview.start(gui="edgechromium",
                      debug=bool(os.environ.get("PAPERAGENT_WEBVIEW_DEBUG")),
                      private_mode=False, **kwargs)
    except Exception as e:  # noqa: BLE001 - 窗口起不来 → 回退浏览器（**不重启后端**）
        logger.exception("原生窗口启动失败，回退浏览器窗口: %s", e)
        open_app_window(url)
        while t.is_alive():
            t.join(timeout=1.0)
        try:
            if shell.icon is not None:
                shell.icon.stop()
        except Exception:  # noqa: BLE001
            pass
        return

    if not shell.quitting:
        logger.warning("窗口循环结束但未走退出流程 → 主动请求优雅关停")
        try:
            request_quit("window loop ended")
        except Exception:  # noqa: BLE001
            pass
        try:
            if shell.icon is not None:
                shell.icon.stop()
        except Exception:  # noqa: BLE001
            pass
    t.join(timeout=GRACE_WAIT + 5)      # 等后端收尾（watchdog 兜底硬退）
    _active_shell = None
    logger.info("桌面壳结束（后端线程存活=%s）", t.is_alive())
