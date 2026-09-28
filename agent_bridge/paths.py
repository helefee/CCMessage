# -*- coding: utf-8 -*-
"""路径与外部程序探测：不写死任何一台机器的目录。"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent

# 每个用户自己的数据（登记的项目、设置、配对过的手机、没归属项目的导出）；可用 AGENT_BRIDGE_HOME 改
HOME = Path(os.environ.get("AGENT_BRIDGE_HOME") or Path.home() / ".agent-bridge")
HOME.mkdir(parents=True, exist_ok=True)

CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


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
