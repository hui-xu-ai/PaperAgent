# -*- coding: utf-8 -*-
"""FastAPI 入口：lifespan 初始化容器 → 路由挂载 → 静态前端托管。

注意：静态挂载必须在所有 API 路由之后（避免遮蔽 /api/*）。
"""
from __future__ import annotations

import asyncio, os, sys, logging, threading
from contextlib import asynccontextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

# 引擎资源适配必须在任何引擎模块 import 之前（引擎仓库目录重构后 asset_root 失效）
from .engine_patch import apply_engine_asset_patch

apply_engine_asset_patch()

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import chat, diary, events, kb, kbmeta, library, lit, papers, plugins, review, tasks, usage
from .api import settings as settings_api
from .config import (APP_DATA_DIR, APP_VERSION, FRONTEND_DIR, get_settings,
                     live_mineru_key, mineru_ready, resolve_live_mineru_parser)
from .services import container

from .memory_trimmer import start_periodic_trim, delayed_trim

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    container.init_container(get_settings())
    container.register_plugins(app)  # 插件 on_register（在路由挂载后）
    yield


app = FastAPI(title="PaperAgent", version=APP_VERSION, lifespan=lifespan)

# P5 点3：随窗口关闭自动退出——记录最近一次 API 活动时间（前端 5s 轮询即心跳）；
# 前端关窗后无请求，空闲超时**优雅**退出（beforeunload sendBeacon 立即关兜底）。
import time as _time
IDLE_EXIT_TIMEOUT = 180          # 秒：前端关窗后最长空转多久退出
_last_activity = _time.time()

# ---------------------------------------------------------------- 优雅关停（2026-09-12）
# 用户模型：关停不能掐断在跑任务。做法：uvicorn `server.should_exit = True`
# （停止接新请求 → 等在飞请求结束 → 触发 FastAPI lifespan 收尾 → 进程自然退出），
# 并保留一个**兜底**：GRACEFUL_EXIT_GRACE 秒宽限窗用尽后硬退出（防挂死）。
_server_ref = None               # uvicorn.Server（run() 里赋值；dev/frozen 都赋）
_exit_requested = threading.Event()
_shell_teardown_done = threading.Event()     # 桌面壳（托盘 + WebView2 窗口）拆卸完成信号
GRACEFUL_EXIT_GRACE = float(os.environ.get("PAPERAGENT_GRACE_SECONDS", "30"))  # 秒：宽限窗
SHELL_TEARDOWN_WAIT = float(os.environ.get("PAPERAGENT_TEARDOWN_WAIT", "8"))   # 秒：等壳拆完


def _mark_shell_teardown_done() -> None:
    """桌面壳拆卸完成（由 `desktop.Shell.quit` 在窗口销毁后调用）。见 watchdog 里的说明。"""
    _shell_teardown_done.set()


def _release_listen_sockets(srv) -> int:
    """主动关闭 uvicorn 的监听 socket ⇒ **端口立刻可用**（不依赖进程是否退出）。

    2026-09-12 实测（打包版，用户与我的验证实例各一次）：退出后进程偶发停在"半死"态——
    只剩 1 个线程、LISTEN socket 仍活（TCP 能连但 `/api/health` 超时）、`taskkill /F` 报
    `There is no running instance of the task`、端口 `bind` 报 **WinError 10013** ⇒ 用户下次
    双击直接报"端口被占用"，只能重启系统或换端口。
    成因：`os._exit` / `ExitProcess` 撞上 **WebView2 拆卸**（COM/内核态等待）⇒ 进程终止
    无法完成，socket 随进程对象一起被吊住。
    因此：**在进入任何重拆卸之前先关监听 socket**，即使进程卡住，端口也不再被占。
    """
    closed = 0
    try:
        inner = getattr(srv, "_server", None)        # uvicorn: Server._server 是 asyncio.Server
        for sock in list(getattr(inner, "sockets", None) or []):
            try:
                sock.close()
                closed += 1
            except Exception:  # noqa: BLE001 - 单个 socket 关闭失败不影响其它
                pass
    except Exception:  # noqa: BLE001
        pass
    return closed


def _request_graceful_exit(reason: str = "") -> bool:
    """请求优雅关停；返回是否已交给 uvicorn（False=没有 server 引用，需调用方兜底）。"""
    logger.info("请求优雅关停（%s）：等待在飞请求与任务收尾…", reason or "未注明")
    _exit_requested.set()
    srv = _server_ref
    if srv is not None:
        try:
            srv.should_exit = True
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("设置 server.should_exit 失败: %s", e)
    return False


def _graceful_exit_watchdog() -> None:
    """后台线程：收到关停请求后**先给在跑任务一个宽限窗**，超时再硬退出。

    为什么必须由它来退：uvicorn 的 `Server.run()` 在 `should_exit` 后会**立刻返回**
    （进程随即结束，而解析/翻译 worker 是 daemon 线程 → 在跑任务被当场掐死、产物半写）。
    所以宽限窗放在这里：主线程随后进入 `_graceful_wait()` 循环，只有本线程走完宽限窗
    后置位 `_exit_requested` 的"已完成"路径才会让主线程结束 → 进程退出。
    超时（宽限窗用尽）→ 硬退，避免"永远不退"；残留在跑任务由
    `TaskManager._recover_orphans` 在下次启动时重新入队（既有安全网）。
    """
    _exit_requested.wait()
    srv = _server_ref
    if srv is not None:
        try:
            srv.should_exit = True          # 停止接收新请求
        except Exception:  # noqa: BLE001
            pass
        closed = _release_listen_sockets(srv)
        if closed:
            logger.info("已主动关闭 %d 个监听 socket（端口立即释放，不依赖进程退出）", closed)
    _graceful_wait()
    # 等桌面壳把托盘 + WebView2 窗口拆干净**再**退出：在 COM 拆卸中途 ExitProcess 会让
    # 进程停在"半死"态（占住端口、taskkill 都杀不掉）——见 `_release_listen_sockets` 实测记录。
    # 有界等待，且**只在桌面壳真的活着时等**（否则白等 8s：实测冒烟退出从 3.6s 变 11.6s）。
    try:
        shell_active = desktop.shell_is_active()
    except Exception:  # noqa: BLE001
        shell_active = False
    if shell_active and not _shell_teardown_done.wait(SHELL_TEARDOWN_WAIT):
        logger.warning("桌面壳拆卸未在 %.0fs 内完成（窗口可能卡住）→ 照常退出",
                       SHELL_TEARDOWN_WAIT)
    logger.warning("优雅关停完成，进程退出")
    os._exit(0)


def _graceful_wait() -> None:
    """有界等待在跑任务收尾（无任务 → 立即返回；有任务 → 最多 GRACEFUL_EXIT_GRACE 秒）。"""
    deadline = _time.time() + GRACEFUL_EXIT_GRACE
    while _time.time() < deadline:
        try:
            n = container.get_tasks().active_tasks()
        except Exception:  # noqa: BLE001 - 容器未初始化/拿不到任务管理器
            n = 0
        if n == 0:
            logger.info("优雅关停：无在跑任务，正常退出")
            return
        if int(_time.time()) % 5 == 0:      # 每 5s 提示一次，别刷屏
            logger.info("优雅关停：仍有 %d 个在跑任务，最多再等 %.0fs…",
                        n, max(0.0, deadline - _time.time()))
        _time.sleep(0.5)
    logger.warning("优雅关停宽限窗（%.0fs）用尽，仍有任务在跑 → 强制退出"
                   "（下次启动会自动重新入队）", GRACEFUL_EXIT_GRACE)


@app.middleware("http")
async def _track_activity(request: Request, call_next):
    global _last_activity
    _last_activity = _time.time()
    return await call_next(request)


@app.middleware("http")
async def _static_no_heuristic_cache(request: Request, call_next):
    """前端静态资源禁用浏览器"启发式缓存"（2026-09-11 P0-2 修复）。

    根因：StaticFiles 只发 ETag/Last-Modified、**不发 Cache-Control**，浏览器按启发式
    规则（约 Last-Modified 后 10% 时长）直接复用旧 app.js → "代码已改，页面还在跑旧 JS"
    （实测：新增的「🔄 同步磁盘」按钮点了没反应，必须手动刷新页面才生效）。

    规则：非 /api 且非 /vendor/（第三方大文件 pdf.js/katex 保留默认缓存）→ no-cache：
    浏览器每次仍带 If-None-Match 校验，命中即 304，代价很小，但绝不会静默用旧代码。
    """
    resp = await call_next(request)
    path = request.url.path
    if not path.startswith("/api") and not path.startswith("/vendor/"):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


def _idle_exit_loop() -> None:
    """后台线程：若开启"随窗口关闭退出"且长时间无前端活动 → **优雅**退出。

    ⚠️ 2026-09-12：旧实现 `os._exit(0)` 会掐断在跑任务、产物半写；现走
    `_request_graceful_exit()`（uvicorn 正常关停 + 超时兜底）。
    """
    while True:
        _time.sleep(10)
        try:
            auto = container.get_settings_service().get_auto_exit()
        except Exception:  # noqa: BLE001 - 探测失败按开启处理
            auto = True
        if auto and (_time.time() - _last_activity) > IDLE_EXIT_TIMEOUT:
            logger.info("前端窗口已关闭（空闲 %ds），优雅退出 PaperAgent", IDLE_EXIT_TIMEOUT)
            _request_graceful_exit("idle timeout")

# API 路由
app.include_router(papers.router)
app.include_router(tasks.router)
app.include_router(chat.router)
app.include_router(events.router)
app.include_router(usage.router)
app.include_router(settings_api.router)
app.include_router(kb.router)
app.include_router(kbmeta.router)
app.include_router(lit.router)
app.include_router(library.router)
app.include_router(review.router)
app.include_router(plugins.router)
app.include_router(diary.router)


@app.get("/api/health")
def health() -> JSONResponse:
    """健康检查：服务 + 引擎 + LLM/MinerU 配置探测。"""
    engine_ready = False
    engine_error = ""
    try:
        import paperparse  # noqa: F401
        engine_ready = True
    except Exception as e:  # pragma: no cover
        engine_error = str(e)
    engine_version = ""
    try:
        from paperparse import __version__ as _ev
        engine_version = _ev
    except Exception:  # noqa: BLE001
        engine_version = ""
    # 数据资产版本（docs/VERSIONING.md §1）：升级后用它核对"程序版本 vs 数据格式"
    data_format = 0
    try:
        from .services import container as _c

        data_format = int(_c.get_assets().data_format())
    except Exception:  # noqa: BLE001 - 容器未就绪时不影响健康检查
        data_format = 0
    return JSONResponse({
        "status": "ok",
        "version": APP_VERSION,
        "data_format": data_format,
        "engine_version": engine_version,
        "engine_ready": engine_ready,
        "engine_error": engine_error,
        "deepseek_configured": bool(settings.deepseek_api_key),
        # 批1：MinerU 配置以 .env 为单一来源，设置中心保存**即时生效** ⇒ 这里必须读实时值
        # （settings 是启动期冻结快照；live_* 在 os.environ 无该键时回退快照）。
        # 批2：`mineru_ready=False` = 解析被硬门禁禁用（无 Key），前端据此显示警示条。
        "mineru_configured": bool(live_mineru_key(settings.mineru_api_key)),
        "mineru_ready": mineru_ready(settings.mineru_api_key),
        "mineru_parser": resolve_live_mineru_parser(settings.mineru_api_key,
                                                    settings.mineru_parser),
        "llm_ready": container.llm_ready(),
        # 单实例闸门用（2026-09-12）：让"新进程"能判断占端口的到底是谁、能不能唤醒窗口
        "desktop": _desktop_shell_alive(),
        "pid": os.getpid(),
        "app_data_dir": str(APP_DATA_DIR),
    })


def _desktop_shell_alive() -> bool:
    """本进程是否跑着桌面壳（有窗口可唤起）。"""
    try:
        from . import desktop
        return desktop.active_shell() is not None
    except Exception:  # noqa: BLE001
        return False


def _component_versions() -> dict:
    """各组件版本汇总（**单一来源**）：后端/前端资源/算法包/知识库库/数据格式。

    前端版本读的是**实际随包发出的** `frontend/version.js`（而非后端自报），
    因此能发现"前后端版本打歪"这一类发布事故（`docs/RELEASE-CHECKLIST.md`）。
    """
    import paperkb
    import paperparse

    from .version import LAYOUT_VERSION, MIN_READABLE_DATA_FORMAT, read_frontend_version

    data_format = 0
    try:
        data_format = int(container.get_assets().data_format())
    except Exception:  # noqa: BLE001 - 容器未就绪不影响版本展示
        data_format = 0
    comp = {
        "app": APP_VERSION,
        "backend": APP_VERSION,
        "frontend": read_frontend_version(FRONTEND_DIR),
        "paperkb": getattr(paperkb, "__version__", ""),
        "paperparse": getattr(paperparse, "__version__", ""),
        "data_format": data_format,
        "layout": LAYOUT_VERSION,
        "min_readable_data_format": MIN_READABLE_DATA_FORMAT,
        "frozen": bool(getattr(sys, "frozen", False)),
    }
    comp["frontend_matches_backend"] = bool(comp["frontend"]) and comp["frontend"] == APP_VERSION
    return comp


@app.get("/api/version")
def version_info() -> JSONResponse:
    """版本契约端点：GUI「关于」与发布自检都读这里。"""
    return JSONResponse({"status": "ok", **_component_versions()})


def _localhost_only(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        logger.warning("拒绝非本机请求: path=%s host=%s", request.url.path, host or "<none>")
        raise HTTPException(status_code=403, detail="only allowed from localhost")


@app.post("/api/desktop/show")
async def desktop_show(request: Request) -> JSONResponse:
    """唤起主窗口（第二个实例/快捷方式用）；无桌面壳时返回 window=false。"""
    _localhost_only(request)
    from . import desktop

    ok = desktop.show_window()
    return JSONResponse({"status": "ok", "window": ok})


@app.post("/api/desktop/quit")
async def desktop_quit(request: Request) -> JSONResponse:
    """**显式**退出（托盘/命令行 `--quit`）：不受 auto_exit 开关限制。

    与 `/api/system/shutdown` 的区别：后者是"随窗口关闭自动退出"的兜底，默认关；
    本端点是用户主动点「退出」的语义 ⇒ 必定优雅关停并释放端口。
    """
    _localhost_only(request)
    logger.info("收到显式退出请求（/api/desktop/quit），优雅关停中…")
    # ⚠️ 不要在这里从后台线程去 `win.destroy()`：pywebview/WinForms 的窗口销毁必须在 GUI 线程，
    # 跨线程销毁会卡死（2026-09-12 实测：冒烟 60s 进程不退出、端口仍占，比不修更糟）。
    # 窗口/托盘由 watchdog 的"先放端口 + 有界等拆卸 + os._exit"兜住即可（见 _release_listen_sockets）。
    asyncio.create_task(_shutdown_worker())
    return JSONResponse({"status": "ok", "message": "quit scheduled"})


@app.post("/api/system/shutdown")
async def system_shutdown(request: Request) -> JSONResponse:
    """一键关停（仅允许本机调用；**默认关=忽略，不退出**——随窗口关闭自动退出默认关）。

    （用户反馈：不要后台监督窗口关闭、不要因关窗误伤后端。故此端点仅当设置里开启
    "随窗口关闭自动退出(auto_exit)" 才真正关停；默认 auto_exit=关，关窗不杀后端。
    2026-09-12：关停改为**优雅**（uvicorn 停止接新请求 → 等在飞请求/任务收尾 →
    lifespan 退出；15s 兜底硬退），不再 `os._exit` 掐断在跑任务。）
    """
    try:
        if not container.get_settings_service().get_auto_exit():
            logger.info("收到本机关停请求，但 auto_exit 已关闭，忽略（后台保持运行）")
            return JSONResponse({"status": "ok", "message": "auto_exit off, ignored"})
    except Exception:  # noqa: BLE001 - 设置读取失败时保守忽略
        return JSONResponse({"status": "ok", "message": "auto_exit check failed, ignored"})
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        logger.warning("拒绝非本机关停请求: host=%s", host or "<none>")
        raise HTTPException(status_code=403, detail="shutdown is only allowed from localhost")
    logger.info("收到本机关停请求，PaperAgent 即将退出")
    asyncio.create_task(_shutdown_worker())
    return JSONResponse({"status": "ok", "message": "shutdown scheduled"})


async def _shutdown_worker() -> None:
    """延迟片刻后**优雅**关停（确保 shutdown 响应已送达客户端）。

    ⚠️ 2026-09-12（用户模型：优雅关停）：旧实现 `os._exit(0)` 会硬杀进程——
    正在跑的解析/翻译/编译产物会半写（SQLite 事务、markdown 写到一半）。
    现在改为 uvicorn 正常关停（跑完在飞请求 → 触发 lifespan 收尾），
    并提供 15s 兜底（`GRACEFUL_EXIT_BACKSTOP`），避免永远不退出。
    """
    await asyncio.sleep(0.5)
    _request_graceful_exit("api shutdown")


# 静态前端（放最后，避免遮蔽 /api 路由）
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")


def _make_tray_icon(server) -> object | None:
    """系统托盘（2026-09-12 起统一委托 `app.desktop`）。

    图标 = 打包资源/exe 同目录 `icon.ico`（**与程序图标同一份文件**）；
    菜单：显示主窗口 / 在浏览器中打开 / 退出（退出 = 优雅关停，端口随进程释放）。
    """
    from . import desktop

    url = f"http://{settings.host}:{settings.port}"
    return desktop.build_tray(
        on_show=lambda: (desktop.show_window() or desktop.open_app_window(url)),
        on_quit=lambda: _request_graceful_exit("tray quit"),
        on_open_browser=lambda: desktop.open_app_window(url),
        tooltip=f"PaperAgent v{APP_VERSION} · 文献 AI 阅读",
    )


def _open_app_window(url: str) -> None:
    """兼容入口：转发 `desktop.open_app_window`（Edge/Chrome --app 干净窗口 + 强制最大化）。"""
    from . import desktop

    desktop.open_app_window(url)


def _setup_file_logging() -> str:
    """把日志同时落到 `logs/paperagent.log`（10MB×3 轮转）；返回日志文件路径。

    背景（工程化 P0）：此前**日志从不落盘**——排障只能看控制台，用户报"翻译失败"
    时没有任何现场（进程重启即丢）。这里在入口挂一个 RotatingFileHandler 到 root
    logger，uvicorn / app / paperkb 的日志一并落盘（幂等，重复调用不会重复挂）。
    级别由 `PAPERAGENT_LOG_LEVEL` 控制（默认 INFO）。
    """
    import logging.handlers

    # 2026-09-12：日志目录改走 APP_DATA_DIR（打包后 = exe 同目录），
    # 不再靠 `__file__` 反推（frozen 下 __file__ 在 _MEIPASS 里，路径语义脆弱）。
    from .config import APP_DATA_DIR

    log_dir = APP_DATA_DIR / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return ""
    log_file = log_dir / "paperagent.log"
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, logging.handlers.RotatingFileHandler) \
                and Path(getattr(h, "baseFilename", "")) == log_file:
            return str(log_file)          # 已挂过（例如 run() 被重复调用）
    level = os.environ.get("PAPERAGENT_LOG_LEVEL", "INFO").upper()
    handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.setLevel(getattr(logging, level, logging.INFO))
    root.addHandler(handler)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    logger.info("日志落盘: %s（level=%s）", log_file, level)
    return str(log_file)


# 2026-09-20：`uvicorn backend.app.main:app` 直启（不经 run() 入口）此前**不挂** root
# FileHandler ⇒ event_bus/compile/usage 日志全丢控制台 ⇒ 排障时日志加总 < 面板账本。
# 模块导入期幂等挂一次，两种启动方式都落盘。
try:
    _setup_file_logging()
except Exception:  # noqa: BLE001 - 落盘失败不阻塞启动
    pass


def _desktop_storage_dir() -> Path:
    """WebView2 用户数据（localStorage 等界面偏好）落 `work/desktop_profile`。

    放 `work/` 的理由（`docs/DATA-LAYOUT.md`）：界面偏好可重建、随时可清，既不是用户资产，
    也不该污染 `data/`（五库 + manifest 的语义）。
    """
    from .config import APP_DATA_DIR

    return APP_DATA_DIR / "work" / "desktop_profile"


def _handle_cli_control(url: str) -> bool:
    """`--show` / `--quit` 与单实例：返回 True 表示本进程应立即结束（不启动后端）。"""
    from . import desktop

    args = sys.argv[1:]
    alive = desktop.running_instance(url)
    if any(a in args for a in ("--quit", "--stop")):
        if not alive:
            logger.info("--quit：没有检测到运行中的 PaperAgent 实例")
            return True
        logger.info("--quit：请求运行中的实例退出（version=%s）", alive.get("version"))
        desktop.signal_instance(url, "quit")
        deadline = _time.time() + 60
        while _time.time() < deadline and desktop.running_instance(url):
            _time.sleep(0.5)
        logger.info("--quit：%s", "实例已退出，端口已释放"
                    if not desktop.running_instance(url) else "等待超时（实例仍在运行）")
        return True
    if alive:
        # 已有实例：优先唤起它的窗口；**唤不起来就必须让人看见**——绝不能静默退出
        # （2026-09-12 用户实测："双击 PaperAgent.exe 没反应，要去任务管理器杀掉后台才能运行"：
        #  占端口的是开发版后端（无窗口、无 /api/desktop/show ⇒ 404），旧实现只写日志就退出。）
        resp = desktop.signal_instance_ex(url, "show")
        if resp and resp.get("window"):
            logger.info("PaperAgent 已在运行（%s）→ 已唤起已有窗口，本进程退出", url)
            return True
        mode = "桌面窗口模式" if alive.get("desktop") else "后台/开发模式（没有窗口可唤起）"
        msg = (
            f"PaperAgent 已经在运行，端口被它占用，本次启动已取消。\n\n"
            f"· 占用地址：{url}\n"
            f"· 进程 PID：{alive.get('pid', '?')}\n"
            f"· 运行模式：{mode}\n"
            f"· 数据目录：{alive.get('app_data_dir', '?')}\n\n"
            f"请先退出那个进程，再重新启动：\n"
            f"  1) 命令行执行：PaperAgent.exe --quit\n"
            f"  2) 或右键右下角托盘图标 → 「退出 PaperAgent」\n"
            f"  3) 或任务管理器结束 PID {alive.get('pid', '?')} 后重试\n\n"
            f"（开发/调试时占端口的是 `python -m app.main`：在它的控制台按 Ctrl+C 结束即可）")
        logger.error("单实例闸门：%s", msg.replace("\n", " "))
        if getattr(sys, "frozen", False):
            desktop.fatal_dialog(msg)
        return True
    return False


def run() -> None:
    """入口：python -m app.main / paperagent 命令 / PyInstaller exe。

    形态（2026-09-12 发布轮）：
    - **桌面壳**（打包版默认 / 开发态 `--desktop`）：原生 WebView2 窗口（启动即最大化）
      + 系统托盘；关窗 = 隐藏到托盘（后端与任务继续），托盘「退出」= 完全关停并释放端口。
    - **服务模式**（开发态默认）：主线程 uvicorn，按需自行访问 http://host:port。
    """
    import uvicorn

    global _server_ref
    _setup_file_logging()
    from . import desktop

    url = f"http://{settings.host}:{settings.port}"
    try:
        if _handle_cli_control(url):
            return
    except Exception:  # noqa: BLE001 - 单实例探测失败不阻塞启动
        logger.warning("单实例探测失败（忽略）", exc_info=True)

    # 内存自清理（V13：必须在阻塞的 uvicorn.run 之前启动）
    delayed_trim(delay=2.0)
    start_periodic_trim(interval=300)
    # 优雅关停线程（先停在飞请求收尾，再给在跑任务 GRACEFUL_EXIT_GRACE 秒宽限）
    threading.Thread(target=_graceful_exit_watchdog, daemon=True).start()

    # 不再启动"随窗口关闭自动退出"监督循环（用户反馈：后台监督窗口关闭、打扰清理）。
    # threading.Thread(target=_idle_exit_loop, daemon=True).start()

    # V12：一律用 uvicorn.Server（可控退出），以便优雅关停（dev 也持有 server 引用）。
    config = uvicorn.Config(app, host=settings.host, port=settings.port, reload=False)
    server = uvicorn.Server(config)
    _server_ref = server

    use_desktop = desktop.is_desktop_requested()
    try:
        if use_desktop and desktop.native_window_supported():
            desktop.run(server, url, _request_graceful_exit,
                        storage_dir=_desktop_storage_dir(),
                        on_teardown_done=_mark_shell_teardown_done)
            # 窗口已关：等任务收尾（watchdog 兜底硬退），再让进程结束
            _graceful_wait()
            logger.warning("优雅关停完成（桌面壳收尾），进程退出")
            # 硬退出：正常 return 会进入解释器 finalize，若有线程卡在 COM/内核态会挂住
            # （表现就是"进程不退出、端口仍占"，实测过）。socket 已在 watchdog 里释放。
            os._exit(0)
        if use_desktop:
            logger.warning("桌面壳不可用（WebView2/pywebview）→ 回退浏览器 app 窗口")
            desktop.open_app_window(url)
        if getattr(sys, "frozen", False) or use_desktop:
            _make_tray_icon(server)
        server.run()
    except SystemExit as e:                      # uvicorn 端口占用等致命错误
        code = getattr(e, "code", 1)
        if code:
            logger.error("启动失败（exit=%s）——端口 %s 可能已被占用", code, settings.port)
            if getattr(sys, "frozen", False):
                desktop.fatal_dialog(
                    f"PaperAgent 启动失败（exit={code}）。\n\n"
                    f"端口 {settings.port} 可能已被占用：请先退出已在运行的 PaperAgent"
                    f"（托盘图标 → 退出），或结束占用该端口的程序。\n\n"
                    f"日志：logs\\paperagent.log")
        raise
    except Exception as e:  # noqa: BLE001 - 打包版无控制台：必须弹窗告知
        logger.exception("启动失败: %s", e)
        if getattr(sys, "frozen", False):
            desktop.fatal_dialog(f"PaperAgent 启动失败：\n{e}\n\n日志：logs\\paperagent.log")
        raise
    # uvicorn 已停止接收请求；若这次退出是**关停请求**触发的，等宽限窗跑完再让进程结束
    # （watchdog 负责置位/超时硬退；这里只是别让主线程立刻返回把 daemon worker 掐死）。
    if _exit_requested.is_set():
        _graceful_wait()
        logger.warning("优雅关停完成（主线程收尾），进程退出")
    else:
        logger.warning("uvicorn 已退出（非关停请求触发），进程结束")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/ 入 path
    run()
