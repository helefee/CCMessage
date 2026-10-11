# -*- coding: utf-8 -*-
"""检查更新 / 自动更新（GitHub Releases）。

- 查 https://api.github.com/repos/helefee/CCMessage/releases/latest（仓库是公开的，不用令牌；
  真要查私有仓库可设环境变量 AGENT_BRIDGE_GITHUB_TOKEN，程序里不存任何密钥）；
  AGENT_BRIDGE_UPDATE_URL 可以换成别的地址（测试用本机假发布）。
- 启动后自动查（最多 6 小时一次，设置里可关）；设置里「检查更新」随时手动查。
- Windows 安装版：新版的 AgentBridge-Setup-<版本>.exe 下到临时目录，用同一发布里的 .sha256 校验，
  然后静默运行安装程序（按用户安装、不用管理员），本程序退出，装完自动重新打开。
- 源码版 / Linux：只提示有新版本，用 git pull 更新。
断网、GitHub 限流都只记一条错误，不影响别的功能。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from . import __version__, paths

REPO = "helefee/CCMessage"
API = os.environ.get("AGENT_BRIDGE_UPDATE_URL") or f"https://api.github.com/repos/{REPO}/releases/latest"
STATE = paths.HOME / "update.json"
EVERY = 6 * 3600
UA = f"agent-bridge/{__version__}"

_lock = threading.Lock()
DL = {"state": "idle", "done": 0, "total": 0, "error": None, "file": None, "version": None}
QUIT = None          # 桌面版放进来的「退出窗口」回调；装更新前调用


def can_install() -> bool:
    return paths.FROZEN and sys.platform == "win32"


def vtuple(v: str) -> tuple:
    m = re.match(r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-.]?([0-9A-Za-z.]+))?", v or "")
    if not m:
        return (0, 0, 0, 0)
    pre = m.group(4)
    # 1.2.0 > 1.2.0-rc1：正式版排在预发布后面
    return (int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0), 0 if pre else 1)


def _load() -> dict:
    try:
        d = json.loads(STATE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(d: dict) -> None:
    try:
        STATE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def _get(url: str, timeout: float = 10, accept: str = "application/vnd.github+json"):
    h = {"User-Agent": UA, "Accept": accept}
    tok = os.environ.get("AGENT_BRIDGE_GITHUB_TOKEN")
    if tok and url.startswith("https://api.github.com/"):
        h["Authorization"] = f"Bearer {tok}"
    return urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout)


def _parse(rel: dict) -> dict:
    tag = rel.get("tag_name") or ""
    assets = rel.get("assets") or []
    setup = next((a for a in assets if re.match(r"(?i)^AgentBridge-Setup-.*\.exe$", a.get("name", ""))), None)
    sha = None
    if setup:
        sha = next((a for a in assets if a.get("name", "").lower() == (setup["name"] + ".sha256").lower()), None)
    return {"latest": tag.lstrip("vV"), "tag": tag, "name": rel.get("name") or tag,
            "notes": (rel.get("body") or "")[:6000], "page": rel.get("html_url"),
            "published": rel.get("published_at"),
            "setup": setup and {"name": setup["name"], "url": setup["browser_download_url"], "size": setup.get("size")},
            "sha_url": sha and sha["browser_download_url"]}


def status() -> dict:
    """界面要的：当前版本、上次查到的结果、下载进度。"""
    d = _load()
    r = d.get("result") or {}
    newer = bool(r.get("latest")) and vtuple(r["latest"]) > vtuple(__version__)
    return {"current": __version__, "mode": "installer" if can_install() else "source",
            "checked": d.get("checked"), "error": d.get("error"), "newer": newer,
            **({k: r.get(k) for k in ("latest", "tag", "name", "notes", "page", "published")} if r else {}),
            "has_setup": bool(r.get("setup") and r.get("sha_url")), "download": dict(DL)}


def check(force: bool = False) -> dict:
    d = _load()
    if not force and d.get("checked") and time.time() - d["checked"] < EVERY:
        return status()
    with _lock:
        try:
            with _get(API) as resp:
                rel = json.loads(resp.read().decode("utf-8"))
            d.update(result=_parse(rel), error=None)
        except Exception as e:
            code = getattr(e, "code", None)
            msg = {404: "没找到发布（还没发过版本，或仓库不公开——私有仓库要设 AGENT_BRIDGE_GITHUB_TOKEN）",
                   403: "GitHub 暂时限流，过一会儿再查"}.get(code) or f"查不到更新（{type(e).__name__}: {e}）"
            d["error"] = msg
        d["checked"] = time.time()
        _save(d)
    return status()


def auto_check_later(enabled, delay: float = 15) -> None:
    """服务启动后过一会儿在后台查一次（遵守 6 小时节流、设置里的开关）。"""
    def run():
        time.sleep(delay)
        try:
            if enabled():
                check()
        except Exception:
            pass
    threading.Thread(target=run, daemon=True, name="update-check").start()


# ---------- 下载 / 安装（只有 Windows 安装版） ----------

def _download(r: dict) -> None:
    try:
        tmp = Path(tempfile.gettempdir()) / "AgentBridge-update"
        tmp.mkdir(exist_ok=True)
        with _get(r["sha_url"], 20, "application/octet-stream") as resp:
            want = resp.read().decode("utf-8", "replace").split()[0].strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", want):
            raise ValueError("发布里的 .sha256 文件格式不对")
        f = tmp / r["setup"]["name"]
        part = f.with_suffix(".part")
        h = hashlib.sha256()
        with _get(r["setup"]["url"], 30, "application/octet-stream") as resp, open(part, "wb") as out:
            DL["total"] = int(resp.headers.get("Content-Length") or r["setup"].get("size") or 0)
            while True:
                b = resp.read(256 * 1024)
                if not b:
                    break
                out.write(b)
                h.update(b)
                DL["done"] += len(b)
        DL["state"] = "verifying"
        if h.hexdigest() != want:
            part.unlink(missing_ok=True)
            raise ValueError("下载的安装包校验不对（SHA256 不符），已删掉，没有安装")
        part.replace(f)
        DL.update(state="ready", file=str(f))
    except Exception as e:
        DL.update(state="error", error=str(e))


def download() -> dict:
    if not can_install():
        raise ValueError("只有 Windows 安装版能自动更新；源码版请 git pull")
    r = (_load().get("result") or {})
    if not (r.get("setup") and r.get("sha_url")) or vtuple(r.get("latest", "")) <= vtuple(__version__):
        raise ValueError("没有可安装的新版本（先检查更新）")
    if DL["state"] in ("downloading", "verifying"):
        return status()
    DL.update(state="downloading", done=0, total=0, error=None, file=None, version=r["latest"])
    threading.Thread(target=_download, args=(r,), daemon=True, name="update-dl").start()
    return status()


def apply(relaunch_args: list[str] | None = None) -> dict:
    """静默运行下好的安装程序，然后让本程序退出；安装程序装完会重新打开 AgentBridge。"""
    if not can_install() or DL["state"] != "ready" or not DL["file"]:
        raise ValueError("安装包还没下好")
    args = [DL["file"], "/VERYSILENT", "/SUPPRESSMSGBOXES", "/CLOSEAPPLICATIONS", "/NORESTART", "/RELAUNCH=1"]
    if relaunch_args:
        args.append("/RELAUNCHARGS=" + " ".join(relaunch_args))
    base = 0x00000008 | 0x00000200            # DETACHED_PROCESS | NEW_PROCESS_GROUP
    try:   # 脱离桌面端的作业对象：桌面端退出时会收拾它起的子进程，安装程序不能跟着被收拾掉
        subprocess.Popen(args, close_fds=True, creationflags=base | 0x01000000)   # BREAKAWAY_FROM_JOB
    except OSError:
        subprocess.Popen(args, close_fds=True, creationflags=base)
    DL["state"] = "installing"

    def bye():
        time.sleep(0.8)            # 先让这次请求的回应发出去
        try:
            if QUIT:
                QUIT()
                return
        except Exception:
            pass
        os._exit(0)
    threading.Thread(target=bye, daemon=True).start()
    return {"ok": True}
