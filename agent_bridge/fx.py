#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""信件动画：在桌面上让一封信从 Claude 窗口飞到 Codex 窗口（或反过来）。

    python fx.py --dir c2x --kind task --label "帮派系统线"    # Claude → Codex
    python fx.py --dir x2c --kind result --label "719d75"      # Codex → Claude

由 server.py 的信箱监视线程在有 Claude↔Codex 消息时调起，一次一封，放完自己退出。
只在 Windows 上工作（找窗口用 Win32 API）；找不到某一边的窗口（没开 / 最小化）就从屏幕边上飞进 / 飞出。
透明、置顶、不抢焦点、鼠标可穿透，不影响正在用的窗口。只用标准库（tkinter + ctypes）。
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as W
import math
import sys
import time
import tkinter as tk

KEY = "#ff00fe"            # 透明色键：画布上这个颜色的像素是透明的
COLORS = {"c2x": ("#d97757", "Claude", "Codex"), "x2c": ("#10a37f", "Codex", "Claude"),
          "u2c": ("#6b5bd2", "你", "Claude"), "u2x": ("#6b5bd2", "你", "Codex")}
KIND_TEXT = {"task": "任务", "result": "结果", "msg": "消息"}


# ---------- 找窗口 ----------

def _dpi_aware():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _exe_of(pid: int) -> str:
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, pid)
    if not h:
        return ""
    buf = ctypes.create_unicode_buffer(1024)
    n = W.DWORD(1024)
    k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n))
    k.CloseHandle(h)
    return buf.value.lower()


def find_windows() -> dict:
    """{'claude': (l,t,r,b) 或 None, 'codex': …}：各取可见、未最小化、面积最大的那个窗口。"""
    u = ctypes.windll.user32
    best: dict = {"claude": None, "codex": None}

    @ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    def cb(h, _):
        if not u.IsWindowVisible(h) or u.IsIconic(h) or u.GetWindowTextLengthW(h) == 0:
            return True
        pid = W.DWORD()
        u.GetWindowThreadProcessId(h, ctypes.byref(pid))
        exe = _exe_of(pid.value)
        who = None
        if exe.endswith("\\claude.exe") and ("\\claude_" in exe or "anthropicclaude" in exe or "\\claude\\" in exe):
            who = "claude"          # Claude 桌面端（商店版 / 安装版）；Claude Code 命令行没有窗口
        elif "openai.codex" in exe and exe.endswith("\\chatgpt.exe"):
            who = "codex"           # Codex 桌面端
        if who:
            r = W.RECT()
            u.GetWindowRect(h, ctypes.byref(r))
            area = (r.right - r.left) * (r.bottom - r.top)
            if area > 200 * 200 and (best[who] is None or area > best[who][1]):
                best[who] = ((r.left, r.top, r.right, r.bottom), area)
        return True

    u.EnumWindows(cb, 0)
    return {k: (v[0] if v else None) for k, v in best.items()}


def screen_rect() -> tuple:
    u = ctypes.windll.user32
    x, y = u.GetSystemMetrics(76), u.GetSystemMetrics(77)          # 虚拟屏幕（多显示器）
    return x, y, x + u.GetSystemMetrics(78), y + u.GetSystemMetrics(79)


# ---------- 画信封 ----------

def draw_envelope(c: tk.Canvas, w: int, h: int, color: str, kind: str, label: str, open_: float = 0.0):
    c.delete("all")
    ew, eh = 106, 72
    x0, y0 = (w - ew) // 2, 10
    x1, y1 = x0 + ew, y0 + eh
    # 投影
    c.create_polygon(x0 + 4, y0 + 6, x1 + 4, y0 + 6, x1 + 4, y1 + 6, x0 + 4, y1 + 6, fill="#3a3a3a", outline="")
    # 信封主体
    c.create_polygon(x0, y0, x1, y0, x1, y1, x0, y1, fill="#fffdf7", outline="#b9b0a0", width=2)
    # 下半两道折线
    c.create_line(x0, y1, (x0 + x1) / 2, y0 + eh * 0.55, x1, y1, fill="#d6cdbd", width=2)
    # 封舌：open_=0 合上（尖朝下），1 打开（翻到上面）
    fy = y0 + eh * 0.58 * (1 - 2 * open_)
    c.create_polygon(x0, y0, x1, y0, (x0 + x1) / 2, fy, fill="#f3ede1", outline="#b9b0a0", width=2)
    # 火漆印
    if open_ < 0.5:
        cx, cy, r = (x0 + x1) / 2, y0 + eh * 0.52, eh * 0.16
        c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=color, outline="")
        c.create_text(cx, cy, text=KIND_TEXT.get(kind, "信")[0], fill="white", font=("Microsoft YaHei UI", max(8, int(r * 0.9)), "bold"))
    # 标签
    c.create_text(w / 2 + 1, y1 + 17, text=label, fill="#202020", font=("Microsoft YaHei UI", 10, "bold"))
    c.create_text(w / 2, y1 + 16, text=label, fill=color, font=("Microsoft YaHei UI", 10, "bold"))


def ease(t: float) -> float:
    return 3 * t * t - 2 * t * t * t        # 平滑起止


def run(direction: str, kind: str, label: str, duration: float):
    _dpi_aware()
    color, src_name, dst_name = COLORS[direction]
    wins = find_windows()
    src_app = {"c2x": "claude", "x2c": "codex"}.get(direction)
    dst_app = {"c2x": "codex", "x2c": "claude", "u2c": "claude", "u2x": "codex"}[direction]
    src, dst = (wins[src_app] if src_app else None), wins[dst_app]
    sl, st, sr, sb = screen_rect()

    def anchor(rect, fallback_x):
        if rect:
            l, t, r, b = rect
            return (l + r) / 2, t + (b - t) * 0.42
        return fallback_x, (st + sb) / 2
    # 某一边没开：Claude 在左、Codex 在右，从对应的屏幕边飞进 / 飞出
    left_x, right_x = sl + 40, sr - 40
    if src_app:
        p0 = anchor(src, left_x if direction == "c2x" else right_x)
    else:                                          # 你发的：从屏幕底部正中飞上来
        p0 = ((sl + sr) / 2, sb - 90)
    p2 = anchor(dst, right_x if dst_app == "codex" else left_x)
    if abs(p0[0] - p2[0]) < 60 and abs(p0[1] - p2[1]) < 60:          # 两个窗口叠在一起：往上绕一圈
        p2 = (p2[0] + 240, p2[1])
    dist = math.hypot(p2[0] - p0[0], p2[1] - p0[1])
    p1 = ((p0[0] + p2[0]) / 2, min(p0[1], p2[1]) - max(120, dist * 0.35))   # 弧线顶点

    W_, H_ = 320, 140
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-transparentcolor", KEY)
    root.attributes("-alpha", 0.0)
    root.configure(bg=KEY)
    c = tk.Canvas(root, width=W_, height=H_, bg=KEY, highlightthickness=0)
    c.pack()
    text = f"{src_name} → {dst_name} · {KIND_TEXT.get(kind, '消息')}" + (f" · {label}" if label else "")
    root.update_idletasks()
    # 鼠标穿透 + 不抢焦点 + 不进任务栏
    try:
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        GWL_EXSTYLE, WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW = -20, 0x80000, 0x20, 0x8000000, 0x80
        st_ = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, st_ | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
    except Exception:
        pass

    def place(x, y):
        root.geometry(f"{W_}x{H_}+{int(x - W_ / 2)}+{int(y - H_ / 3)}")

    start = time.perf_counter()
    fade_in, fly, land = 0.18, duration, 0.9
    draw_envelope(c, W_, H_, color, kind, text)
    place(*p0)
    while True:
        t = time.perf_counter() - start
        if t < fade_in:                                   # 在发件窗口上浮现
            root.attributes("-alpha", t / fade_in)
            place(p0[0], p0[1] - 20 * (t / fade_in))
        elif t < fade_in + fly:                           # 沿弧线飞
            u_ = ease((t - fade_in) / fly)
            x = (1 - u_) ** 2 * p0[0] + 2 * (1 - u_) * u_ * p1[0] + u_ ** 2 * p2[0]
            y = (1 - u_) ** 2 * (p0[1] - 20) + 2 * (1 - u_) * u_ * p1[1] + u_ ** 2 * p2[1]
            y += math.sin(u_ * math.pi * 3) * 6           # 一点点飘
            root.attributes("-alpha", 1.0)
            place(x, y)
        elif t < fade_in + fly + land:                    # 落地：拆信 + 「已送达」+ 淡出
            v = (t - fade_in - fly) / land
            draw_envelope(c, W_, H_, color, kind, "✓ 已送达" if v > 0.25 else text, open_=min(1.0, v * 2.5))
            place(p2[0], p2[1] - 14 * math.sin(min(1.0, v * 2) * math.pi))
            root.attributes("-alpha", 1.0 if v < 0.6 else max(0.0, 1 - (v - 0.6) / 0.4))
        else:
            break
        root.update()
        time.sleep(1 / 90)
    root.destroy()


def main():
    ap = argparse.ArgumentParser(description="Claude ↔ Codex 信件动画")
    ap.add_argument("--dir", choices=["c2x", "x2c", "u2c", "u2x"], default="c2x",
                    help="c2x：Claude→Codex；x2c：Codex→Claude；u2c / u2x：你→Claude / Codex")
    ap.add_argument("--kind", default="msg", choices=["msg", "task", "result"])
    ap.add_argument("--label", default="")
    ap.add_argument("--duration", type=float, default=1.15, help="飞行秒数")
    ap.add_argument("--probe", action="store_true", help="只打印找到的窗口位置")
    a = ap.parse_args()
    if sys.platform != "win32":
        return
    if a.probe:
        _dpi_aware()
        print(find_windows())
        return
    run(a.dir, a.kind, a.label[:16], a.duration)


if __name__ == "__main__":
    main()
