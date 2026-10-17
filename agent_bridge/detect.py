# -*- coding: utf-8 -*-
"""Claude Code / Codex 在这台电脑上「有没有」：进程 + 数据目录 + 设置里的手动指定，给侧栏开关、提示和「诊断」用。

0.7.8 起：只要数据目录在（或最近有会话文件在写）就算「有」，不再因为没匹配到进程就把那一栏收起。
进程只是加分项，认得：原生安装的 claude.exe（~/.local/bin）、npm 装的（node.exe … @anthropic-ai/claude-code/cli.js，
或包里自带的 claude.exe）、Claude 桌面端（AnthropicClaude / 商店版 WindowsApps\\Claude_…）和它自带的 claude-code、
VS Code / Cursor 扩展里的、WSL（只能看到 WSL 在跑，再去查 WSL 里的 ~/.claude）。
Codex 同理（codex.exe、npm 的 @openai/codex、桌面端 OpenAI.Codex、VS Code 的 openai.chatgpt 扩展）。
名字和路径都不分大小写；读别的进程命令行失败（权限不够 / 管理员进程）只计数，不报错。不起 PowerShell、不弹控制台窗口。

测试：AGENT_BRIDGE_FAKE_PROCS 指向 JSON [{"name","exe","cmd"}] 代替真实进程；AGENT_BRIDGE_FAKE_WSL 是逗号分隔的发行版名
（配 AGENT_BRIDGE_WSL_ROOT 当 \\\\wsl.localhost）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

NOWIN = 0x08000000 if os.name == "nt" else 0
AGENTS = ("claude", "codex")
LABEL = {"claude": "Claude Code", "codex": "Codex"}
KIND_LABEL = {"cli": "命令行（原生安装）", "npm": "命令行（npm / node）", "desktop": "桌面端", "desktop-cli": "桌面端自带的命令行",
              "vscode": "编辑器扩展（VS Code / Cursor 等）", "wsl": "WSL（只能看到 WSL 在跑）"}
RECENT = 24 * 3600                 # 这么久内有会话文件在写 = 「最近用过」
_INTEREST = ("claude", "codex", "chatgpt", "node", "bun", "deno", "wsl", "wslhost", "wslrelay", "vmmemwsl")


def _norm(s: str) -> str:
    return (s or "").replace("\\", "/").lower()


def classify(name: str, exe: str = "", cmd: str = "") -> list[tuple[str, str]]:
    """一个进程 → [(agent, kind)]。不分大小写，/ 和 \\ 都认。纯函数，方便测试。"""
    n = _norm(name).rsplit("/", 1)[-1]
    n = n[:-4] if n.endswith(".exe") else n
    p, c = _norm(exe), _norm(cmd)
    pc = p + " " + c
    out: list[tuple[str, str]] = []
    if n == "claude":
        if "/extensions/anthropic.claude-code" in pc:
            out.append(("claude", "vscode"))
        elif "@anthropic-ai/claude-code" in p:
            out.append(("claude", "npm"))                 # npm 装的、包里自带原生 claude.exe
        elif "/claude/claude-code/" in p or "/claude-code-vm/" in p:
            out.append(("claude", "desktop-cli"))         # 桌面端自带的（%APPDATA%\Claude\claude-code\<版本>\claude.exe）
        elif "/windowsapps/claude_" in p or "anthropicclaude" in p or "/claude.app/contents/macos/claude" in p:
            out.append(("claude", "desktop"))
        else:
            out.append(("claude", "cli"))
    elif n in ("node", "bun", "deno"):
        if "@anthropic-ai/claude-code" in c or "claude-code/cli.js" in c or "claude-code/cli.mjs" in c or "/extensions/anthropic.claude-code" in c:
            out.append(("claude", "vscode" if "/extensions/anthropic.claude-code" in c else "npm"))
        if "@openai/codex" in c or ("/extensions/openai.chatgpt" in c and "codex" in c):
            out.append(("codex", "vscode" if "/extensions/openai.chatgpt" in c else "npm"))
    elif n == "codex":
        if "/extensions/openai.chatgpt" in pc:
            out.append(("codex", "vscode"))
        elif "@openai/codex" in p:
            out.append(("codex", "npm"))
        elif "/windowsapps/openai.codex" in p:
            out.append(("codex", "desktop"))              # 商店版 Codex 桌面端（WindowsApps\OpenAI.Codex_…）
        elif "openai.codex" in p or "/openai/codex/bin/" in p or "codex.app/contents/" in p:
            out.append(("codex", "desktop" if "app-server" in c else "desktop-cli"))
        else:
            out.append(("codex", "cli"))
    elif n in ("chatgpt", "codex") and "openai.codex" in p:
        out.append(("codex", "desktop"))
    elif n in ("wsl", "wslhost", "wslrelay", "vmmemwsl"):
        out += [("claude", "wsl"), ("codex", "wsl")]
    return out


# ---------- 进程列表 ----------

def _win_cmdline(h) -> str | None:
    """NtQueryInformationProcess(ProcessCommandLineInformation=60)：只要 PROCESS_QUERY_LIMITED_INFORMATION（Win 8.1+）。"""
    import ctypes
    from ctypes import wintypes
    nt = ctypes.windll.ntdll
    nt.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
    nt.NtQueryInformationProcess.restype = ctypes.c_long
    need = wintypes.ULONG(0)
    nt.NtQueryInformationProcess(h, 60, None, 0, ctypes.byref(need))
    if not need.value or need.value > 1 << 20:
        return None
    buf = ctypes.create_string_buffer(need.value)
    if nt.NtQueryInformationProcess(h, 60, buf, need.value, ctypes.byref(need)) != 0:
        return None

    class US(ctypes.Structure):
        _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", ctypes.c_void_p)]
    us = US.from_buffer(buf)
    return ctypes.wstring_at(us.Buffer, us.Length // 2) if us.Buffer and us.Length else ""


def _win_procs(stats: dict) -> list[dict]:
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32

    class PE(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    snap = k32.CreateToolhelp32Snapshot(2, 0)
    out: list[dict] = []
    if not snap or snap == wintypes.HANDLE(-1).value:
        stats["errors"].append("进程快照失败")
        return out
    try:
        e = PE(); e.dwSize = ctypes.sizeof(PE)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            stats["total"] += 1
            name = e.szExeFile
            base = name.lower()[:-4] if name.lower().endswith(".exe") else name.lower()
            if base in _INTEREST:
                path, cmd = "", ""
                h = k32.OpenProcess(0x1000, False, e.th32ProcessID)          # PROCESS_QUERY_LIMITED_INFORMATION
                if h:
                    try:
                        b = ctypes.create_unicode_buffer(1024); size = wintypes.DWORD(1024)
                        if k32.QueryFullProcessImageNameW(h, 0, b, ctypes.byref(size)):
                            path = b.value
                        if base in ("node", "bun", "deno", "claude", "codex"):
                            try:
                                cmd = _win_cmdline(h)
                                if cmd is None:
                                    stats["cmd_denied"] += 1; cmd = ""
                            except Exception:
                                stats["cmd_denied"] += 1; cmd = ""
                    finally:
                        k32.CloseHandle(h)
                else:
                    stats["denied"] += 1
                out.append({"pid": e.th32ProcessID, "name": name, "exe": path, "cmd": cmd})
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return out


def _posix_procs(stats: dict) -> list[dict]:
    out = []
    if sys.platform.startswith("linux") and Path("/proc").is_dir():
        for d in Path("/proc").iterdir():
            if not d.name.isdigit():
                continue
            try:
                raw = (d / "cmdline").read_bytes()
            except OSError:
                stats["denied"] += 1
                continue
            stats["total"] += 1
            cmd = raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
            try:
                exe = os.readlink(d / "exe")
            except OSError:
                exe = raw.split(b"\0")[0].decode("utf-8", "replace")
            out.append({"pid": int(d.name), "name": Path(exe or cmd.split(" ")[0]).name, "exe": exe, "cmd": cmd})
        return out
    try:
        r = subprocess.run(["ps", "-axo", "pid=,comm=,command="], capture_output=True, timeout=10)
    except Exception as e:
        stats["errors"].append(f"ps 失败：{type(e).__name__}")
        return out
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        bits = line.strip().split(None, 2)
        if len(bits) >= 2:
            stats["total"] += 1
            out.append({"pid": bits[0], "name": Path(bits[1]).name, "exe": bits[1], "cmd": bits[2] if len(bits) > 2 else ""})
    return out


def processes(stats: dict | None = None) -> list[dict]:
    stats = stats if stats is not None else {"total": 0, "denied": 0, "cmd_denied": 0, "errors": []}
    fake = os.environ.get("AGENT_BRIDGE_FAKE_PROCS")
    if fake:
        try:
            ps = json.loads(Path(fake).read_text(encoding="utf-8"))
        except Exception:
            ps = []
        stats["total"] = len(ps); stats["fake"] = True
        stats["denied"] = sum(1 for p in ps if p.get("denied"))
        return [dict({"pid": i + 1, "exe": "", "cmd": ""}, **p) for i, p in enumerate(ps) if not p.get("denied")]
    try:
        return _win_procs(stats) if os.name == "nt" else _posix_procs(stats)
    except Exception as e:
        stats["errors"].append(f"读进程失败：{type(e).__name__}")
        return []


# ---------- 数据目录 ----------

def _newest(globs: list[tuple[Path, str]], cap: int = 4000) -> float:
    best, n = 0.0, 0
    for base, pat in globs:
        try:
            for f in base.glob(pat):
                n += 1
                try:
                    best = max(best, f.stat().st_mtime)
                except OSError:
                    pass
                if n >= cap:
                    return best
        except OSError:
            pass
    return best


def _settings() -> dict:
    from . import paths
    try:
        return json.loads((paths.HOME / "settings.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _wsl_distros() -> list[str]:
    f = os.environ.get("AGENT_BRIDGE_FAKE_WSL")
    if f is not None:
        return [x for x in f.split(",") if x]
    if os.name != "nt":
        return []
    try:
        r = subprocess.run(["wsl.exe", "-l", "--running", "-q"], capture_output=True, timeout=4, creationflags=NOWIN)
        raw = r.stdout
        txt = raw.decode("utf-16-le", "replace") if b"\0" in raw else raw.decode("utf-8", "replace")
        return [x.strip().strip("\0") for x in txt.splitlines() if x.strip().strip("\0")] if r.returncode == 0 else []
    except Exception:
        return []


def msix_claude_dirs() -> list[Path]:
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    try:
        return [pk / "LocalCache" / "Roaming" / "Claude" for pk in sorted((local / "Packages").glob("Claude_*"))]
    except OSError:
        return []


CODEX_STORE_SUBS = ("LocalCache/Roaming/.codex", "LocalCache/Local/.codex", "LocalCache/Roaming/Codex", "LocalCache/Local/Codex", "LocalState/.codex")


def codex_store_homes(only_real: bool = True) -> list[Path]:
    """商店版 Codex（%LOCALAPPDATA%\\Packages\\OpenAI.Codex_*）的虚拟化目录里像 CODEX_HOME 的（有 sessions/ 或 config.toml）。
    2026-10-17 实测那台机器上只有 Logs 和网页缓存，会话照旧在 ~/.codex。"""
    out = []
    for pk in codex_store_installed():
        for sub in CODEX_STORE_SUBS:
            h = pk / sub
            if not only_real or (h / "sessions").is_dir() or (h / "config.toml").is_file():
                out.append(h)
    return out


def codex_store_installed() -> list[Path]:
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    try:
        return sorted((local / "Packages").glob("OpenAI.Codex_*"))
    except OSError:
        return []


def candidates(agent: str, settings: dict | None = None, wsl: list[str] | None = None) -> list[dict]:
    """要查的数据目录：[{label, path, kind: config|app, sessions: [(base, glob)]}]。"""
    s = settings if settings is not None else _settings()
    home, env = Path.home(), os.environ
    appdata = Path(env.get("APPDATA") or home / "AppData" / "Roaming")
    local = Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local")
    out: list[dict] = []

    def add(label, p, kind):
        p = Path(os.path.expanduser(str(p)))
        if any(str(x["path"]).lower() == str(p).lower() for x in out):
            return
        if agent == "claude":
            ses = [(p / "projects", "*/*.jsonl")] if kind == "config" else [(p / "claude-code-sessions", "*/*/*.json")]
        else:
            ses = [(p / "sessions", "*/*/*/*.jsonl"), (p / "archived_sessions", "*.jsonl")] if kind == "config" else []
        out.append({"label": label, "path": p, "kind": kind, "sessions": ses})

    win = sys.platform == "win32" or bool(env.get("APPDATA"))
    if agent == "claude":
        if s.get("claude_dir"):
            add("设置里指定的", s["claude_dir"], "config")
        if env.get("CLAUDE_CONFIG_DIR"):
            add("环境变量 CLAUDE_CONFIG_DIR", env["CLAUDE_CONFIG_DIR"], "config")
        add("~/.claude", home / ".claude", "config")
        if win:
            add("%APPDATA%\\Claude（桌面端）", appdata / "Claude", "app")
            for d in msix_claude_dirs():
                add("商店版桌面端（MSIX 虚拟化目录）", d, "app")
        elif sys.platform == "darwin":
            add("桌面端", home / "Library" / "Application Support" / "Claude", "app")
        else:
            add("桌面端", Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "Claude", "app")
    else:
        if s.get("codex_dir"):
            add("设置里指定的", s["codex_dir"], "config")
        if env.get("CODEX_HOME"):
            add("环境变量 CODEX_HOME", env["CODEX_HOME"], "config")
        add("~/.codex", home / ".codex", "config")
        if win:
            for h in codex_store_homes():
                add("商店版 Codex 的虚拟化目录", h, "config")
    for d in wsl or []:
        try:
            root = Path(env["AGENT_BRIDGE_WSL_ROOT"]) if env.get("AGENT_BRIDGE_WSL_ROOT") else Path("\\\\wsl.localhost")
            for u in sorted((root / d / "home").glob("*")):
                add(f"WSL {d}", u / (".claude" if agent == "claude" else ".codex"), "config")
        except OSError:
            pass
    return out


def _scan_dir(c: dict) -> dict:
    p = c["path"]
    try:
        ex = p.is_dir()
    except OSError:
        ex = False
    r = {"label": c["label"], "path": str(p), "exists": ex, "recent": 0.0}
    if ex:
        r["recent"] = _newest(c["sessions"])
    return r


# ---------- 汇总 ----------

def _mask(p: str) -> str:
    h = str(Path.home())
    return ("~" + p[len(h):]) if p and p.lower().startswith(h.lower()) else p


def _ago(t: float) -> str:
    d = max(0, time.time() - t)
    return "刚刚" if d < 90 else f"{int(d // 60)} 分钟前" if d < 3600 else f"{int(d // 3600)} 小时前" if d < 86400 else f"{int(d // 86400)} 天前"


def report() -> dict:
    s = _settings()
    stats = {"total": 0, "denied": 0, "cmd_denied": 0, "errors": []}
    ps = processes(stats)
    hits = {a: [] for a in AGENTS}
    for p in ps:
        for a, k in classify(p.get("name", ""), p.get("exe", ""), p.get("cmd", "")):
            hits[a].append({"pid": p.get("pid"), "name": p.get("name"), "kind": k, "exe": _mask(p.get("exe") or "")})
    wsl_on = any(h["kind"] == "wsl" for h in hits["claude"])
    distros = _wsl_distros() if wsl_on else []
    now = time.time()
    out = {"ts": now, "platform": sys.platform, "procs": stats, "wsl": wsl_on, "wsl_distros": distros}
    for a in AGENTS:
        dirs = [_scan_dir(c) for c in candidates(a, s, distros)]
        real = [h for h in hits[a] if h["kind"] != "wsl"]
        force = s.get(f"{a}_show") == "always"
        data = [d for d in dirs if d["exists"]]
        if a == "claude" and (Path.home() / ".claude.json").is_file():
            data.append({"label": "~/.claude.json", "path": str(Path.home() / ".claude.json"), "exists": True})
            dirs.append({"label": "~/.claude.json（登录信息）", "path": str(Path.home() / ".claude.json"), "exists": True, "recent": 0.0})
        recent = max([d.get("recent") or 0.0 for d in dirs] + [0.0])
        avail = bool(force or real or data)
        state = "running" if real else "recent" if recent and now - recent < RECENT else "data" if data else "forced" if force else "none"
        kinds = sorted({h["kind"] for h in real})
        if state == "running":
            why = f"{LABEL[a]} 在运行：" + "、".join(KIND_LABEL.get(k, k) for k in kinds)
        elif state == "recent":
            why = f"没看到 {LABEL[a]} 进程，但 {_ago(recent)} 还有会话文件在写，按「在用」算"
        elif state == "data":
            why = f"没看到 {LABEL[a]} 进程；数据在（{_mask(data[0]['path'])}），照样显示"
        elif state == "forced":
            why = f"没检测到 {LABEL[a]}，设置里选了「总是显示」"
        else:
            why = f"没检测到 {LABEL[a]}：没有匹配的进程，查过的目录都不在（{'、'.join(_mask(d['path']) for d in dirs)}）"
        if wsl_on and state != "running":
            why += "；WSL 在跑，里面的进程看不到" + ("，已查 WSL 里的目录" if distros else "")
        extra = []
        if a == "claude":
            try:
                from . import claude_sync
                ad = claude_sync.app_dirs_brief()
                extra = [f"桌面端{x['name']}：用 {_mask(x['path'])}（{x['why']}）" for x in ad["parts"]]
                if ad["msix_running"] is not None:
                    extra.append("桌面端在跑：" + ("商店版（WindowsApps）" if ad["msix_running"] else "安装版"))
                if ad["override"]:
                    extra.append("桌面端目录：设置里手动指定 " + _mask(ad["override"]))
            except Exception as e:
                extra = [f"桌面端目录判断出错：{type(e).__name__}"]
        else:
            st_ = codex_store_installed()
            if st_:
                homes = codex_store_homes()
                extra.append(f"商店版 Codex 已安装（{st_[0].name}）；虚拟化目录里" + ("有会话 / 配置：" + "、".join(_mask(str(h)) for h in homes) if homes else "没有会话 / 配置，用 ~/.codex"))
        out[a] = {"available": avail, "notes": extra, "running": bool(real), "state": state, "kinds": kinds, "why": why,
                  "recent": recent or None, "force": force, "procs": real[:12],
                  "dirs": [dict(d, path=_mask(d["path"])) for d in dirs]}
    return out


def text(r: dict | None = None) -> str:
    """「诊断」复制出去的那段：版本、平台、查了哪些进程 / 目录、结论。家目录写成 ~；不含 key、账号、命令行参数。"""
    from . import __version__
    r = r or report()
    st = r["procs"]
    L = [f"CC Msg {__version__} 诊断 · {time.strftime('%Y-%m-%d %H:%M:%S')} · {r['platform']}",
         f"进程：共 {st['total']} 个，打不开 {st['denied']} 个，读不到命令行 {st['cmd_denied']} 个" + ("（测试用假进程）" if st.get("fake") else "")
         + (f"；错误：{'；'.join(st['errors'])}" if st["errors"] else "")]
    try:
        from . import apps
        ds = apps.status()
        L.append("桌面端在不在跑（会话管理 / 等它退出再改 / 自动同步用）：" + " · ".join(f"{LABEL[a]} {'开着' if ds.get(a) else '没开'}" for a in AGENTS)
                 + f"（{apps.LAST.get('via')}{'；' + apps.LAST['error'] if apps.LAST.get('error') else ''}）")
    except Exception as e:
        L.append(f"桌面端在不在跑：查不了（{type(e).__name__}）")
    if r.get("wsl"):
        L.append("WSL：在跑" + ("，发行版 " + "、".join(r["wsl_distros"]) if r.get("wsl_distros") else "（没列出在跑的发行版）"))
    for a in AGENTS:
        x = r[a]
        L.append(f"\n[{LABEL[a]}] {'显示' if x['available'] else '不显示'} · {x['state']} · {x['why']}")
        for p in x["procs"]:
            L.append(f"  进程 {p['name']} (pid {p['pid']}) → {KIND_LABEL.get(p['kind'], p['kind'])}  {p['exe']}")
        if not x["procs"]:
            L.append("  进程：没有匹配的")
        for d in x["dirs"]:
            L.append(f"  {'✓' if d['exists'] else '✗'} {d['label']}: {d['path']}" + (f"  最近会话 {_ago(d['recent'])}" if d.get("recent") else ""))
        for n_ in x.get("notes") or []:
            L.append("  " + n_)
        L.append(f"  设置：{'总是显示' if x['force'] else '按检测'}")
    return "\n".join(L)


_C = {"t": 0.0, "v": None}


def cached(max_age: float = 10) -> dict:
    now = time.time()
    if _C["v"] is None or now - _C["t"] > max_age or os.environ.get("AGENT_BRIDGE_FAKE_PROCS"):
        try:
            _C["v"] = report()
        except Exception as e:                                   # 检测本身出错：宁可显示也不藏
            _C["v"] = {"ts": now, "error": str(e), **{a: {"available": True, "running": False, "state": "error", "kinds": [],
                       "why": f"检测出错（{type(e).__name__}），先照样显示", "recent": None, "force": False} for a in AGENTS}}
        _C["t"] = now
    return _C["v"]


def forget() -> None:
    _C["v"] = None


def brief() -> dict:
    """/api/apps/status 用：{"claude": bool, "codex": bool, ts, detail}（bool = 该不该显示那一栏）。"""
    r = cached()
    return {"claude": r["claude"]["available"], "codex": r["codex"]["available"], "ts": r["ts"],
            "detail": {a: {k: r[a].get(k) for k in ("running", "state", "why", "kinds", "recent", "force")} for a in AGENTS}}


def pick_folder(start: str = "") -> dict:
    """Windows：系统的选文件夹框（PowerShell + WinForms，置顶；不弹控制台）。别的系统：让用户手填。"""
    if os.name != "nt":
        return {"ok": False, "manual": True, "error": "这台电脑上请直接填路径"}
    start = start if start and Path(os.path.expanduser(start)).is_dir() else str(Path.home())
    ps = ("Add-Type -AssemblyName System.Windows.Forms;"
          "$f=New-Object System.Windows.Forms.Form;$f.TopMost=$true;"
          "$d=New-Object System.Windows.Forms.FolderBrowserDialog;$d.Description='选 Claude / Codex 的数据目录';"
          "$d.SelectedPath=$env:CCMSG_PICK_START;$d.ShowNewFolderButton=$false;"
          "[Console]::OutputEncoding=[Text.Encoding]::UTF8;if($d.ShowDialog($f) -eq 'OK'){$d.SelectedPath}")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps], capture_output=True, timeout=600, stdin=subprocess.DEVNULL,
                           creationflags=NOWIN, env=dict(os.environ, CCMSG_PICK_START=start))
        p = r.stdout.decode("utf-8", "replace").strip()
        return {"ok": True, "path": p} if p else {"ok": False, "cancel": True}
    except Exception as e:
        return {"ok": False, "manual": True, "error": f"打不开选文件夹框（{type(e).__name__}），请直接填路径"}
