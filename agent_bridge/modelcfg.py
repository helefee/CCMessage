# -*- coding: utf-8 -*-
"""模型与 API：读 / 改 Claude Code 和 Codex 的模型设置、API Key（只动 CC Msg 自己管的那几项）。

- 只改白名单里的键，其余原样保留：JSON 保持原顺序，TOML 用 tomlkit 保留注释和格式。
- 每次写之前先备份到 ~/.agent-bridge/backups，写临时文件再原子替换，写完重新读一遍核对；备份一键还原。
- key 存 Windows 凭据管理器（keystore.py），配置文件里只写取 key 的命令。返回给网页的只有打码后的样子。
- 供应商切换归 CC Switch：检测到它时页面会标「由 CC Switch 管理」，这里不抢。
- 迁移明文 key、统一子代理模型、指向代理等都只在用户点按钮时做，先备份。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import time
import urllib.request
from pathlib import Path

from . import keystore, paths

CLAUDE_ENV_OWNED = ["ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                    "ANTHROPIC_DEFAULT_FABLE_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL", "ANTHROPIC_BASE_URL"]
CLAUDE_TOP_OWNED = ["model", "effortLevel", "apiKeyHelper"]
CLAUDE_SECRET_ENV = ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"]
CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh"]
CODEX_TOP_OWNED = ["model", "review_model", "model_reasoning_effort", "model_provider"]
CODEX_AGENTS_OWNED = ["default_subagent_model", "default_subagent_reasoning_effort"]
CODEX_EFFORTS = ["minimal", "low", "medium", "high", "xhigh"]
BACKUPS = paths.HOME / "backups"
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,127}$")


def mask(v):
    return keystore.mask(v)


# ---------------- 文件位置 ----------------

def claude_file(scope: str, root: str | None) -> Path:
    if scope == "project":
        return Path(root) / ".claude" / "settings.local.json"
    return paths.CLAUDE_HOME / "settings.json"


def codex_file(scope: str, root: str | None) -> Path:
    if scope == "project":
        return Path(root) / ".codex" / "config.toml"
    return paths.CODEX_HOME / "config.toml"


# ---------------- 备份 / 原子写 ----------------

def backup(p: Path, label: str) -> str | None:
    if not p.exists():
        return None
    BACKUPS.mkdir(parents=True, exist_ok=True)
    bid = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}-{label}"
    shutil.copy2(p, BACKUPS / (bid + p.suffix))
    with open(BACKUPS / "index.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"id": bid, "file": str(p), "ext": p.suffix, "at": time.time(), "label": label}, ensure_ascii=False) + "\n")
    return bid


def backups(limit: int = 60) -> list[dict]:
    try:
        rows = [json.loads(x) for x in (BACKUPS / "index.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    except OSError:
        return []
    rows = [r for r in rows if (BACKUPS / (r["id"] + r["ext"])).exists()]
    return list(reversed(rows))[:limit]


def restore(bid: str) -> dict:
    r = next((x for x in backups(10000) if x["id"] == bid), None)
    if not r:
        raise ValueError("没有这个备份")
    dst = Path(r["file"])
    before = backup(dst, "before-restore")
    atomic_write(dst, (BACKUPS / (bid + r["ext"])).read_bytes())
    return {"restored": str(dst), "backup_of_current": before}


def atomic_write(p: Path, data: bytes):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".ccmsg-{os.getpid()}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    for i in range(5):                       # Windows：文件正被别人读时 replace 可能短暂失败
        try:
            os.replace(tmp, p)
            return
        except PermissionError:
            time.sleep(0.2 * (i + 1))
    os.replace(tmp, p)


# ---------------- Claude ----------------

def _read_json(p: Path) -> dict:
    if not p.exists():
        return {}
    t = p.read_text(encoding="utf-8-sig")
    return json.loads(t) if t.strip() else {}


def _helper_name(helper: str | None) -> str | None:
    m = re.search(r"\bkey\"?\s+\"?get\"?\s+\"?([a-z0-9._-]+)", helper or "")
    return m.group(1) if m else None


def claude_state(scope: str = "user", root: str | None = None) -> dict:
    p = claude_file(scope, root)
    err = None
    try:
        s = _read_json(p)
    except (ValueError, OSError) as e:
        s, err = {}, f"{type(e).__name__}: {e}"
    env = s.get("env") if isinstance(s.get("env"), dict) else {}
    helper = s.get("apiKeyHelper")
    hname = _helper_name(helper)
    secrets = {k: mask(env.get(k)) for k in CLAUDE_SECRET_ENV if env.get(k)}
    cj = _read_json(paths.CLAUDE_HOME.parent / ".claude.json") if not os.environ.get("CLAUDE_CONFIG_DIR") else _read_json(paths.CLAUDE_HOME / ".claude.json")
    oauth = cj.get("oauthAccount") or {}
    return {"file": str(p), "exists": p.exists(), "error": err, "scope": scope,
            "values": {**{k: s.get(k) for k in ("model", "effortLevel")}, **{k: env.get(k) for k in CLAUDE_ENV_OWNED}},
            "apiKeyHelper": {"set": bool(helper), "ours": bool(hname), "key": hname,
                             "masked": (mask(keystore.get(hname)) if hname else None) if hname else None,
                             "other": (helper if helper and not hname else None)},
            "plaintext": secrets, "login": {"oauth": bool(oauth), "billing": oauth.get("billingType"),
                                            "email": _mask_email(oauth.get("emailAddress"))},
            "other_env": sorted(k for k in env if k not in CLAUDE_ENV_OWNED and k not in CLAUDE_SECRET_ENV),
            "symlink": p.is_symlink()}


def _mask_email(e):
    if not e or "@" not in e:
        return None
    a, b = e.split("@", 1)
    return a[:2] + "***@" + b


def _check_model(v):
    if v in (None, ""):
        return None
    v = str(v).strip()
    if not MODEL_RE.match(v):
        raise ValueError(f"模型名不对：{v}")
    return v


def set_claude(scope: str, root: str | None, changes: dict, label: str = "claude") -> dict:
    """changes：{键: 值}，值为 None / "" 表示删掉。只接受 CC Msg 管的键。"""
    allowed = set(CLAUDE_TOP_OWNED + CLAUDE_ENV_OWNED) if scope == "user" else {"model", "effortLevel"}
    bad = set(changes) - allowed
    if bad:
        raise ValueError("这些键 CC Msg 不管：" + "、".join(sorted(bad)))
    p = claude_file(scope, root)
    s = _read_json(p)
    want = {}
    for k, v in changes.items():
        if k == "effortLevel":
            v = v or None
            if v and v not in CLAUDE_EFFORTS:
                raise ValueError("effortLevel 只能是 " + "/".join(CLAUDE_EFFORTS))
        elif k == "ANTHROPIC_BASE_URL":
            v = (v or "").strip() or None
            if v and not re.match(r"^https?://[^\s]+$", v):
                raise ValueError("Base URL 要以 http:// 或 https:// 开头")
        elif k == "apiKeyHelper":
            v = (keystore.helper_line(v) if v else None)
        else:
            v = _check_model(v)
        want[k] = v
    bid = backup(p, label)
    env = s.get("env") if isinstance(s.get("env"), dict) else None
    had_env = bool(env)
    for k, v in want.items():
        if k in CLAUDE_ENV_OWNED:
            if env is None:
                env = s["env"] = {}
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
        else:
            if v is None:
                s.pop(k, None)
            else:
                s[k] = v
    if had_env and env is not None and not env:
        s.pop("env")
    atomic_write(p, (json.dumps(s, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    back = _read_json(p)
    benv = back.get("env") or {}
    ok = all((benv.get(k) if k in CLAUDE_ENV_OWNED else back.get(k)) == v for k, v in want.items())
    return {"ok": ok, "file": str(p), "backup": bid}


# ---------------- Codex ----------------

def _toml():
    try:
        import tomlkit
        return tomlkit
    except ImportError:
        raise RuntimeError("缺少 tomlkit（pip install tomlkit）")


def codex_state(scope: str = "user", root: str | None = None) -> dict:
    p = codex_file(scope, root)
    err, cfg = None, {}
    try:
        import tomllib
        cfg = tomllib.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    provs = []
    for pid, v in (cfg.get("model_providers") or {}).items():
        if not isinstance(v, dict):
            continue
        auth = v.get("auth") if isinstance(v.get("auth"), dict) else None
        kname = None
        if auth and "key" in (auth.get("args") or []) and "get" in (auth.get("args") or []):
            kname = (auth.get("args") or [None])[-1]
        provs.append({"id": pid, "name": v.get("name") or pid, "base_url": v.get("base_url"), "wire_api": v.get("wire_api"),
                      "env_key": v.get("env_key"), "env_key_set": bool(v.get("env_key") and os.environ.get(v.get("env_key"))),
                      "auth_ours": bool(kname), "key": kname, "key_masked": mask(keystore.get(kname)) if kname else None,
                      "auth_other": bool(auth and not kname), "plaintext": mask(v.get("experimental_bearer_token")) or None})
    auth = {}
    if scope == "user":
        a = _read_json(paths.CODEX_HOME / "auth.json") if (paths.CODEX_HOME / "auth.json").exists() else {}
        auth = {"mode": a.get("auth_mode"), "api_key": mask(a.get("OPENAI_API_KEY")) or None, "chatgpt": bool(a.get("tokens"))}
    ag = cfg.get("agents") if isinstance(cfg.get("agents"), dict) else {}
    return {"file": str(p), "exists": p.exists(), "error": err, "scope": scope,
            "values": {**{k: cfg.get(k) for k in CODEX_TOP_OWNED}, **{k: ag.get(k) for k in CODEX_AGENTS_OWNED}},
            "providers": provs, "auth": auth, "profiles": sorted((cfg.get("profiles") or {}).keys()),
            "forced_login_method": cfg.get("forced_login_method")}


def set_codex(scope: str, root: str | None, changes: dict, provider_auth: dict | None = None, label: str = "codex",
              add_provider: dict | None = None) -> dict:
    """changes：顶层 model / review_model / model_reasoning_effort / model_provider，和 [agents] 两项。
    provider_auth：{"id": 供应商, "key": key 名 或 None（去掉我们的 auth）}。add_provider：{"id","name","base_url","wire_api"}。"""
    tk = _toml()
    allowed = set(CODEX_TOP_OWNED + CODEX_AGENTS_OWNED) if scope == "user" else {"model", "model_reasoning_effort"}
    bad = set(changes) - allowed
    if bad:
        raise ValueError("这些键 CC Msg 不管：" + "、".join(sorted(bad)))
    for k, v in changes.items():
        if k.endswith("effort") and v and v not in CODEX_EFFORTS + ["none", "ultra", "max"]:
            raise ValueError(f"{k} 不认识：{v}")
        if k in ("model", "review_model", "default_subagent_model"):
            _check_model(v)
    p = codex_file(scope, root)
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    doc = tk.parse(text)
    bid = backup(p, label)
    for k, v in changes.items():
        tgt = doc
        if k in CODEX_AGENTS_OWNED:
            if "agents" not in doc:
                if not v:
                    continue
                doc["agents"] = tk.table()
            tgt = doc["agents"]
        if v in (None, ""):
            if k in tgt:
                del tgt[k]
        else:
            tgt[k] = str(v)
    if "agents" in doc and not len(doc["agents"]):
        del doc["agents"]
    if add_provider:
        pid = add_provider["id"]
        if not re.match(r"^[A-Za-z0-9_-]{1,40}$", pid):
            raise ValueError("供应商 id 不对")
        if "model_providers" not in doc:
            doc["model_providers"] = tk.table(is_super_table=True)
        t = doc["model_providers"].get(pid)
        if t is None:
            t = tk.table()
            doc["model_providers"][pid] = t
        for k in ("name", "base_url", "wire_api"):
            if add_provider.get(k):
                t[k] = add_provider[k]
    if provider_auth:
        pid = provider_auth["id"]
        mp = doc.get("model_providers") or {}
        if pid not in mp:
            raise ValueError(f"config.toml 里没有 [model_providers.{pid}]")
        t = mp[pid]
        if provider_auth.get("key"):
            cmd = keystore.helper_cmd(provider_auth["key"])
            a = tk.table()
            a["command"] = cmd[0]
            a["args"] = cmd[1:]
            a["timeout_ms"] = 15000
            t["auth"] = a
            for k in ("experimental_bearer_token", "env_key"):
                if provider_auth.get("drop_" + k) and k in t:
                    del t[k]
        elif "auth" in t:
            del t["auth"]
    out = tk.dumps(doc)
    import tomllib
    tomllib.loads(out)                                     # 写出去之前先确认还是合法 TOML
    atomic_write(p, out.encode("utf-8"))
    back = tomllib.loads(p.read_text(encoding="utf-8"))
    ok = all(((back.get("agents") or {}).get(k) if k in CODEX_AGENTS_OWNED else back.get(k)) == (v or None) for k, v in changes.items())
    return {"ok": ok, "file": str(p), "backup": bid}


# ---------------- 谁在管：CC Switch ----------------

def ccswitch() -> dict:
    db = Path.home() / ".cc-switch" / "cc-switch.db"
    if not db.exists():
        return {"installed": False}
    out = {"installed": True, "current": {}}
    for i in range(3):
        try:
            c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=3)
            for app, name in c.execute("SELECT app_type, name FROM providers WHERE is_current=1"):   # 只读名字，不读 settings_config（里面有 key）
                out["current"][app] = name
            c.close()
            return out
        except sqlite3.Error as e:
            out["error"] = str(e)
            time.sleep(0.5)
    return out


# ---------------- 代理：opencodex / magpie ----------------

def _ping(url: str, t: float = 0.8):
    try:
        with urllib.request.urlopen(url, timeout=t) as r:
            return r.status
    except Exception as e:
        return getattr(e, "code", None)


def proxies() -> list[dict]:
    home = Path(os.environ.get("OPENCODEX_HOME") or Path.home() / ".opencodex")
    cfg = _read_json(home / "config.json") if (home / "config.json").exists() else {}
    port = int(cfg.get("port") or 10100)
    ocx = {"name": "opencodex", "configured": bool(cfg), "config": str(home / "config.json"), "port": port,
           "running": _ping(f"http://127.0.0.1:{port}/") is not None,
           "default_provider": cfg.get("defaultProvider"), "providers": sorted((cfg.get("providers") or {}).keys()),
           "subagent_models": cfg.get("subagentModels") or [], "anthropic_base": f"http://127.0.0.1:{port}",
           "openai_base": f"http://127.0.0.1:{port}/v1"}
    mag = {"name": "magpie", "configured": (Path.home() / ".magpie").exists(), "port": 3425,
           "running": _ping("http://127.0.0.1:3425/") is not None, "anthropic_base": "http://127.0.0.1:3425",
           "openai_base": "http://127.0.0.1:3425/v1", "providers": [], "subagent_models": []}
    return [ocx, mag]


# ---------------- 子代理定义（.claude/agents/*.md） ----------------
FM = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


def agents(root: str | None = None) -> list[dict]:
    dirs = [("user", paths.CLAUDE_HOME / "agents")]
    if root:
        dirs.append(("project", Path(root) / ".claude" / "agents"))
    out = []
    for scope, d in dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.md")):
            try:
                t = f.read_text(encoding="utf-8")
            except OSError:
                continue
            m = FM.match(t)
            fm = m.group(1) if m else ""
            mm = re.search(r"^model:\s*(.+?)\s*$", fm, re.M)
            model = mm.group(1).strip().strip('"\'') if mm else None
            route = re.search(r"<!--\s*ocx-route:\s*([^\s]+)\s*-->", t)
            ocx = f.name.startswith("ocx-")
            out.append({"file": str(f), "name": f.stem, "scope": scope, "model": model, "ocx": ocx,
                        "real_model": route.group(1) if route else model, "managed_by": "opencodex" if ocx else None})
    return out


def unify_agents(files: list[str], model: str, root: str | None = None) -> dict:
    """把选中的子代理定义里的 model: 改成同一个（ocx-* 一律跳过）。每个文件都先备份。"""
    model = _check_model(model)
    known = {a["file"]: a for a in agents(root)}
    done, skipped = [], []
    for f in files:
        a = known.get(f)
        if not a or a["ocx"]:
            skipped.append(f)
            continue
        p = Path(f)
        t = p.read_text(encoding="utf-8")
        m = FM.match(t)
        if m:
            fm = m.group(1)
            fm2 = re.sub(r"^model:.*$", f"model: {model}", fm, flags=re.M) if re.search(r"^model:", fm, re.M) else fm + f"\nmodel: {model}"
            t2 = t[:m.start(1)] + fm2 + t[m.end(1):]
        else:
            t2 = f"---\nmodel: {model}\n---\n\n" + t
        backup(p, "agent-" + p.stem[:30])
        atomic_write(p, t2.encode("utf-8"))
        done.append(f)
    return {"changed": done, "skipped": skipped}


# ---------------- 迁移明文 key ----------------

def plaintext() -> list[dict]:
    """找出配置文件里的明文 key（只报位置和打码）。"""
    out = []
    cs = claude_state()
    for k, m in cs["plaintext"].items():
        out.append({"tool": "claude", "where": cs["file"], "field": f"env.{k}", "masked": m})
    for pr in codex_state()["providers"]:
        if pr["plaintext"]:
            out.append({"tool": "codex", "where": codex_state()["file"], "field": f"model_providers.{pr['id']}.experimental_bearer_token",
                        "masked": pr["plaintext"], "provider": pr["id"]})
    return out


def migrate(tool: str, field: str, key_name: str) -> dict:
    """把一个明文 key 挪进凭据管理器，换成取 key 的命令。先备份；新 key 能取出来才删明文。"""
    key_name = keystore.check_name(key_name)
    if tool == "claude":
        k = field.split(".", 1)[1]
        if k not in CLAUDE_SECRET_ENV:
            raise ValueError("不认识的字段")
        p = claude_file("user", None)
        s = _read_json(p)
        v = (s.get("env") or {}).get(k)
        if not v:
            raise ValueError("这个字段已经没有明文 key 了")
        keystore.put(key_name, v, f"Claude {k}（从 settings.json 迁移）")
        if keystore.get(key_name) != v:
            raise RuntimeError("存进凭据管理器后读回来不一致，没改配置")
        bid = backup(p, "migrate-claude")
        s["env"].pop(k)
        s["apiKeyHelper"] = keystore.helper_line(key_name)
        atomic_write(p, (json.dumps(s, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        return {"ok": True, "backup": bid, "file": str(p)}
    if tool == "codex":
        pid = field.split(".")[1]
        tk = _toml()
        p = codex_file("user", None)
        doc = tk.parse(p.read_text(encoding="utf-8"))
        t = doc["model_providers"][pid]
        v = str(t.get("experimental_bearer_token") or "")
        if not v:
            raise ValueError("这个供应商已经没有明文 key 了")
        keystore.put(key_name, v, f"Codex {pid}（从 config.toml 迁移）")
        if keystore.get(key_name) != v:
            raise RuntimeError("存进凭据管理器后读回来不一致，没改配置")
        r = set_codex("user", None, {}, provider_auth={"id": pid, "key": key_name, "drop_experimental_bearer_token": True}, label="migrate-codex")
        return {"ok": r["ok"], "backup": r["backup"], "file": r["file"]}
    raise ValueError("tool 只能是 claude / codex")


# ---------------- 测试连接 ----------------

def test_connection(tool: str, base_url: str | None, key_name: str | None, model: str | None, use_settings_key: bool = False) -> dict:
    """发一个最小请求，只回状态码 / 耗时 / 简短错误（会把 key 从错误文本里抹掉）。"""
    key = keystore.get(key_name) if key_name else None
    if not key and use_settings_key and tool == "claude":
        env = (_read_json(claude_file("user", None)).get("env") or {})
        key = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")
    if not key:
        return {"ok": False, "error": "没有可用的 key（官方登录用 OAuth / ChatGPT 登录，CC Msg 测不了；在 Claude 里 /status、Codex 里 /status 看）"}
    t0 = time.time()
    if tool == "claude":
        base = (base_url or "https://api.anthropic.com").rstrip("/")
        body = json.dumps({"model": model or "claude-haiku-4-5", "max_tokens": 1, "messages": [{"role": "user", "content": "ping"}]}).encode()
        req = urllib.request.Request(base + "/v1/messages", body, {"content-type": "application/json", "x-api-key": key,
                                                                  "authorization": "Bearer " + key, "anthropic-version": "2023-06-01"})
    else:
        base = (base_url or "https://api.openai.com/v1").rstrip("/")
        req = urllib.request.Request(base + "/models", headers={"authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            st, txt = r.status, r.read(400).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        st, txt = e.code, e.read(400).decode("utf-8", "replace")
    except Exception as e:
        st, txt = None, f"{type(e).__name__}: {e}"
    txt = txt.replace(key, "***")
    return {"ok": bool(st and 200 <= st < 300), "status": st, "ms": int((time.time() - t0) * 1000), "detail": txt[:300], "url": base}


# ---------------- 汇总给页面 ----------------

def overview(root: str | None = None) -> dict:
    try:
        from . import apps
        run = apps.run_status()
    except Exception:
        run = {}
    return {"claude": {"user": claude_state("user"), "project": claude_state("project", root) if root else None},
            "codex": {"user": codex_state("user"), "project": codex_state("project", root) if root else None},
            "keys": keystore.names(), "keystore": keystore.backend(), "ccswitch": ccswitch(), "proxies": proxies(),
            "agents": agents(root), "plaintext": plaintext(), "running": run, "backups": backups(20),
            "helper_example": keystore.helper_line("example")}
