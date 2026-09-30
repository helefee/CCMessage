# -*- coding: utf-8 -*-
"""命令行入口。

    python -m agent_bridge                     起管理服务（网页界面，默认 http://127.0.0.1:8765）
    python -m agent_bridge serve [--port N] [--no-browser]
    python -m agent_bridge install <项目目录>    不开界面，直接把总线装进项目（钩子合并、git 排除、自检）
    python -m agent_bridge uninstall <项目目录> [--purge]
    python -m agent_bridge selftest [<项目目录>] 不给目录就在临时目录里装一份跑完整自检
    python -m agent_bridge doctor              检查这台电脑能不能用（Python、git、Codex、Claude、二维码库）
    python -m agent_bridge export <Codex 线程>  Codex 线程导出成交接文件（给 Claude 接手）
    python -m agent_bridge codex-list          最近的 Codex 线程
    python -m agent_bridge claude-sync [--dry-run] [--list] [--to 账号/组织]
    python -m agent_bridge codex-sync [--dry-run] [--days 14] [--cwd 项目] [--to 账号/组织]   把 Codex 对话同步进 Claude 桌面端列表
                                               切换账号后看不到的 Claude 桌面端会话，补齐到当前账号的列表里
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

if sys.version_info < (3, 11):
    sys.exit("agent-bridge 需要 Python 3.11 或更新版本")


def _print(obj):
    sys.stdout.reconfigure(encoding="utf-8")
    if isinstance(obj, dict) and "log" in obj:
        print("\n".join(obj["log"]))
        st = obj.get("selftest")
        if st:
            print("\n自检：" + ("通过" if st["ok"] else "有问题"))
            print("\n".join(st["steps"]))
    else:
        print(json.dumps(obj, ensure_ascii=False, indent=1) if not isinstance(obj, str) else obj)


def doctor() -> int:
    from . import paths
    ok = True

    def line(good, text, hint=""):
        nonlocal ok
        ok &= bool(good) or hint.startswith("（可选）")
        print(("✓ " if good else "✗ ") + text + ("" if good or not hint else "  —— " + hint))
    sys.stdout.reconfigure(encoding="utf-8")
    line(True, f"Python {sys.version.split()[0]}（{sys.executable}）")
    line(shutil.which("git"), "git", "装 git 才能自动写 .git/info/exclude、交接文件里查分支")
    cx = paths.codex_exe()
    line(cx, f"Codex 命令行：{cx or '没找到'}", "装 Codex 桌面端，或设环境变量 CODEX_CLI_PATH")
    line(paths.CODEX_HOME.exists(), f"Codex 数据目录：{paths.CODEX_HOME}", "Codex 还没用过")
    line(paths.CLAUDE_HOME.exists(), f"Claude Code 数据目录：{paths.CLAUDE_HOME}", "Claude Code 还没用过")
    try:
        import qrcode  # noqa: F401
        line(True, "二维码库 qrcode（手机扫码配对用）")
    except Exception:
        line(False, "二维码库 qrcode", "（可选）pip install qrcode —— 没有就只显示配对链接")
    line(sys.platform == "win32", f"系统：{sys.platform}", "（可选）信件动画只在 Windows 上有；其余功能各平台都能用")
    print(f"\n数据目录：{paths.HOME}")
    print("结论：" + ("可以用" if ok else "有必需项没满足，见上面 ✗"))
    return 0 if ok else 1


def selftest(target: str | None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    if target:
        from . import server
        r = server.selftest(server.norm_root(target))
        print("\n".join(r["steps"]))
        return 0 if r["ok"] else 1
    tmp = Path(tempfile.mkdtemp(prefix="agent-bridge-selftest-"))
    try:
        subprocess.run(["git", "init", "-q", str(tmp)], capture_output=True)
        # 用临时数据目录，别把测试项目登记进真实列表（必须在加载 server 之前设）
        os.environ["AGENT_BRIDGE_HOME"] = str(tmp / "_home")
        from . import server
        r = server.install(tmp)
        print("\n".join(r["log"]))
        print("\n自检：")
        print("\n".join(r["selftest"]["steps"]))
        cfg = json.loads((tmp / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
        cx = json.loads((tmp / ".codex" / "hooks.json").read_text(encoding="utf-8"))
        print(f"\nClaude 钩子事件：{sorted(cfg['hooks'])}；Codex 钩子事件：{sorted(cx['hooks'])}")
        u = server.uninstall(tmp, purge=True)
        print("卸载：" + "；".join(u["log"]))
        return 0 if r["selftest"]["ok"] else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv and not argv[0].startswith("-") else "serve"
    rest = argv[1:] if argv and not argv[0].startswith("-") else argv
    if cmd == "serve":
        from . import server
        return server.main(rest)
    if cmd == "install":
        from . import server
        if not rest:
            sys.exit("用法：python -m agent_bridge install <项目目录>")
        _print(server.install(server.norm_root(rest[0])))
        return 0
    if cmd == "uninstall":
        from . import server
        if not rest:
            sys.exit("用法：python -m agent_bridge uninstall <项目目录> [--purge]")
        _print(server.uninstall(server.norm_root(rest[0]), "--purge" in rest))
        return 0
    if cmd == "selftest":
        return selftest(rest[0] if rest else None)
    if cmd == "doctor":
        return doctor()
    if cmd == "claude-sync":
        from . import claude_sync
        return claude_sync.main(rest)
    if cmd == "codex-sync":
        from . import codex_sync
        return codex_sync.main(rest)
    if cmd in ("export", "codex-list", "to-claude"):
        from .paths import PKG
        sub = {"codex-list": "list"}.get(cmd, cmd)
        return subprocess.call([sys.executable, str(PKG / "codex_bridge.py"), sub, *rest])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main() or 0)
