# -*- coding: utf-8 -*-
"""桌面版：在一个独立窗口（pywebview）里开管理界面。

    python -m agent_bridge desktop [--port 8765]

- 本机 127.0.0.1:端口 上已经有服务在跑（比如 start.cmd 起的）：直接连上它，关窗口不影响它。
- 没有：在本进程里起服务；关窗口时服务跟着退出。
- 浏览器照样能打开同一个地址（http://127.0.0.1:8765/），手机端也照旧。
- 只开一个窗口：再启动一次就把已有的窗口提到前面（藏在托盘里也一样）。
- Windows：托盘图标 + 右键菜单 + 通知（见 tray.py）；关窗口缺省藏到托盘，真退出用托盘菜单的「退出」或 --quit。
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

TITLE = "CC Msg"            # 测试实例（CCMSG_INSTANCE）会变成 "CC Msg TEST"，见 main()
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


def _single_instance(wait: float = 0) -> bool:
    """True = 我是第一个；False = 已经有一个开着（顺手把它的窗口提到前面）。wait：等前一个退出（重启用）。"""
    global _mutex
    if os.name == "nt":
        import ctypes
        from . import tray
        k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32
        t0 = time.time()
        while wait and tray.mutex_exists() and time.time() - t0 < wait:
            time.sleep(0.25)
        _mutex = k32.CreateMutexW(None, False, tray.names()["mutex"])
        if k32.GetLastError() == 183:          # ERROR_ALREADY_EXISTS
            if tray.post(tray.CMD_SHOW):       # 新版：托盘窗口收到后自己显示并拉到前面（我们先把前台权让给它）
                return False
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
        """只开 GitHub 上的页面（发布页、PR / issue、检查详情），别的不开。"""
        if isinstance(url, str) and url.startswith("https://github.com/"):
            webbrowser.open(url)
            return True
        return False


class App:
    """托盘用的窗口操作（tray.Tray 调这些；都可以从别的线程调）。"""

    def __init__(self, port: int, url: str, srv):
        self.port, self.url, self.srv = port, url, srv
        self.win = None
        self.tray = None
        self.quitting = False
        self.hidden = False
        self.hint_shown = False

    def front_hwnd(self):
        return _find_window(os.getpid()) if os.name == "nt" else None

    def visible(self) -> bool:
        return not self.hidden

    def show(self):
        if not self.win:
            return
        self.hidden = False
        try:
            self.win.show()
            self.win.restore()
        except Exception:
            pass
        if os.name == "nt":
            _bring_front()
        try:
            urllib.request.urlopen(urllib.request.Request(self.url + "api/tray/seen", data=b"{}", method="POST",
                                   headers={"X-Msgbus": "1", "Content-Type": "application/json"}), timeout=3).close()
        except Exception:
            pass

    def hide(self):
        if self.win:
            self.hidden = True
            self.win.hide()

    def open(self, target: dict):
        self.show()
        if self.win and target:
            try:
                self.win.evaluate_js("window.ccOpen && window.ccOpen(%s)" % json.dumps(target))
            except Exception as e:
                print("[desktop] 打开会话出错：", e)

    def browser(self):
        webbrowser.open(self.url)

    def check_update(self):
        from . import update
        try:
            req = urllib.request.Request(self.url + "api/update/check", data=b"{}", method="POST",
                                         headers={"X-Msgbus": "1", "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                st = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            self.tray and self.tray.notify("检查更新失败", str(e)[:200])
            return
        if st.get("newer"):
            self.open({"settings": True})                    # 窗口里的设置有「下载并安装」
        elif self.tray:
            self.tray.notify("已经是最新版本", f"CC Msg {update.__version__}" + (f"（GitHub 上最新 {st.get('latest')}）" if st.get("latest") else ""))

    def restart(self):
        import subprocess
        from . import paths
        args = paths.self_cmd("desktop", "--wait", "--port", str(self.port))
        if paths.FROZEN:
            args = [paths.EXE, "desktop", "--wait", "--port", str(self.port)]
        elif os.name == "nt":
            pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            if os.path.exists(pyw):
                args[0] = pyw
        flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0         # DETACHED_PROCESS | NEW_PROCESS_GROUP
        subprocess.Popen(args, close_fds=True, creationflags=flags, cwd=str(paths.PKG.parent))
        self.hard_quit()

    def quit(self):
        pending = 0
        try:
            st = self.tray.state if self.tray else {}
            pending = int(st.get("active") or 0)
        except Exception:
            pass
        if pending and os.name == "nt":
            import ctypes
            r = ctypes.windll.user32.MessageBoxW(None, f"还有 {pending} 个会话在干活。\n退出 CC Msg 不影响它们之间互发消息（钩子照常工作），"
                                                 "只是看不到界面、收不到通知。\n\n确定退出？", TITLE, 0x4 | 0x20 | 0x40000)   # YESNO | QUESTION | TOPMOST
            if r != 6:
                return
        self.hard_quit()

    def hard_quit(self):
        """真退出：去掉托盘图标、关窗口；webview.start() 返回后服务跟着停。"""
        if self.quitting:
            return
        self.quitting = True
        if self.tray:
            self.tray.remove()
        try:
            if self.win:
                self.win.destroy()
                return
        except Exception:
            pass
        os._exit(0)


def _bring_front():
    """把我们的窗口拉到前面：先正常 SetForegroundWindow；不行（Windows 前台锁）就借一下当前前台线程的输入队列。"""
    import ctypes
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    for _ in range(30):
        hwnd = _find_window(os.getpid())
        if hwnd:
            break
        time.sleep(0.05)
    else:
        return
    if u32.IsIconic(hwnd):
        u32.ShowWindow(hwnd, 9)                # SW_RESTORE
    if u32.SetForegroundWindow(hwnd) and u32.GetForegroundWindow() == hwnd:
        return
    fg = u32.GetForegroundWindow()
    t1 = u32.GetWindowThreadProcessId(fg, None) if fg else 0
    t2 = k32.GetCurrentThreadId()
    if t1 and t1 != t2 and u32.AttachThreadInput(t2, t1, True):
        try:
            u32.BringWindowToTop(hwnd)
            u32.SetForegroundWindow(hwnd)
        finally:
            u32.AttachThreadInput(t2, t1, False)


def main(argv=None) -> int:
    global TITLE
    ap = argparse.ArgumentParser(prog="agent_bridge desktop")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--debug", action="store_true", help="打开开发者工具")
    ap.add_argument("--quit", action="store_true", help="让正在运行（包括藏在托盘里）的 CC Msg 退出（安装程序用）")
    ap.add_argument("--autostart", action="store_true", help="开机自启时用（按设置决定藏不藏到托盘）")
    ap.add_argument("--minimized", action="store_true", help="启动时藏到托盘")
    ap.add_argument("--wait", action="store_true", help="等前一个退出再起（重启用）")
    ap.add_argument("--instance", default=None, help="实例名（测试版用，和真装的分开：互斥量、托盘、开机自启项都带上它）")
    a = ap.parse_args(argv)
    if a.instance is not None:
        os.environ["CCMSG_INSTANCE"] = a.instance
    _log_to_file()
    if os.name == "nt":
        from . import tray
        TITLE = tray.names()["title"]
        if a.quit:
            if not tray.mutex_exists():
                return 0
            tray.post(tray.CMD_QUIT)
            t0 = time.time()
            while tray.mutex_exists() and time.time() - t0 < 20:
                time.sleep(0.25)
            return 0 if not tray.mutex_exists() else 1
    elif a.quit:
        return 0
    if not _single_instance(20 if a.wait else 0):
        return 0
    if os.name == "nt":
        try:
            import ctypes
            from . import tray
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(tray.names()["aumid"])
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
    settings = _settings(url)
    app = App(a.port, url, srv)
    start_hidden = os.name == "nt" and (a.minimized or bool(settings.get("start_min")))
    win = webview.create_window(TITLE, url, js_api=Api(url), width=st.get("width", 1360), height=st.get("height", 880),
                                x=st.get("x"), y=st.get("y"), min_size=(720, 480), text_select=True, hidden=start_hidden)
    app.win, app.hidden = win, start_hidden

    def on_closing():
        _save_state(win)
        if os.name != "nt" or app.quitting or not app.tray or (app.tray and app.tray.ending):
            return True
        if not _settings(url).get("tray_close", True):
            if app.tray:
                app.tray.remove()
            return True
        threading.Thread(target=app.hide, daemon=True).start()       # 不在关窗口的回调里直接藏（GUI 线程）
        if not _settings(url).get("tray_hint_shown"):
            _set_setting(url, "tray_hint_shown", True)
            app.tray.notify(f"{TITLE} 仍在后台运行", "点托盘图标打开窗口；右键菜单里可以退出、改「关闭窗口时最小化到托盘」")
        return False
    win.events.closing += on_closing
    from . import update
    update.QUIT = app.hard_quit         # 装更新前：真退出（不是藏到托盘）→ 服务跟着退 → 安装程序接手
    if os.name == "nt":
        win.events.shown += lambda: threading.Thread(target=_win_icon, daemon=True).start()
        try:
            from . import tray
            app.tray = tray.Tray(app)
            app.tray.start()
            if start_hidden and not a.autostart:
                app.tray.notify(f"{TITLE} 在托盘里运行", "点托盘图标打开窗口")
        except Exception as e:
            print("[desktop] 托盘图标没能加上：", e)
            app.tray = None
            if start_hidden:
                app.hidden = False
                threading.Timer(1.0, win.show).start()
    storage = paths.HOME / "webview"
    storage.mkdir(parents=True, exist_ok=True)
    kw = {"private_mode": False, "storage_path": str(storage), "debug": a.debug}
    if os.name != "nt" and ICON_PNG.exists():
        kw["icon"] = str(ICON_PNG)
    webview.start(**kw)
    if app.tray:
        app.tray.remove()
    if srv:
        srv.shutdown()                  # 服务是我起的：窗口关了就一起退
    return 0


def _settings(url: str) -> dict:
    try:
        with urllib.request.urlopen(url + "api/settings", timeout=3) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return {}


def _set_setting(url: str, k: str, v) -> None:
    try:
        urllib.request.urlopen(urllib.request.Request(url + "api/settings", data=json.dumps({k: v}).encode("utf-8"), method="POST",
                                                      headers={"X-Msgbus": "1", "Content-Type": "application/json"}), timeout=3).close()
    except Exception:
        pass


def _fatal(msg: str) -> None:
    print("[desktop] " + msg)
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg, TITLE, 0x10)
        except Exception:
            pass
