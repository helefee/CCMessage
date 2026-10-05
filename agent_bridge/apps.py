# -*- coding: utf-8 -*-
"""桌面端（Claude / Codex）在不在跑、怎么重新打开、以及「等它退出再改」的延后执行。

为什么要等：两个桌面端都把会话登记放在内存里，退出时写回磁盘。开着的时候改它们的数据文件，
退出那一下就被盖回去（2026-10-05 实测：Claude 同步在 08:04:11 写完，08:04:55 退出时 10 个开着的会话被写回旧版）。
所以所有改桌面端数据的操作都走 run_or_defer：没开就立刻做；开着就排进队列，起一个独立的后台进程
等它完全退出、再做、做完把它重新打开。

    python -m agent_bridge after-quit <claude|codex>     后台等待进程（run_or_defer 自己起，不用手动跑）
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import paths

QUEUE = paths.HOME / "pending-jobs.json"
LOG = paths.HOME / "after-quit.log"
NOWIN = 0x08000000 if os.name == "nt" else 0
NAMES = {"claude": "Claude 桌面端", "codex": "Codex 桌面端"}


# ---------- 进程 ----------

def _procs() -> list[dict]:
    """[{pid, name, exe, cmd}]，只列可能相关的几种。"""
    if os.name == "nt":
        ps = ("Get-CimInstance Win32_Process -Filter \"Name='Claude.exe' OR Name='claude.exe' OR Name='ChatGPT.exe' "
              "OR Name='Codex.exe' OR Name='codex.exe'\" | Select-Object ProcessId,Name,ExecutablePath,CommandLine "
              "| ConvertTo-Json -Compress")
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=30,
                               creationflags=NOWIN)
            raw = json.loads(r.stdout.decode("utf-8", "replace") or "[]")
        except Exception:
            return []
        raw = raw if isinstance(raw, list) else [raw]
        return [{"pid": p.get("ProcessId"), "name": p.get("Name") or "", "exe": p.get("ExecutablePath") or "",
                 "cmd": p.get("CommandLine") or ""} for p in raw]
    try:
        r = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, timeout=10)
    except Exception:
        return []
    out = []
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        pid, _, cmd = line.strip().partition(" ")
        out.append({"pid": pid, "name": Path(cmd.split(" ")[0]).name, "exe": cmd.split(" ")[0], "cmd": cmd})
    return out


def _is_app(kind: str, p: dict) -> bool:
    exe = p["exe"].lower().replace("/", "\\")
    if kind == "claude":
        # 桌面端：商店版在 WindowsApps\Claude_…，安装版在 AnthropicClaude；Claude Code 命令行（…\claude-code\…）不算
        if os.name != "nt":
            return "/claude.app/contents/macos/claude" in p["exe"].lower()
        return p["name"].lower() == "claude.exe" and "claude-code" not in exe and (
            "\\windowsapps\\claude_" in exe or "anthropicclaude" in exe)
    if kind == "codex":
        if os.name != "nt":
            return "codex.app/contents/macos" in p["exe"].lower() or "app-server" in p["cmd"]
        return ("openai.codex" in exe and p["name"].lower() in ("chatgpt.exe", "codex.exe")) or (
            p["name"].lower() == "codex.exe" and "app-server" in p["cmd"])
    return False


def running(kind: str) -> list[dict]:
    fake = os.environ.get("AGENT_BRIDGE_FAKE_APP")       # 测试用：这个文件在 = 桌面端「开着」
    if fake:
        return [{"pid": 0, "name": "fake", "exe": "", "cmd": ""}] if Path(fake).exists() else []
    return [p for p in _procs() if _is_app(kind, p)]


def status() -> dict:
    if os.environ.get("AGENT_BRIDGE_FAKE_APP"):
        return {k: bool(running(k)) for k in NAMES}
    ps = _procs()
    return {k: any(_is_app(k, p) for p in ps) for k in NAMES}


# ---------- 重新打开 ----------

def launch_info(kind: str, procs: list[dict] | None = None) -> dict | None:
    """在它还开着时记下怎么重新打开：商店版用 AppsFolder 的 AppID，安装版用 exe 路径。"""
    procs = procs if procs is not None else running(kind)
    exe = next((p["exe"] for p in procs if p["exe"] and not p["name"].lower() == "codex.exe"), None) or \
        next((p["exe"] for p in procs if p["exe"]), None)
    if not exe:
        return None
    low = exe.lower().replace("/", "\\")
    if os.name == "nt" and "\\windowsapps\\" in low:
        pkg = Path(exe).parts[[x.lower() for x in Path(exe).parts].index("windowsapps") + 1]
        fam = pkg.split("_")[0] + "_" + pkg.split("__")[-1] if "__" in pkg else None
        if fam:
            try:
                r = subprocess.run(["powershell", "-NoProfile", "-Command",
                                    f"(Get-StartApps | Where-Object {{ $_.AppID -like '{fam}!*' }} | Select-Object -First 1).AppID"],
                                   capture_output=True, timeout=30, creationflags=NOWIN)
                aid = r.stdout.decode("utf-8", "replace").strip()
                if aid:
                    return {"aumid": aid}
            except Exception:
                pass
    if sys.platform == "darwin" and ".app/" in exe:
        return {"app": exe[: exe.index(".app/") + 4]}
    return {"exe": exe}


def relaunch(info: dict | None) -> bool:
    if not info:
        return False
    try:
        if info.get("aumid"):
            os.startfile(f"shell:AppsFolder\\{info['aumid']}")  # type: ignore[attr-defined]
        elif info.get("app"):
            subprocess.Popen(["open", info["app"]])
        else:
            subprocess.Popen([info["exe"]], creationflags=0x00000008 if os.name == "nt" else 0)
        return True
    except Exception:
        return False


# ---------- 队列 ----------

def _log(msg: str) -> None:
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%m-%d %H:%M:%S} {msg}\n")


def _load_q() -> list[dict]:
    try:
        return json.loads(QUEUE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_q(q: list[dict]) -> None:
    tmp = QUEUE.with_suffix(".tmp")
    tmp.write_text(json.dumps(q, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, QUEUE)


def pending() -> list[dict]:
    """排队中和最近 20 条做完的。"""
    return _load_q()[-40:]


def _ops():
    """op 名 → 函数(**args) -> dict。放在函数里导入，免得循环引用。"""
    from . import claude_sync, codex_sync, codex_unify, session_mgr
    return {
        "claude-sync": lambda **a: claude_sync.run(a.get("target"), only=a.get("only")),
        "codex-sync": lambda **a: codex_sync.run(a.get("days", 14), a.get("root"), only=a.get("only")),
        "claude-restore": lambda **a: claude_sync.restore(a["backup"]),
        "codex-unify": lambda **a: codex_unify.run(**a),
        "codex-restore": lambda **a: codex_unify.restore(a["ledger"]),
        "sessions-batch": lambda **a: session_mgr.batch(**a),
        "trash-restore": lambda **a: session_mgr.trash_restore(a["batch"]),
    }


def _watcher_alive(kind: str) -> bool:
    lock = paths.HOME / f"after-quit-{kind}.pid"
    try:
        pid = int(lock.read_text())
    except Exception:
        return False
    if os.name == "nt":
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, creationflags=NOWIN)
        return str(pid).encode() in r.stdout
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _spawn_watcher(kind: str) -> None:
    if _watcher_alive(kind):
        return
    args = [sys.executable, "-m", "agent_bridge", "after-quit", kind]
    cwd = str(paths.PKG.parent)
    if os.name == "nt":
        # 独立进程组 + 脱离父进程的作业对象：桌面端退出时会收拾它起的子进程，等待进程不能跟着被收拾掉
        base = 0x00000008 | 0x00000200 | NOWIN          # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW
        try:
            subprocess.Popen(args, cwd=cwd, creationflags=base | 0x01000000, close_fds=True)   # BREAKAWAY_FROM_JOB
        except OSError:
            subprocess.Popen(args, cwd=cwd, creationflags=base, close_fds=True)
    else:
        subprocess.Popen(args, cwd=cwd, start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_or_defer(kind: str, op: str, args: dict, title: str) -> dict:
    """kind 对应的桌面端没开就立刻做；开着就排队，等它退出后由后台进程做，做完重新打开它。"""
    procs = running(kind)
    if not procs:
        r = _ops()[op](**args)
        return dict(r if isinstance(r, dict) else {"result": r}, deferred=False)
    q = _load_q()
    job = {"id": uuid.uuid4().hex[:8], "kind": kind, "op": op, "args": args, "title": title,
           "state": "waiting", "queued": time.time(), "relaunch": launch_info(kind, procs)}
    q.append(job)
    _save_q(q)
    _spawn_watcher(kind)
    _log(f"排队 {job['id']} {title}（等 {NAMES[kind]} 退出）")
    return {"ok": True, "deferred": True, "job": job["id"], "app": NAMES[kind],
            "hint": f"{NAMES[kind]} 正开着，直接改会在它退出时被盖回去。已排队：请完全退出 {NAMES[kind]}（托盘里也退），"
                    "后台会等它退干净再做，做完自动重新打开。"}


def cancel(job_id: str) -> bool:
    q = _load_q()
    hit = False
    for j in q:
        if j["id"] == job_id and j["state"] == "waiting":
            j["state"], hit = "cancelled", True
    _save_q(q)
    return hit


def watch(kind: str, max_wait: float = 6 * 3600) -> int:
    """后台等待进程本体：等 kind 退出 → 依次做排队的活 → 重新打开。"""
    lock = paths.HOME / f"after-quit-{kind}.pid"
    lock.write_text(str(os.getpid()))
    try:
        _log(f"开始等 {NAMES[kind]} 退出")
        t0 = time.time()
        while running(kind):
            if time.time() - t0 > max_wait:
                _log("等太久，放弃（队列留着，下次排队时再起）")
                return 1
            if not any(j["kind"] == kind and j["state"] == "waiting" for j in _load_q()):
                _log("队列里没有要做的了（被取消），退出")
                return 0
            time.sleep(2)
        time.sleep(5)       # 退出收尾写盘有个尾巴，再等几秒
        q, ops, relaunch_info = _load_q(), _ops(), None
        for j in q:
            if j["kind"] != kind or j["state"] != "waiting":
                continue
            if running(kind):   # 中途又被打开了：剩下的留到下次
                _log("中途又打开了，剩下的不做")
                break
            try:
                r = ops[j["op"]](**j["args"])
                j["state"], j["result"] = "done", _brief(r)
                _log(f"做完 {j['id']} {j['title']}：{j['result']}")
            except Exception as e:
                j["state"], j["result"] = "failed", f"{type(e).__name__}: {e}"
                _log(f"失败 {j['id']} {j['title']}：{j['result']}")
            j["finished"] = time.time()
            relaunch_info = relaunch_info or j.get("relaunch")
            _save_q(q)
        _save_q(q[-60:])
        if relaunch_info and not running(kind):
            _log(f"重新打开 {NAMES[kind]}：" + ("成功" if relaunch(relaunch_info) else "失败，请手动打开"))
        return 0
    finally:
        try:
            lock.unlink()
        except Exception:
            pass


def _brief(r) -> str:
    if not isinstance(r, dict):
        return str(r)[:200]
    keys = ("done", "updated", "restored", "removed", "moved", "threads", "files", "error")
    s = "，".join(f"{k}={r[k]}" for k in keys if k in r and not isinstance(r[k], (list, dict)))
    return s or ("ok" if r.get("ok", True) else "失败")
