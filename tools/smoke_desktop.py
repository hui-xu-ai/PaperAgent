#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""桌面壳冒烟测试（**真实链路**）：窗口最大化 / 关窗隐藏到托盘 / 托盘退出释放端口。

用法：
  # 开发态（backend 源码 + venv python）
  python tools/smoke_desktop.py --dev
  # 打包态（dist 产物）
  python tools/smoke_desktop.py --exe dist/PaperAgent/PaperAgent.exe

判据（逐条打印 OK/!!，全部 OK 才 exit 0）：
  1) 后端就绪（/api/health 200）
  2) 窗口出现且 **IsZoomed=true**（启动即最大化），矩形覆盖屏幕工作区
  3) 窗口标题/类名不含浏览器痕迹（类名 WindowsForms10.* = 原生 WebView2 宿主，不是 Chrome_WidgetWin）
  4) PostMessage(WM_CLOSE) → 窗口隐藏（IsWindowVisible=false）但**后端仍活着**
  5) POST /api/desktop/show → 窗口重新可见
  6) POST /api/desktop/quit → 进程在宽限窗内退出、**端口释放**
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE_URL = "http://127.0.0.1:8900"      # main() 按 --port 覆写
_port = 8900
WM_CLOSE = 0x0010
u32 = ctypes.windll.user32
results: list[tuple[str, bool, str]] = []


def mark(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"    {'OK ' if ok else '!! '}{name}" + (f"  [{detail}]" if detail else ""))


def http_json(path: str, method: str = "GET", timeout: float = 3.0) -> dict | None:
    try:
        req = urllib.request.Request(BASE_URL + path, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return None


def port_open(port: int = 0) -> bool:
    with socket.socket() as s:
        s.settimeout(0.6)
        return s.connect_ex(("127.0.0.1", port or _port)) == 0


def wait_health(timeout: float = 60.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if (http_json("/api/health") or {}).get("status") == "ok":
            return True
        time.sleep(0.5)
    return False


def windows_of_pid(pid: int) -> list[tuple[int, str, str]]:
    out: list[tuple[int, str, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lp):
        owner = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid or not u32.IsWindowVisible(hwnd):
            return True
        n = u32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 2) if n else ctypes.create_unicode_buffer(2)
        u32.GetWindowTextW(hwnd, buf, n + 1)
        cls = ctypes.create_unicode_buffer(256)
        u32.GetClassNameW(hwnd, cls, 256)
        out.append((hwnd, buf.value, cls.value))
        return True

    u32.EnumWindows(_cb, 0)
    return out


def rect_of(hwnd: int) -> list[int]:
    r = wintypes.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(r))
    return [r.left, r.top, r.right, r.bottom]


def workarea() -> list[int]:
    wa = wintypes.RECT()
    u32.SystemParametersInfoW(0x0030, 0, ctypes.byref(wa), 0)
    return [wa.left, wa.top, wa.right, wa.bottom]


def main() -> int:
    global BASE_URL, _port
    ap = argparse.ArgumentParser()
    ap.add_argument("--dev", action="store_true")
    ap.add_argument("--exe", default="")
    ap.add_argument("--port", type=int, default=8900)
    args = ap.parse_args()
    _port = args.port
    BASE_URL = f"http://127.0.0.1:{args.port}"
    env = dict(os.environ, PAPERAGENT_PORT=str(args.port))

    if port_open(args.port):
        print(f"!! 端口 {args.port} 已被占用（先退出正在运行的 PaperAgent）："
              f"PaperAgent.exe --quit 或托盘退出")
        return 2

    log = ROOT / "work" / "scratch" / "smoke_desktop_stdout.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    if args.exe:
        exe = Path(args.exe)
        if not exe.is_absolute():
            exe = ROOT / exe
        cmd = [str(exe)]
        cwd = str(exe.parent)
        print(f"=== 打包态冒烟：{exe} ===")
    elif args.dev:
        cmd = [sys.executable, "-m", "app.main", "--desktop"]
        cwd = str(ROOT / "backend")
        print("=== 开发态冒烟：python -m app.main --desktop ===")
    else:
        print("需指定 --dev 或 --exe")
        return 2

    with log.open("w", encoding="utf-8", errors="replace") as fh:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=fh, stderr=subprocess.STDOUT, env=env)
    print(f"    进程已起 pid={proc.pid}（输出: {log.relative_to(ROOT)}）")
    try:
        # 1) 后端就绪
        ok = wait_health(90.0)
        mark("后端就绪 /api/health", ok)
        if not ok:
            return 1
        h = http_json("/api/health") or {}
        print(f"    health: version={h.get('version')} data_format={h.get('data_format')} "
              f"engine={h.get('engine_version')}")
        v = http_json("/api/version") or {}
        print(f"    version: app={v.get('app')} frontend={v.get('frontend')} "
              f"paperkb={v.get('paperkb')} paperparse={v.get('paperparse')} "
              f"match={v.get('frontend_matches_backend')}")
        mark("前后端版本一致（/api/version）", v.get("frontend_matches_backend") is True,
             f"frontend={v.get('frontend')} app={v.get('app')}")
        # 数据层可通行（paperkb / sqlite / pymupdf 依赖在打包后真的能加载）
        km = http_json("/api/kb-meta/kind/options")
        mark("数据层可用（/api/kb-meta/kind/options）", km is not None, str(km)[:70])

        # 2) 窗口：出现 + 最大化 + 原生类名
        hwnd = title = cls = None
        end = time.time() + 30
        while time.time() < end and hwnd is None:
            for h_, t_, c_ in windows_of_pid(proc.pid):
                if c_.startswith("WindowsForms10"):
                    hwnd, title, cls = h_, t_, c_
                    break
            time.sleep(0.5)
        mark("原生窗口出现（WindowsForms10.* 宿主）", hwnd is not None,
             f"标题={title!r} 类名={cls!r}")
        if hwnd is None:
            for h_, t_, c_ in windows_of_pid(proc.pid):
                print(f"      · 进程窗口：{t_!r} / {c_!r}")
            return 1
        mark("不是浏览器窗口（无 Chrome_WidgetWin 类）", not (cls or "").startswith("Chrome_"))

        time.sleep(1.5)                       # 等最大化动画收敛
        r, wa = rect_of(hwnd), workarea()
        w, hgt = r[2] - r[0], r[3] - r[1]
        zw = bool(u32.IsZoomed(hwnd))
        mark("启动即最大化（IsZoomed=true）", zw, f"rect={r} workarea={wa}")
        mark("窗口覆盖屏幕（≥ 工作区 95% 宽高）",
             w >= (wa[2] - wa[0]) * 0.95 and hgt >= (wa[3] - wa[1]) * 0.95,
             f"{w}x{hgt} vs {wa[2] - wa[0]}x{wa[3] - wa[1]}")

        # 3) 关窗 = 隐藏到托盘（后端继续）
        u32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        time.sleep(2.5)
        vis = bool(u32.IsWindowVisible(hwnd))
        alive = (http_json("/api/health") or {}).get("status") == "ok"
        mark("关窗后窗口隐藏", not vis)
        mark("关窗后后端仍在跑（隐藏到托盘）", alive)

        # 4) 唤起窗口
        r_show = http_json("/api/desktop/show", method="POST") or {}
        time.sleep(2.0)
        vis2 = bool(u32.IsWindowVisible(hwnd))
        mark("/api/desktop/show 唤起窗口", bool(r_show.get("window")) and vis2,
             f"api={r_show.get('window')} visible={vis2}")

        # 5) 退出 = 释放端口 + 进程退出
        t0 = time.time()
        r_quit = http_json("/api/desktop/quit", method="POST") or {}
        mark("/api/desktop/quit 受理", r_quit.get("status") == "ok")
        gone = False
        while time.time() - t0 < 60:
            if proc.poll() is not None and not port_open(args.port):
                gone = True
                break
            time.sleep(0.5)
        dt = time.time() - t0
        mark("退出后端口释放 + 进程结束", gone,
             f"{dt:.1f}s exit={proc.poll()} port_open={port_open(args.port)}")
    finally:
        if proc.poll() is None:
            print("    （清理）进程仍在，强制结束")
            proc.kill()

    bad = [n for n, ok, _ in results if not ok]
    print(f"\n结论: {'[PASS] 桌面壳冒烟全部通过' if not bad else f'[FAIL] {len(bad)} 项未过: {bad}'}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
