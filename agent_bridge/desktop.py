# -*- coding: utf-8 -*-
"""桌面版：在一个独立窗口（pywebview）里开管理界面。

    python -m agent_bridge desktop [--port 8765]

- 本机 127.0.0.1:端口 上已经有服务在跑（比如 start.cmd 起的）：直接连上它，关窗口不影响它。
- 没有：在本进程里起服务；关窗口时服务跟着退出。
- 浏览器照样能打开同一个地址（http://127.0.0.1:8765/），手机端也照旧。
- 只开一个窗口：再启动一次就把已有的窗口提到前面。
- 窗口大小、位置记在 ~/.agent-bridge/desktop.json；界面的本地设置（主题等）存在 ~/.agent-bridge/webview/。
Windows 用 WebView2（Edge 内核，Win10 / 11 自带），Linux 用 GTK 或 Qt（见 README）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.request
import webbrowser

from . import paths

TITLE = "会话消息总线"
STATE = paths.HOME / "desktop.json"
LOG = paths.HOME / "desktop.log"
ICON_ICO = paths.PKG / "icon.ico"
ICON_PNG = paths.PKG / "icon.png"


def _log_to_file():
    """pythonw 没有控制台：输出写到 ~/.agent-bridge/desktop.log，出了错也有地方查。"""
    if sys.stdout is None or sys.stderr is None:
        f = open(LOG, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or f
        sys.stderr = sys.stderr or f


def ping(port: int, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=timeout) as r:
            return bool(json.loads(r.read().decode("utf-8")).get("ok"))
    except Exception:
        return False


# ---------- 只开一个 ----------

_mutex = None
PIDF = paths.HOME / "desktop.pid"


def _find_window(pid: int):
    """进程 pid 的、标题是 TITLE 的可见顶层窗口（按标题全局找会撞上别的程序里同名的窗口，比如资源管理器）。"""
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        p = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and u32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            u32.GetWindowTextW(hwnd, buf, 256)
            if buf.value == TITLE:
                found.append(hwnd)
                return False
        return True
    u32.EnumWindows(cb, 0)
    return found[0] if found else None


def _single_instance() -> bool:
    """True = 我是第一个；False = 已经有一个开着（顺手把它的窗口提到前面）。"""
    global _mutex
    if os.name == "nt":
        import ctypes
        k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32
        _mutex = k32.CreateMutexW(None, False, "Local\\AgentBridgeDesktop")
        if k32.GetLastError() == 183:          # ERROR_ALREADY_EXISTS
            try:
                hwnd = _find_window(int(PIDF.read_text().strip()))
            except Exception:
                hwnd = None
            if hwnd:
                u32.ShowWindow(hwnd, 9)        # SW_RESTORE
                u32.SetForegroundWindow(hwnd)
            return False
        PIDF.write_text(str(os.getpid()))
        return True
    try:
        import fcntl
        _mutex = open(paths.HOME / "desktop.lock", "w")
        fcntl.flock(_mutex, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


# ---------- 窗口位置 ----------

def _load_state() -> dict:
    try:
        d = json.loads(STATE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_state(win) -> None:
    try:
        d = {"width": int(win.width), "height": int(win.height), "x": int(win.x), "y": int(win.y)}
        if d["width"] >= 400 and d["height"] >= 300 and d["x"] > -10000 and d["y"] > -10000:
            STATE.write_text(json.dumps(d), encoding="utf-8")
    except Exception:
        pass


def _win_icon():
    """Windows：窗口和任务栏用我们的图标（pywebview 在 Windows 上不管图标，进程又是 pythonw）。"""
    if os.name != "nt" or not ICON_ICO.exists():
        return
    import ctypes
    u32 = ctypes.windll.user32
    for _ in range(50):
        hwnd = _find_window(os.getpid())
        if hwnd:
            break
        time.sleep(0.1)
    else:
        return
    u32.LoadImageW.restype = ctypes.c_void_p
    u32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_void_p]
    for which, size in ((1, 32), (0, 16)):         # ICON_BIG / ICON_SMALL
        h = u32.LoadImageW(None, str(ICON_ICO), 1, size, size, 0x10)   # IMAGE_ICON, LR_LOADFROMFILE
        if h:
            u32.SendMessageW(hwnd, 0x80, which, ctypes.c_void_p(h))     # WM_SETICON


class Api:
    """界面里 window.pywebview.api.* 能调的（⋯ 菜单「在浏览器中打开」）。"""

    def __init__(self, url: str):
        self._url = url

    def open_browser(self, path: str = "/"):
        webbrowser.open(self._url.rstrip("/") + (path or "/"))
        return True

    def open_url(self, url: str):
        """只开 GitHub 上的发布页（更新说明里的链接），别的不开。"""
        if isinstance(url, str) and url.startswith("https://github.com/"):
            webbrowser.open(url)
            return True
        return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="agent_bridge desktop")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--debug", action="store_true", help="打开开发者工具")
    a = ap.parse_args(argv)
    _log_to_file()
    if not _single_instance():
        return 0
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("CCMessage.AgentBridge")
        except Exception:
            pass
    url = f"http://127.0.0.1:{a.port}/"
    srv = None
    if not ping(a.port):
        from . import server
        srv = server.start(a.port)
        if not srv:                     # 端口被别的程序占着、又不是我们的服务
            for _ in range(10):         # 也可能是另一个刚起来的服务，等一下
                if ping(a.port):
                    break
                time.sleep(0.5)
            else:
                _fatal(f"端口 {a.port} 被别的程序占着，界面起不来。")
                return 1
        print(f"[desktop] 服务已在本进程里启动：{url}")
    else:
        print(f"[desktop] 连上已在运行的服务：{url}")
    try:
        import webview
    except ImportError:
        _fatal("没装 pywebview：pip install pywebview（Linux 还要 GTK 或 Qt，见 README）。先用浏览器打开 " + url)
        webbrowser.open(url)
        if srv:                         # 服务是我起的：没有窗口也让它跑着，Ctrl+C / 结束进程退出
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                pass
        return 1
    st = _load_state()
    win = webview.create_window(TITLE, url, js_api=Api(url), width=st.get("width", 1360), height=st.get("height", 880),
                                x=st.get("x"), y=st.get("y"), min_size=(720, 480), text_select=True)
    win.events.closing += lambda: _save_state(win)
    from . import update
    update.QUIT = win.destroy           # 装更新前：关窗口 → 服务跟着退 → 安装程序接手
    if os.name == "nt":
        win.events.shown += lambda: threading.Thread(target=_win_icon, daemon=True).start()
    storage = paths.HOME / "webview"
    storage.mkdir(parents=True, exist_ok=True)
    kw = {"private_mode": False, "storage_path": str(storage), "debug": a.debug}
    if os.name != "nt" and ICON_PNG.exists():
        kw["icon"] = str(ICON_PNG)
    webview.start(**kw)
    if srv:
        srv.shutdown()                  # 服务是我起的：窗口关了就一起退
    return 0


def _fatal(msg: str) -> None:
    print("[desktop] " + msg)
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg, TITLE, 0x10)
        except Exception:
            pass
