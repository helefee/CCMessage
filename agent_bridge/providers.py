# -*- coding: utf-8 -*-
"""供应商：CC Msg 自己管 Claude Code / Codex 的供应商（名字、Base URL、key、模型），一键切换，不依赖 CC Switch。

三个层级（越往下越优先，都不改别人的全局配置）：
- 全局：改 ~/.claude/settings.json / ~/.codex/config.toml 里 CC Msg 管的那几项（先备份、原子写、读回核对）。
- 项目：Claude 写 <项目>/.claude/settings.local.json（命令行 / VS Code 认，交互模式要先信任该目录；桌面版不认）。
        Codex 不允许项目配置改供应商（官方限制），所以只记在这里：CC Msg 开的命令行会话和后台派活按它启动；
        模型 / 思考强度照常写进 <项目>/.codex/config.toml（桌面版也认，项目要受信任）。
- 会话：CC Msg 开终端启动 / 恢复会话。Claude 用 `--settings <文件>`（文件里没有 key），Codex 用 `-c` 覆盖。只限命令行。

key 一律在凭据管理器里（keystore），配置文件里只有取 key 的命令。providers.json 里只存 key 的名字。
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from . import keystore, modelcfg, paths

STORE = paths.HOME / "providers.json"
LAUNCH = paths.HOME / "launch"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
URL_RE = re.compile(r"^https?://[^\s\"']+$")
CLAUDE_MODELS = {"model": "model", "opus": "ANTHROPIC_DEFAULT_OPUS_MODEL", "sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
                 "haiku": "ANTHROPIC_DEFAULT_HAIKU_MODEL", "fable": "ANTHROPIC_DEFAULT_FABLE_MODEL",
                 "subagent": "CLAUDE_CODE_SUBAGENT_MODEL"}
CLAUDE_MAP_ENV = [v for k, v in CLAUDE_MODELS.items() if k != "model"]
CODEX_MODELS = ["model", "review_model", "subagent"]
CODEX_BUILTIN = {"openai", "ollama", "lmstudio"}
CODEX_SESSION_ID = "ccmsg-session"
WIRE = ("responses", "chat")

# 每一层在哪些地方生效（页面上照这个标）
REACH = {
    "claude": {"global": {"cli": True, "desktop": False, "note": "Claude 桌面版走自己的登录 / 第三方推理配置，不读 settings.json 的 Base URL 和 key"},
               "project": {"cli": True, "desktop": False, "note": "命令行 / VS Code 认；交互模式要先信任这个目录才生效；桌面版不认"},
               "session": {"cli": True, "desktop": False, "note": "CC Msg 开一个终端跑 claude --settings …，只限命令行"}},
    "codex": {"global": {"cli": True, "desktop": True, "note": "命令行和桌面版都认；桌面版要退出重开"},
              "project": {"cli": True, "desktop": False, "note": "Codex 不许项目配置改供应商：只对 CC Msg 开的命令行会话和后台派活生效；模型 / 思考强度桌面版也认"},
              "session": {"cli": True, "desktop": False, "note": "CC Msg 开一个终端跑 codex -c …，只限命令行"}},
}


# ---------------- 存储 ----------------

def load() -> dict:
    try:
        d = json.loads(STORE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    d.setdefault("claude", [])
    d.setdefault("codex", [])
    d.setdefault("active", {})
    d.setdefault("projects", {})
    return d


def save(d: dict):
    modelcfg.atomic_write(STORE, (json.dumps(d, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _tool(tool):
    if tool not in ("claude", "codex"):
        raise ValueError("tool 只能是 claude / codex")
    return tool


def get(tool: str, pid: str) -> dict:
    p = next((x for x in load()[_tool(tool)] if x["id"] == pid), None)
    if not p:
        raise ValueError(f"没有这个供应商：{pid}")
    return p


def key_name(tool: str, pid: str) -> str:
    return keystore.check_name(f"{tool}-{pid}")


def _slug(name: str, taken: set) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:24] or "p"
    if not s[0].isalnum():
        s = "p" + s
    out, i = s, 2
    while out in taken:
        out, i = f"{s}-{i}", i + 1
    return out


def _clean(tool: str, p: dict) -> dict:
    """校验并只留认识的字段。"""
    mode = p.get("mode") or "key"
    if tool == "claude" and mode not in ("login", "key"):
        raise ValueError("Claude 的方式只能是 login / key")
    if tool == "codex" and mode not in ("chatgpt", "key"):
        raise ValueError("Codex 的方式只能是 chatgpt / key")
    name = str(p.get("name") or "").strip()[:60]
    if not name:
        raise ValueError("名字不能空")
    base = str(p.get("base_url") or "").strip().rstrip("/")
    if mode == "key":
        if not base:
            base = "https://api.anthropic.com" if tool == "claude" else "https://api.openai.com/v1"
        if not URL_RE.match(base):
            raise ValueError("Base URL 要以 http:// 或 https:// 开头")
    else:
        base = ""
    out = {"id": p.get("id"), "name": name, "mode": mode, "base_url": base, "notes": str(p.get("notes") or "")[:200],
           "from": p.get("from")}
    models = p.get("models") or {}
    keys = CLAUDE_MODELS if tool == "claude" else CODEX_MODELS
    out["models"] = {k: modelcfg._check_model(models.get(k)) for k in keys if models.get(k)}
    eff = p.get("effort") or None
    if eff:
        ok = modelcfg.CLAUDE_EFFORTS if tool == "claude" else modelcfg.CODEX_EFFORTS + ["none", "ultra", "max"]
        if eff not in ok:
            raise ValueError(f"思考强度不认识：{eff}")
    out["effort"] = eff
    if tool == "codex":
        w = p.get("wire_api") or "responses"
        if w not in WIRE:
            raise ValueError("wire_api 只能是 responses / chat")
        out["wire_api"] = w
    return out


def upsert(tool: str, prov: dict, secret: str | None = None) -> dict:
    """新增 / 修改一个供应商。secret 给了就存进凭据管理器（名字 <tool>-<id>），不给就保留原来的 key。"""
    _tool(tool)
    d = load()
    lst = d[tool]
    pid = prov.get("id")
    old = next((x for x in lst if x["id"] == pid), None) if pid else None
    if pid and not old and not ID_RE.match(pid):
        raise ValueError("id 只能是小写字母、数字和 -")
    c = _clean(tool, prov)
    if not old:
        c["id"] = pid if pid and ID_RE.match(pid) and pid not in {x["id"] for x in lst} else _slug(c["name"], {x["id"] for x in lst})
        c["created"] = time.time()
    else:
        c["id"] = old["id"]
        c["created"] = old.get("created")
        c["from"] = c.get("from") or old.get("from")
    c["key"] = (old or {}).get("key")
    if c["mode"] == "key":
        if secret:
            kn = key_name(tool, c["id"])
            keystore.put(kn, secret.strip(), f"CC Msg 供应商 {tool}/{c['name']}")
            c["key"] = kn
    else:
        c["key"] = None
    c["updated"] = time.time()
    if old:
        lst[lst.index(old)] = c
    else:
        lst.append(c)
    save(d)
    return view_one(tool, c)


def delete(tool: str, pid: str, drop_key: bool = False) -> dict:
    d = load()
    p = next((x for x in d[_tool(tool)] if x["id"] == pid), None)
    if not p:
        raise ValueError("没有这个供应商")
    used = [r for r, v in d["projects"].items() if v.get(tool) == pid]
    if used:
        raise ValueError("这些项目还在用它，先取消项目覆盖：" + "、".join(used))
    d[tool].remove(p)
    if d["active"].get(tool) == pid:
        d["active"].pop(tool)
    save(d)
    if drop_key and p.get("key"):
        keystore.delete(p["key"])
    return {"ok": True}


def view_one(tool: str, p: dict) -> dict:
    v = dict(p)
    v["key_masked"] = keystore.mask(keystore.get(p["key"])) if p.get("key") else None
    v["key_missing"] = bool(p.get("mode") == "key" and not v["key_masked"])
    return v


# ---------------- 当前生效的是谁 ----------------

def _claude_live(scope="user", root=None) -> dict:
    s = modelcfg._read_json(modelcfg.claude_file(scope, root))
    env = s.get("env") if isinstance(s.get("env"), dict) else {}
    return {"base_url": (env.get("ANTHROPIC_BASE_URL") or "").rstrip("/"), "helper_key": modelcfg._helper_name(s.get("apiKeyHelper")),
            "plaintext": [k for k in modelcfg.CLAUDE_SECRET_ENV if env.get(k)], "has_helper": "apiKeyHelper" in s}


def _codex_cfg(p: Path) -> dict:
    import tomllib
    try:
        return tomllib.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return {}


def codex_slot(d: dict | None = None) -> str:
    """全局切换时用哪个 [model_providers.<id>]：沿用现在的自定义 id（Codex 按它分对话列表），没有就 ccmsg。"""
    d = d or load()
    if d.get("codex_slot"):
        return d["codex_slot"]
    cur = _codex_cfg(paths.CODEX_HOME / "config.toml").get("model_provider")
    return cur if cur and cur not in CODEX_BUILTIN else "ccmsg"


def _codex_live() -> dict:
    cfg = _codex_cfg(paths.CODEX_HOME / "config.toml")
    pid = cfg.get("model_provider") or "openai"
    t = (cfg.get("model_providers") or {}).get(pid) or {}
    a = t.get("auth") if isinstance(t.get("auth"), dict) else {}
    args = a.get("args") or []
    kname = args[-1] if len(args) >= 3 and args[-3:-1] == ["key", "get"] else None
    return {"provider": pid, "base_url": (t.get("base_url") or "").rstrip("/"), "helper_key": kname,
            "plaintext": bool(t.get("experimental_bearer_token")), "requires_openai_auth": bool(t.get("requires_openai_auth"))}


def matches(tool: str, p: dict) -> bool:
    if tool == "claude":
        lv = _claude_live()
        if p["mode"] == "login":
            return not lv["base_url"] and not lv["helper_key"] and not lv["plaintext"]
        return lv["base_url"] == p["base_url"].rstrip("/") and lv["helper_key"] == p.get("key") and not lv["plaintext"]
    lv = _codex_live()
    if p["mode"] == "chatgpt":
        return lv["provider"] == "openai"
    return lv["base_url"] == p["base_url"].rstrip("/") and lv["helper_key"] == p.get("key")


def overview(root: str | None = None) -> dict:
    d = load()
    out = {"reach": REACH, "codex_slot": codex_slot(d), "projects": d["projects"], "cli": {"claude": claude_exe(), "codex": codex_exe()}}
    for tool in ("claude", "codex"):
        act = d["active"].get(tool)
        rows = []
        for p in d[tool]:
            v = view_one(tool, p)
            v["active"] = p["id"] == act
            v["live"] = matches(tool, p)
            v["projects"] = [r for r, x in d["projects"].items() if x.get(tool) == p["id"]]
            rows.append(v)
        out[tool] = rows
        out[tool + "_drift"] = bool(act and not any(r["live"] for r in rows if r["id"] == act))
    out["claude_live"] = _claude_live()
    out["codex_live"] = _codex_live()
    if root:
        out["project"] = {"root": root, **(d["projects"].get(root) or {})}
    return out


# ---------------- 全局切换 ----------------

def _need_key(tool, p):
    if p["mode"] != "key":
        return None
    if not p.get("key") or not keystore.get(p["key"]):
        raise ValueError(f"「{p['name']}」还没有 key，先编辑填一个")
    keystore.helper_selftest(p["key"])
    return p["key"]


def apply_global(tool: str, pid: str, drop_plaintext: bool = False) -> dict:
    """一键切换（全局）。会被拿掉的明文 key 先列出来要求确认（备份里还在）。"""
    p = get(tool, pid)
    kn = _need_key(tool, p)
    if tool == "claude":
        r = _apply_claude_file(modelcfg.claude_file("user", None), p, kn, scope="user", drop_plaintext=drop_plaintext)
    else:
        r = _apply_codex_global(p, kn, drop_plaintext)
    d = load()
    d["active"][tool] = pid
    save(d)
    r["live"] = matches(tool, p)
    return r


def _claude_values(p: dict, kn: str | None, scope: str) -> tuple[dict, dict]:
    """返回 (env 要设的, 顶层要设的)。值 None = 删掉，"" = 显式置空（项目 / 会话层用来盖住用户层）。"""
    blank = "" if scope != "user" else None
    env, top = {}, {}
    if p["mode"] == "key":
        env["ANTHROPIC_BASE_URL"] = p["base_url"]
        top["apiKeyHelper"] = keystore.helper_line(kn)
    else:
        env["ANTHROPIC_BASE_URL"] = blank
        top["apiKeyHelper"] = blank
    for k in modelcfg.CLAUDE_SECRET_ENV:        # 用户层的明文 key 会压过 apiKeyHelper（实测），上层要置空
        env[k] = blank
    m = p.get("models") or {}
    for short, var in CLAUDE_MODELS.items():
        if short == "model":
            if m.get("model"):
                top["model"] = m["model"]
            continue
        env[var] = m.get(short) or blank
    if p.get("effort"):
        top["effortLevel"] = p["effort"]
    return env, top


def _apply_claude_file(f: Path, p: dict, kn: str | None, scope: str, drop_plaintext: bool = False) -> dict:
    s = modelcfg._read_json(f)
    cur_env = s.get("env") if isinstance(s.get("env"), dict) else {}
    plain = [k for k in modelcfg.CLAUDE_SECRET_ENV if cur_env.get(k)]
    if plain and not drop_plaintext:
        raise PlaintextError(f"{f.name} 里有明文 key：" + "、".join(plain) + "。它会压过 CC Msg 设的 key，切换要把它拿掉（备份里还在）。确认后再切。",
                             [{"field": "env." + k, "masked": keystore.mask(cur_env[k])} for k in plain])
    env_set, top_set = _claude_values(p, kn, scope)
    bid = modelcfg.backup(f, f"switch-claude-{p['id']}")
    env = s.get("env") if isinstance(s.get("env"), dict) else None
    had_env = bool(env)
    for k, v in env_set.items():
        if v is None:
            if env is not None:
                env.pop(k, None)
        else:
            if env is None:
                env = s["env"] = {}
            env[k] = v
    for k, v in top_set.items():
        if v is None:
            s.pop(k, None)
        else:
            s[k] = v
    if env is not None and not env and (had_env or "env" in s):
        s.pop("env", None)
    modelcfg.atomic_write(f, (json.dumps(s, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    back = modelcfg._read_json(f)
    benv = back.get("env") or {}
    ok = all(benv.get(k) == v for k, v in env_set.items()) and all(back.get(k) == v for k, v in top_set.items())
    return {"ok": ok, "file": str(f), "backup": bid}


class PlaintextError(ValueError):
    def __init__(self, msg, items):
        super().__init__(msg)
        self.items = items


def _apply_codex_global(p: dict, kn: str | None, drop_plaintext: bool) -> dict:
    tk = modelcfg._toml()
    f = paths.CODEX_HOME / "config.toml"
    doc = tk.parse(f.read_text(encoding="utf-8") if f.exists() else "")
    d = load()
    slot = codex_slot(d)
    if p["mode"] == "key":
        mp = doc.get("model_providers")
        t = mp.get(slot) if mp is not None else None
        if t is not None and t.get("experimental_bearer_token") and not drop_plaintext:
            raise PlaintextError(f"[model_providers.{slot}] 里有明文 experimental_bearer_token，切换要把它换成取 key 的命令（备份里还在）。确认后再切。",
                                 [{"field": f"model_providers.{slot}.experimental_bearer_token", "masked": keystore.mask(str(t.get("experimental_bearer_token")))}])
    bid = modelcfg.backup(f, f"switch-codex-{p['id']}")
    if p["mode"] == "chatgpt":
        doc["model_provider"] = "openai"
    else:
        if "model_providers" not in doc:
            doc["model_providers"] = tk.table(is_super_table=True)
        mp = doc["model_providers"]
        if slot not in mp:
            mp[slot] = tk.table()
        t = mp[slot]
        t["name"] = p["name"]
        t["base_url"] = p["base_url"]
        t["wire_api"] = p.get("wire_api") or "responses"
        for k in ("experimental_bearer_token", "env_key", "requires_openai_auth"):   # 和 auth 不能并存（Codex 会拒绝加载）
            if k in t:
                del t[k]
        cmd = keystore.helper_cmd(kn)
        a = tk.table()
        a["command"] = cmd[0]
        a["args"] = cmd[1:]
        a["timeout_ms"] = 15000
        t["auth"] = a
        doc["model_provider"] = slot
    m = p.get("models") or {}
    for k in ("model", "review_model"):
        if m.get(k):
            doc[k] = m[k]
    if p.get("effort"):
        doc["model_reasoning_effort"] = p["effort"]
    if m.get("subagent"):
        if "agents" not in doc:
            doc["agents"] = tk.table()
        doc["agents"]["default_subagent_model"] = m["subagent"]
    out = tk.dumps(doc)
    import tomllib
    tomllib.loads(out)
    modelcfg.atomic_write(f, out.encode("utf-8"))
    return {"ok": True, "file": str(f), "backup": bid, "slot": slot if p["mode"] == "key" else "openai"}


def set_slot(slot: str) -> dict:
    if not re.match(r"^[A-Za-z0-9_-]{1,40}$", slot) or slot in CODEX_BUILTIN:
        raise ValueError("id 只能是字母、数字、_ 和 -，也不能是 openai / ollama / lmstudio")
    d = load()
    d["codex_slot"] = slot
    save(d)
    return {"ok": True, "slot": slot}


# ---------------- 项目覆盖 ----------------

def apply_project(tool: str, root: str, pid: str | None, drop_plaintext: bool = False) -> dict:
    """pid=None 取消覆盖（只删 CC Msg 写过的那几项）。"""
    d = load()
    proj = d["projects"].setdefault(root, {})
    res = {"ok": True}
    if tool == "claude":
        f = modelcfg.claude_file("project", root)
        if pid:
            p = get(tool, pid)
            kn = _need_key(tool, p)
            res = _apply_claude_file(f, p, kn, scope="project", drop_plaintext=drop_plaintext)
            _git_exclude(Path(root), [".claude/settings.local.json"])
        else:
            res = _clear_claude_project(f)
    else:
        if pid:
            p = get(tool, pid)
            kn = _need_key(tool, p)
            ch = {}
            if (p.get("models") or {}).get("model"):
                ch["model"] = p["models"]["model"]
            if p.get("effort"):
                ch["model_reasoning_effort"] = p["effort"]
            if ch:
                res = modelcfg.set_codex("project", root, ch, label=f"project-codex-{pid}")
            _write_bus_args(root, codex_args(p, kn))
        else:
            _write_bus_args(root, None)
    if pid:
        proj[tool] = pid
    else:
        proj.pop(tool, None)
    if not proj:
        d["projects"].pop(root, None)
    save(d)
    return res


def _clear_claude_project(f: Path) -> dict:
    if not f.exists():
        return {"ok": True, "file": str(f), "backup": None}
    s = modelcfg._read_json(f)
    bid = modelcfg.backup(f, "project-claude-clear")
    env = s.get("env") if isinstance(s.get("env"), dict) else None
    if env is not None:
        for k in ["ANTHROPIC_BASE_URL", *modelcfg.CLAUDE_SECRET_ENV, *CLAUDE_MAP_ENV]:
            env.pop(k, None)
        if not env:
            s.pop("env")
    for k in ("apiKeyHelper",):
        s.pop(k, None)
    modelcfg.atomic_write(f, (json.dumps(s, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return {"ok": True, "file": str(f), "backup": bid}


def _git_exclude(root: Path, pats):
    try:
        from .server import git_exclude
        git_exclude(root, pats)
    except Exception:
        pass


def _bus_dir(root: str) -> Path | None:
    for rel in (".msgbus", ".temp/msgbus"):
        p = Path(root) / rel
        if (p / "bus.py").exists() or (p / "sessions").is_dir():
            return p
    return None


def _write_bus_args(root: str, args: list[str] | None):
    """后台派活（bus 跑 codex exec）也按项目的供应商来：写一份 -c 参数给它读。"""
    b = _bus_dir(root)
    if not b:
        return
    f = b / "codex-provider.json"
    if args is None:
        try:
            f.unlink()
        except OSError:
            pass
        return
    modelcfg.atomic_write(f, json.dumps({"args": args}, ensure_ascii=False).encode("utf-8"))


# ---------------- 会话（命令行） ----------------

def _toml_str(v: str) -> str:
    return json.dumps(v, ensure_ascii=False)


def codex_args(p: dict, kn: str | None) -> list[str]:
    """codex 的 -c 覆盖：用单独的 provider id（和用户配置里的 auth 不冲突；恢复老对话时它原来的归属不变）。"""
    if p["mode"] == "chatgpt":
        a = ["-c", 'model_provider="openai"']
    else:
        pid = CODEX_SESSION_ID
        cmd = keystore.helper_cmd(kn)
        a = ["-c", f"model_provider={_toml_str(pid)}",
             "-c", f"model_providers.{pid}.name={_toml_str(p['name'])}",
             "-c", f"model_providers.{pid}.base_url={_toml_str(p['base_url'])}",
             "-c", f"model_providers.{pid}.wire_api={_toml_str(p.get('wire_api') or 'responses')}",
             "-c", f"model_providers.{pid}.auth.command={_toml_str(cmd[0])}",
             "-c", f"model_providers.{pid}.auth.args={json.dumps(cmd[1:], ensure_ascii=False)}",
             "-c", f"model_providers.{pid}.auth.timeout_ms=15000"]
    m = p.get("models") or {}
    if m.get("model"):
        a += ["-c", f"model={_toml_str(m['model'])}"]
    if m.get("review_model"):
        a += ["-c", f"review_model={_toml_str(m['review_model'])}"]
    if p.get("effort"):
        a += ["-c", f"model_reasoning_effort={_toml_str(p['effort'])}"]
    if m.get("subagent"):
        a += ["-c", f"agents.default_subagent_model={_toml_str(m['subagent'])}"]
    return a


def claude_settings_file(p: dict, kn: str | None) -> Path:
    """claude --settings 用的文件（只有 Base URL、取 key 的命令和模型，没有 key）。"""
    env, top = _claude_values(p, kn, scope="session")
    body = {"env": env, **top}
    LAUNCH.mkdir(parents=True, exist_ok=True)
    f = LAUNCH / f"claude-{p['id']}.json"
    modelcfg.atomic_write(f, (json.dumps(body, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return f


def _newest(pat: str) -> str | None:
    c = sorted(glob.glob(pat), key=lambda x: os.path.getmtime(x), reverse=True)
    return c[0] if c else None


def claude_exe() -> str | None:
    p = os.environ.get("CLAUDE_CLI_PATH")
    if p and Path(p).exists():
        return p
    w = shutil.which("claude")
    if w:
        return w
    if os.name == "nt":
        for pat in (os.path.expandvars(r"%USERPROFILE%\.local\bin\claude.exe"),
                    os.path.expandvars(r"%APPDATA%\Claude\claude-code\*\*\claude.exe"),
                    os.path.expandvars(r"%APPDATA%\Claude\claude-code\*\claude.exe")):
            x = _newest(pat)
            if x:
                return x
    return None


def codex_exe() -> str | None:
    p = os.environ.get("CODEX_CLI_PATH")
    if p and Path(p).exists():
        return p
    if os.name == "nt":
        x = _newest(os.path.expandvars(r"%LOCALAPPDATA%\OpenAI\Codex\bin\*\codex.exe"))
        if x:
            return x
    return shutil.which("codex")


def session_cmd(tool: str, pid: str | None, root: str, resume: str | None = None, prompt: str | None = None) -> list[str]:
    """拼命令行（不含 key）。pid=None：按项目覆盖 → 全局，什么都不加。"""
    exe = claude_exe() if tool == "claude" else codex_exe()
    if not exe:
        raise RuntimeError(f"找不到 {tool} 命令行（Claude Code / Codex 没装命令行版；可设 {'CLAUDE' if tool == 'claude' else 'CODEX'}_CLI_PATH）")
    d = load()
    pid = pid or (d["projects"].get(root) or {}).get(tool)
    extra = []
    if pid:
        p = get(tool, pid)
        kn = _need_key(tool, p)
        if tool == "claude":
            extra = ["--settings", str(claude_settings_file(p, kn))]
        else:
            extra = codex_args(p, kn)
    if resume and not re.match(r"^[A-Za-z0-9-]{6,80}$", resume):
        raise ValueError("会话号不对")
    if tool == "claude":
        cmd = [exe, *extra]
        if resume:
            cmd += ["--resume", resume]
        if prompt is not None:
            cmd += ["-p", prompt]
    else:
        if prompt is not None:
            cmd = [exe, "exec", *(["resume", resume] if resume else []), "--skip-git-repo-check", *extra, prompt]
        else:
            cmd = [exe, *extra, "-C", root, *(["resume", resume] if resume else [])]
    return cmd


def launch(tool: str, pid: str | None, root: str, resume: str | None = None, dry: bool = False) -> dict:
    """开一个新终端窗口跑命令行会话（用户点了才开，是可见窗口）。dry=True 只返回命令行。"""
    _tool(tool)
    if not Path(root).is_dir():
        raise ValueError("项目目录不存在")
    cmd = session_cmd(tool, pid, root, resume)
    shown = subprocess.list2cmdline(cmd) if os.name == "nt" else " ".join(cmd)
    if dry:
        return {"ok": True, "cmd": shown, "argv": cmd}
    env = dict(os.environ)
    for k in ("CLAUDE_CODE_SESSION_ID", "CODEX_SESSION_ID", "CODEX_THREAD_ID", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(k, None)
    if os.name == "nt":
        subprocess.Popen(cmd, cwd=root, env=env, creationflags=0x00000010)        # CREATE_NEW_CONSOLE：用户要看的终端
    elif sys.platform == "darwin":
        script = "cd " + _sh(root) + " && " + " ".join(_sh(a) for a in cmd)
        subprocess.Popen(["osascript", "-e", f'tell application "Terminal" to do script {json.dumps(script)}'])
    else:
        term = shutil.which("x-terminal-emulator") or shutil.which("gnome-terminal") or shutil.which("xterm")
        if not term:
            raise RuntimeError("找不到终端程序；命令行在这里，可以自己复制去跑：" + shown)
        subprocess.Popen([term, "-e", *cmd], cwd=root, env=env)
    return {"ok": True, "cmd": shown}


def _sh(a: str) -> str:
    return "'" + a.replace("'", "'\\''") + "'"


# ---------------- 从 CC Switch 导入（只读） ----------------

def _ccs_rows() -> list[tuple]:
    db = Path.home() / ".cc-switch" / "cc-switch.db"
    if not db.exists():
        raise ValueError("没装 CC Switch（~/.cc-switch/cc-switch.db 不存在）")
    last = None
    for i in range(6):
        try:
            c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=3)
            rows = list(c.execute("SELECT id, app_type, name, settings_config, is_current, website_url FROM providers"))
            c.close()
            return rows
        except sqlite3.Error as e:
            last = e
            time.sleep(0.5 * (i + 1))
    raise RuntimeError(f"CC Switch 的数据库打不开（被占用？）：{last}")


def _ccs_parse(app: str, sc: dict) -> tuple[dict, str | None]:
    """把 CC Switch 的 settings_config 变成 (供应商, key)。key 只在内存里。"""
    if app == "claude":
        env = sc.get("env") if isinstance(sc.get("env"), dict) else {}
        key = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY") or None
        base = env.get("ANTHROPIC_BASE_URL") or ""
        models = {"model": env.get("ANTHROPIC_MODEL"), "opus": env.get("ANTHROPIC_DEFAULT_OPUS_MODEL"),
                  "sonnet": env.get("ANTHROPIC_DEFAULT_SONNET_MODEL"), "haiku": env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL") or env.get("ANTHROPIC_SMALL_FAST_MODEL"),
                  "fable": env.get("ANTHROPIC_DEFAULT_FABLE_MODEL"), "subagent": env.get("CLAUDE_CODE_SUBAGENT_MODEL")}
        mode = "key" if (key or base) else "login"
        return {"mode": mode, "base_url": base, "models": {k: v for k, v in models.items() if v}}, key
    import tomllib
    cfg = sc.get("config")
    try:
        cfg = tomllib.loads(cfg) if isinstance(cfg, str) else (cfg if isinstance(cfg, dict) else {})
    except Exception:
        cfg = {}
    auth = sc.get("auth") if isinstance(sc.get("auth"), dict) else {}
    pid = cfg.get("model_provider")
    t = ((cfg.get("model_providers") or {}).get(pid) or {}) if pid else {}
    key = t.get("experimental_bearer_token") or auth.get("OPENAI_API_KEY") or None
    if not pid or pid == "openai":
        mode, base = ("key", "https://api.openai.com/v1") if key else ("chatgpt", "")
    else:
        mode, base = "key", t.get("base_url") or ""
    models = {"model": cfg.get("model"), "review_model": cfg.get("review_model"),
              "subagent": (cfg.get("agents") or {}).get("default_subagent_model") if isinstance(cfg.get("agents"), dict) else None}
    return {"mode": mode, "base_url": base, "wire_api": t.get("wire_api") or "responses",
            "effort": cfg.get("model_reasoning_effort"), "models": {k: v for k, v in models.items() if v}}, key


def ccswitch_preview() -> list[dict]:
    """列出能导入的（只读；key 只给打码）。"""
    have = {(t, p.get("from")) for t in ("claude", "codex") for p in load()[t]}
    out = []
    for rid, app, name, sc, cur, site in _ccs_rows():
        if app not in ("claude", "codex"):
            continue
        try:
            scd = json.loads(sc or "{}")
        except ValueError:
            continue
        p, key = _ccs_parse(app, scd)
        if p["mode"] == "login" and not p["models"]:
            continue                                  # 空壳（只有一个子代理之类也算有内容）
        out.append({"ccs_id": rid, "tool": app, "name": name, "current": bool(cur), "mode": p["mode"], "base_url": p["base_url"],
                    "models": p["models"], "key_masked": keystore.mask(key) if key else None,
                    "imported": (app, f"ccswitch:{rid}") in have})
    return out


def ccswitch_import(ids: list[str]) -> dict:
    """把选中的导进来：key 进凭据管理器。CC Switch 的数据库只读，不改它。"""
    done, skipped = [], []
    want = set(ids)
    have = {(t, p.get("from")) for t in ("claude", "codex") for p in load()[t]}
    for rid, app, name, sc, cur, site in _ccs_rows():
        if rid not in want or app not in ("claude", "codex"):
            continue
        if (app, f"ccswitch:{rid}") in have:
            skipped.append(name)
            continue
        p, key = _ccs_parse(app, json.loads(sc or "{}"))
        eff = p.get("effort")
        ok_eff = modelcfg.CLAUDE_EFFORTS if app == "claude" else modelcfg.CODEX_EFFORTS + ["none", "ultra", "max"]
        p.update({"name": name, "from": f"ccswitch:{rid}", "notes": "从 CC Switch 导入", "effort": eff if eff in ok_eff else None})
        p["models"] = {k: v for k, v in p["models"].items() if modelcfg.MODEL_RE.match(str(v))}
        v = upsert(app, p, secret=key)
        done.append({"tool": app, "id": v["id"], "name": name, "key_masked": v["key_masked"]})
    return {"imported": done, "skipped": skipped}


# ---------------- 测试连接 ----------------

def test(tool: str, pid: str) -> dict:
    p = get(tool, pid)
    if p["mode"] != "key":
        return {"ok": False, "error": "官方登录用 OAuth / ChatGPT 登录，CC Msg 测不了"}
    m = p.get("models") or {}
    return modelcfg.test_connection(tool, p["base_url"], p.get("key"), m.get("model") if tool == "codex" else (m.get("haiku") or m.get("model")))
