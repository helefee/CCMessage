# -*- coding: utf-8 -*-
"""路径与外部程序探测：不写死任何一台机器的目录。"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent

# Windows 安装版：Nuitka 编译成 CCMsg.exe（0.2.x 叫 AgentBridge.exe，程序目录里没有 .py 源码），总线是同目录 bus\bus.exe
FROZEN = "__compiled__" in globals()

def _exe_path() -> str:
    """编译版自己的 exe 完整路径（Nuitka 里 sys.executable 是程序目录下一个并不存在的 python.exe，还可能是 8.3 短路径）。"""
    p = sys.argv[0]
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        b = ctypes.create_unicode_buffer(32768)
        if k.GetModuleFileNameW(None, b, 32768):
            p = b.value
        if k.GetLongPathNameW(p, b, 32768):
            p = b.value
    return os.path.abspath(p)


EXE = _exe_path() if FROZEN else sys.executable      # 安装版：CCMsg.exe 的完整路径
APP_DIR = Path(EXE).parent if FROZEN else None
BUS_EXE = (APP_DIR / "bus" / "bus.exe") if FROZEN else None


def self_cmd(*args: str) -> list[str]:
    """再起一个自己跑子命令：安装版是 CCMsg.exe <子命令>，源码版是 python -m agent_bridge <子命令>。"""
    if FROZEN:
        return [EXE, *args]
    return [sys.executable, "-m", "agent_bridge", *args]

# 每个用户自己的数据（登记的项目、设置、配对过的手机、没归属项目的导出）；可用 AGENT_BRIDGE_HOME 改
HOME = Path(os.environ.get("AGENT_BRIDGE_HOME") or Path.home() / ".agent-bridge")
HOME.mkdir(parents=True, exist_ok=True)

def _set_dir(k: str) -> str | None:
    """设置里手动指定的 Claude / Codex 数据目录（设置 → 「Claude / Codex 检测」；改了重启 CC Msg 后全面生效）。环境变量优先。"""
    try:
        import json
        v = json.loads((HOME / "settings.json").read_text(encoding="utf-8")).get(k) or ""
        return v if v and Path(v).is_dir() else None
    except Exception:
        return None


CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR") or _set_dir("claude_dir") or Path.home() / ".claude")
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or _set_dir("codex_dir") or Path.home() / ".codex")


def codex_exe() -> str | None:
    """Codex 命令行：环境变量 → Windows 桌面端自带的 → PATH 里的 → macOS 桌面端自带的。"""
    p = os.environ.get("CODEX_CLI_PATH")
    if p and Path(p).exists():
        return p
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "OpenAI" / "Codex" / "bin"
        c = sorted(base.glob("*/codex.exe"), key=lambda x: x.stat().st_mtime, reverse=True)
        if c:
            return str(c[0])
    w = shutil.which("codex")
    if w:
        return w
    for mac in ("/Applications/Codex.app/Contents/Resources/codex", "/Applications/Codex.app/Contents/MacOS/codex"):
        if Path(mac).exists():
            return mac
    return None
