# -*- coding: utf-8 -*-
"""API Key 存放：Windows 凭据管理器（CredWriteW / CredReadW，按当前用户加密保存）。

Claude 通过 settings.json 的 apiKeyHelper、Codex 通过 [model_providers.<id>.auth] 去跑 `CCMsg.exe key get <名字>` 取 key，
所以 key 不落在任何配置文件里。网页 / API 只拿得到打码后的样子（sk-…abcd）。
非 Windows（或设了 AGENT_BRIDGE_KEYSTORE=file，测试用）退回 ~/.agent-bridge/keys.json（权限 600），界面会标出来。
"""
from __future__ import annotations

import json
import os
import re
import sys

from . import paths

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,47}$")


def _prefix() -> str:
    try:
        from .tray import instance
        i = instance()
    except Exception:
        i = ""
    return "CCMsg" + ("-" + i if i else "") + "/key/"


def backend() -> str:
    if os.environ.get("AGENT_BRIDGE_KEYSTORE") == "file" or sys.platform != "win32":
        return "file"
    return "wincred"


def mask(v: str | None) -> str:
    if not v:
        return ""
    v = str(v)
    if len(v) <= 8:
        return "****"
    head = v[:3] if not v.startswith("sk-") else "sk-"
    return f"{head}...{v[-4:]}"


def check_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not NAME_RE.match(name):
        raise ValueError("key 名字只能用小写字母、数字、点、横线、下划线（最多 48 个字）")
    return name


# ---------- 文件后备（测试 / 非 Windows） ----------
def _fpath():
    return paths.HOME / "keys.json"


def _fload() -> dict:
    try:
        return json.loads(_fpath().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _fsave(d: dict):
    p = _fpath()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, p)


# ---------- Windows 凭据管理器 ----------
def _win():
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                    ("CredentialBlob", ctypes.POINTER(ctypes.c_char)), ("Persist", wintypes.DWORD),
                    ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                    ("UserName", wintypes.LPWSTR)]
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    P = ctypes.POINTER(CREDENTIAL)
    adv.CredWriteW.argtypes = [P, wintypes.DWORD]
    adv.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(P)]
    adv.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    adv.CredEnumerateW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.POINTER(P))]
    adv.CredFree.argtypes = [ctypes.c_void_p]
    return ctypes, CREDENTIAL, adv, P


def put(name: str, secret: str, comment: str = ""):
    name = check_name(name)
    secret = (secret or "").strip()
    if not secret or len(secret) > 2000 or any(c in secret for c in "\r\n\0"):
        raise ValueError("key 是空的或格式不对")
    if backend() == "file":
        d = _fload()
        d[name] = {"v": secret, "c": comment}
        return _fsave(d)
    ctypes, CREDENTIAL, adv, P = _win()
    blob = secret.encode("utf-8")
    buf = ctypes.create_string_buffer(blob, len(blob))
    c = CREDENTIAL()
    c.Type = 1                                   # CRED_TYPE_GENERIC
    c.TargetName = _prefix() + name
    c.Comment = (comment or "CC Msg")[:200]
    c.CredentialBlobSize = len(blob)
    c.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))
    c.Persist = 2                                # CRED_PERSIST_LOCAL_MACHINE：只在这台电脑、这个用户
    c.UserName = "ccmsg"
    if not adv.CredWriteW(ctypes.byref(c), 0):
        raise OSError(f"写入凭据管理器失败（{ctypes.get_last_error()}）")


def get(name: str) -> str | None:
    name = check_name(name)
    if backend() == "file":
        return (_fload().get(name) or {}).get("v")
    ctypes, CREDENTIAL, adv, P = _win()
    pc = P()
    if not adv.CredReadW(_prefix() + name, 1, 0, ctypes.byref(pc)):
        return None
    try:
        c = pc.contents
        return ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize).decode("utf-8")
    finally:
        adv.CredFree(pc)


def delete(name: str) -> bool:
    name = check_name(name)
    if backend() == "file":
        d = _fload()
        ok = d.pop(name, None) is not None
        _fsave(d)
        return ok
    ctypes, CREDENTIAL, adv, P = _win()
    return bool(adv.CredDeleteW(_prefix() + name, 1, 0))


def names() -> list[dict]:
    """[{name, masked, comment}]，不含明文。"""
    out = []
    if backend() == "file":
        for k, v in sorted(_fload().items()):
            out.append({"name": k, "masked": mask(v.get("v")), "comment": v.get("c", "")})
        return out
    ctypes, CREDENTIAL, adv, P = _win()
    from ctypes import wintypes
    n = wintypes.DWORD()
    arr = ctypes.POINTER(P)()
    if not adv.CredEnumerateW(_prefix() + "*", 0, ctypes.byref(n), ctypes.byref(arr)):
        return out
    try:
        for i in range(n.value):
            c = arr[i].contents
            v = ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize).decode("utf-8", "replace")
            out.append({"name": c.TargetName[len(_prefix()):], "masked": mask(v), "comment": c.Comment or ""})
    finally:
        adv.CredFree(arr)
    return sorted(out, key=lambda x: x["name"])


def helper_cmd(name: str) -> list[str]:
    """Claude / Codex 取 key 时要跑的命令（安装版是 CCMsg.exe key get NAME）。"""
    name = check_name(name)
    if paths.FROZEN:
        return paths.self_cmd("key", "get", name)
    return [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "keyhelper.py"), "key", "get", name]


def helper_selftest(name: str) -> None:
    """像 Claude / Codex 那样在别的目录里真跑一次取 key 命令，输出必须和存的一致；不一致就抛错（错误里不带 key）。"""
    import hmac
    import subprocess
    import tempfile
    want = get(name)
    if not want:
        raise RuntimeError(f"凭据管理器里没有 {name}")
    kw = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
    try:
        r = subprocess.run(helper_cmd(name), cwd=tempfile.gettempdir(), stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=30, **kw)
    except Exception as e:
        raise RuntimeError(f"取 key 命令跑不起来：{type(e).__name__}，没改配置") from None
    got = r.stdout.decode("utf-8", "replace").strip()
    if r.returncode != 0 or not hmac.compare_digest(got.encode(), want.strip().encode()):
        raise RuntimeError(f"取 key 命令试跑结果不对（退出码 {r.returncode}），没改配置")


def helper_line(name: str) -> str:
    """apiKeyHelper 用的一行命令：每段都加双引号（cmd 和 bash 都认）。"""
    return " ".join('"' + a.replace('"', '') + '"' if (" " in a or "\\" in a) else a for a in helper_cmd(name))


def main(argv: list[str]) -> int:
    """CLI：key get NAME（只给 Claude / Codex 调用，把 key 打到 stdout）；key list（只打码）。"""
    if len(argv) >= 2 and argv[0] == "get":
        try:
            v = get(argv[1])
        except Exception as e:
            print(f"ccmsg key: {e}", file=sys.stderr)
            return 2
        if not v:
            print(f"ccmsg key: 凭据管理器里没有 {argv[1]}（在 CC Msg →「模型与 API」里存一个）", file=sys.stderr)
            return 1
        sys.stdout.write(v)
        sys.stdout.flush()
        return 0
    if argv[:1] == ["list"]:
        for k in names():
            print(k["name"], k["masked"])
        return 0
    print("用法：key get <名字> | key list", file=sys.stderr)
    return 2
