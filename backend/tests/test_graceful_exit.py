# -*- coding: utf-8 -*-
"""退出路径守卫（2026-09-12 用户实测报障回归）。

**报障**：打包版退出后偶发留下"半死"进程——只剩 1 个线程、LISTEN socket 仍活
（TCP 能连但 `/api/health` 超时）、`taskkill /F` 报 "There is no running instance of the task"、
端口 `bind` 报 **WinError 10013** ⇒ 用户下次双击直接报「端口被占用」，只能重启系统或换端口。
实测两次（用户托盘退出 + 我的验证实例 `/api/desktop/quit`）。

**根因**：进程退出发生在 **WebView2/COM 拆卸途中**（日志里 `should_exit` 到 `os._exit` 只隔 24ms），
`os._exit`/`ExitProcess` 撞上内核态等待 ⇒ 终止无法完成，socket 随进程对象被吊住。

**修法（本文件锁死）**：
1. `_release_listen_sockets()`：进入任何重拆卸之前**主动关闭监听 socket** ⇒ 端口立刻可用；
2. `_mark_shell_teardown_done()` + watchdog 有界等待 ⇒ 不再在 COM 拆卸中途终止进程；
3. 桌面壳 `Shell.quit()` 在窗口销毁**之后**回报（顺序由本文件断言）。
"""
from __future__ import annotations


def test_release_listen_sockets_closes_all_and_is_defensive():
    import app.main as m

    class _Sock:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class _Server:
        def __init__(self, socks):
            self._server = type("Inner", (), {"sockets": socks})()

    a, b = _Sock(), _Sock()
    assert m._release_listen_sockets(_Server([a, b])) == 2
    assert a.closed and b.closed, "监听 socket 必须被主动关闭（否则端口被吊住）"
    # 没有 asyncio server / 传错对象：不抛异常（退出路径不能因它崩）
    assert m._release_listen_sockets(object()) == 0
    assert m._release_listen_sockets(None) == 0


def test_release_listen_sockets_survives_single_close_error():
    import app.main as m

    class _Bad:
        def close(self):
            raise OSError("boom")

    class _Good:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    bad, good = _Bad(), _Good()
    outer = type("S", (), {"_server": type("I", (), {"sockets": [bad, good]})()})()
    assert m._release_listen_sockets(outer) == 1, "单个 socket 关闭失败不该影响其它"
    assert good.closed


def test_mark_shell_teardown_done_sets_event():
    import app.main as m

    m._shell_teardown_done.clear()
    m._mark_shell_teardown_done()
    assert m._shell_teardown_done.is_set()
    m._shell_teardown_done.clear()


def test_shell_quit_reports_teardown_after_window_destroy():
    """顺序硬约束：**先销毁窗口**（WebView2/COM 拆卸），**再**回报"拆卸完成"。

    反了就等于"在 COM 拆卸中途让 watchdog 退进程" —— 那正是"半死"进程的成因。
    """
    from app.desktop import Shell

    calls: list[tuple[str, str]] = []
    shell = Shell("http://127.0.0.1:1",
                  lambda reason: calls.append(("request_quit", reason)),
                  on_teardown_done=lambda: calls.append(("teardown_done", "")))

    class _Win:
        def destroy(self):
            calls.append(("win_destroy", ""))

    class _Icon:
        def stop(self):
            calls.append(("icon_stop", ""))

    shell.win, shell.icon = _Win(), _Icon()
    shell.quit()

    names = [c[0] for c in calls]
    assert "win_destroy" in names and "teardown_done" in names
    assert names.index("win_destroy") < names.index("teardown_done"), \
        "必须在窗口销毁之后才回报拆卸完成"
    assert shell.quitting is True
    # 幂等：再点一次不重复走流程
    before = len(calls)
    shell.quit()
    assert len(calls) == before
