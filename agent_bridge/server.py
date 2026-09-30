#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent-bridge 管理服务：Claude Code ↔ Codex 会话消息总线的本地网页界面。

    python -m agent_bridge                起服务并打开浏览器（默认 http://127.0.0.1:8765）
    python -m agent_bridge --port N       换端口；--no-browser 不自动开浏览器

功能：选项目 → 一键把总线装进项目（bus.py + Claude 钩子 + Codex 钩子 + git 排除）→ 自检；
看在线会话、实时消息流、以「用户」身份发消息（给 Codex 发时顺带 codex queue 叫醒）。
界面关掉不影响会话之间互发 —— 真正干活的是两边的钩子和项目里的信箱。

只用标准库（二维码用 qrcode 包，没装就只显示链接）。缺省只听 127.0.0.1；所有写操作要带 X-Msgbus 头（挡跨站请求）。
手机端：电脑界面 ⋯ →「手机端」按需打开局域网监听并出一次性配对二维码（3 分钟、只能用一次），
手机扫码后拿到 30 天的设备令牌（HttpOnly cookie）。局域网来的请求没有有效令牌一律拒绝；
手机只能看、发消息、派活，安装 / 卸载 / 配对 / 设置这些管理操作只认本机。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import tomllib
import uuid
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import paths
from . import transcripts

TOOL = paths.PKG                      # 程序本体（包目录）
DATA = paths.HOME                     # 每个用户自己的数据：~/.agent-bridge
BUS_SRC = TOOL / "bus.py"
INDEX = TOOL / "index.html"
REGISTRY = DATA / "projects.json"
PY = Path(sys.executable).as_posix()
CODEX_HOME = paths.CODEX_HOME
CLAUDE_HOME = paths.CLAUDE_HOME
EVENTS = ["SessionStart", "UserPromptSubmit", "PostToolUse"]
# Claude 多一个 Stop：会话闲下来时后台待命（asyncRewake），有消息退出码 2 叫醒会话
CLAUDE_EVENTS = EVENTS + ["Stop"]
REWAKE_TIMEOUT = 20 * 3600
CODEX_EVENT_KEYS = {"SessionStart": "session_start", "UserPromptSubmit": "user_prompt_submit",
                    "PostToolUse": "post_tool_use"}
_reg_lock = threading.Lock()


# ---------- 小工具 ----------

def md5_file(p: Path) -> str | None:
    try:
        return hashlib.md5(p.read_bytes()).hexdigest()
    except Exception:
        return None


def read_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return default
    except Exception as e:
        raise ValueError(f"{p} 不是合法 JSON：{e}")


def write_json(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, p)


def backup(p: Path, bak_dir: Path) -> str | None:
    if not p.exists():
        return None
    bak_dir.mkdir(parents=True, exist_ok=True)
    dst = bak_dir / f"{p.parent.name}-{p.name}.{datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(p, dst)
    return str(dst)


def norm_root(path: str) -> Path:
    p = Path(path.strip().strip('"')).expanduser()
    if not p.is_absolute():
        raise ValueError("请填绝对路径")
    p = p.resolve()
    if not p.is_dir():
        raise ValueError(f"目录不存在：{p}")
    return p


# ---------- 项目登记 ----------

def load_registry() -> list[dict]:
    return read_json(REGISTRY, [])


def save_registry(rows: list[dict]) -> None:
    write_json(REGISTRY, rows)


def find_bus_dir(root: Path) -> str | None:
    """已装的总线在哪：.msgbus/；早期版本装在 .temp/msgbus/ 的也认（别挪，挪了 Codex 钩子要重新信任）。"""
    for rel in (".msgbus", ".temp/msgbus"):
        if (root / rel / "bus.py").exists():
            return rel
    return None


# ---------- 钩子配置 ----------

def claude_cmd(bus_rel: str) -> str:
    return f'"{PY}" "$CLAUDE_PROJECT_DIR/{bus_rel}/bus.py" hook claude'


def codex_cmd(root: Path, bus_rel: str) -> str:
    return f'"{PY}" "{root.as_posix()}/{bus_rel}/bus.py" hook codex'


def is_ours(cmd: str, agent: str) -> bool:
    return "bus.py" in cmd and f"hook {agent}" in cmd and "msgbus" in cmd


def events_with_ours(cfg: dict, agent: str) -> set[str]:
    got = set()
    for ev, groups in (cfg.get("hooks") or {}).items():
        for g in groups or []:
            for h in g.get("hooks") or []:
                if is_ours(h.get("command", ""), agent):
                    got.add(ev)
    return got


def add_hooks(cfg: dict, cmd: str, agent: str) -> list[str]:
    hooks = cfg.setdefault("hooks", {})
    added = []
    have = events_with_ours(cfg, agent)
    for ev in (CLAUDE_EVENTS if agent == "claude" else EVENTS):
        if ev in have:
            continue
        entry = {"hooks": [{"type": "command", "command": cmd, "timeout": 10}]}
        if ev == "PostToolUse":
            entry = {"matcher": "*", **entry}
        if ev == "Stop":
            entry = {"hooks": [{
                "type": "command", "command": f"{cmd} --rewake --max-secs {REWAKE_TIMEOUT - 60}",
                "asyncRewake": True, "timeout": REWAKE_TIMEOUT,
                "rewakeMessage": "📨 msgbus：",
                "rewakeSummary": "msgbus：收到别的会话发来的消息"}]}
        hooks.setdefault(ev, []).append(entry)
        added.append(ev)
    return added


def remove_hooks(cfg: dict, agent: str) -> int:
    n = 0
    hooks = cfg.get("hooks") or {}
    for ev in list(hooks):
        new_groups = []
        for g in hooks[ev] or []:
            hs = [h for h in g.get("hooks") or [] if not is_ours(h.get("command", ""), agent)]
            n += len(g.get("hooks") or []) - len(hs)
            if hs:
                new_groups.append(dict(g, hooks=hs))
        if new_groups:
            hooks[ev] = new_groups
        else:
            del hooks[ev]
    return n


def claude_files(root: Path) -> list[Path]:
    return [root / ".claude" / "settings.json", root / ".claude" / "settings.local.json"]


def codex_trust(root: Path) -> dict:
    """读 ~/.codex/config.toml 里 hooks.state 的信任记录（只读，不替用户写）。"""
    path = str(root / ".codex" / "hooks.json")
    try:
        cfg = tomllib.loads((CODEX_HOME / "config.toml").read_text(encoding="utf-8"))
    except Exception:
        return {}
    state = ((cfg.get("hooks") or {}).get("state") or {})
    out = {}
    for ev, key in CODEX_EVENT_KEYS.items():
        out[ev] = any(k.lower().startswith(f"{path}:{key}:".lower()) and v.get("trusted_hash")
                      for k, v in state.items())
    return out


# ---------- 状态 ----------

def project_status(root: Path) -> dict:
    bus_rel = find_bus_dir(root)
    st = {"root": str(root), "name": root.name, "bus_dir": bus_rel, "exists": root.is_dir()}
    ev_claude = set()
    claude_where = []
    for f in claude_files(root):
        try:
            got = events_with_ours(read_json(f, {}), "claude")
        except ValueError as e:
            st["error"] = str(e)
            got = set()
        if got:
            claude_where.append(f.name)
        ev_claude |= got
    try:
        ev_codex = events_with_ours(read_json(root / ".codex" / "hooks.json", {}), "codex")
    except ValueError as e:
        st["error"] = str(e)
        ev_codex = set()
    trust = codex_trust(root)
    st.update({
        "claude_hooks": sorted(ev_claude),
        "claude_files": claude_where,
        "codex_hooks": sorted(ev_codex),
        "codex_trusted": sorted(ev for ev, ok in trust.items() if ok),
        "bus_outdated": bool(bus_rel) and md5_file(root / bus_rel / "bus.py") != md5_file(BUS_SRC),
    })
    st["installed"] = bool(bus_rel) and len(ev_claude) == len(CLAUDE_EVENTS) and len(ev_codex) == len(EVENTS)
    st["claude_total"], st["codex_total"] = len(CLAUDE_EVENTS), len(EVENTS)
    return st


# ---------- 安装 / 卸载 / 自检 ----------

def git_exclude(root: Path, patterns: list[str]) -> list[str]:
    """把总线目录等加进 .git/info/exclude（不改 .gitignore，不进版本库）。"""
    if not (root / ".git").exists():
        return []
    try:
        r = subprocess.run(["git", "-C", str(root), "rev-parse", "--git-path", "info/exclude"],
                           capture_output=True, text=True, timeout=10)
        ex = Path(r.stdout.strip())
        if not ex.is_absolute():
            ex = root / ex
    except Exception:
        return []
    added = []
    cur = ex.read_text(encoding="utf-8", errors="replace") if ex.exists() else ""
    lines = set(cur.splitlines())
    for pat in patterns:
        # 已被 git 跟踪的文件不排除（排除了也没用，还会误导）
        tracked = subprocess.run(["git", "-C", str(root), "ls-files", "--error-unmatch", pat.rstrip("/")],
                                 capture_output=True, timeout=10).returncode == 0
        if pat not in lines and not tracked:
            added.append(pat)
    if added:
        ex.parent.mkdir(parents=True, exist_ok=True)
        with open(ex, "a", encoding="utf-8", newline="\n") as f:
            if cur and not cur.endswith("\n"):
                f.write("\n")
            f.write("# agent-bridge\n" + "\n".join(added) + "\n")
    return added


def install(root: Path) -> dict:
    log = []
    bus_rel = find_bus_dir(root) or ".msgbus"
    bus_dir = root / bus_rel
    bak = bus_dir / "bak"
    bus_dir.mkdir(parents=True, exist_ok=True)
    if md5_file(bus_dir / "bus.py") != md5_file(BUS_SRC):
        b = backup(bus_dir / "bus.py", bak)
        shutil.copy2(BUS_SRC, bus_dir / "bus.py")
        log.append(f"写入 {bus_rel}/bus.py" + (f"（旧版备份 {Path(b).name}）" if b else ""))
    else:
        log.append(f"{bus_rel}/bus.py 已是最新")

    # Claude：已经挂在哪个文件就留在哪；没挂过的挂到 settings.local.json（个人配置，一般不进库）
    have_any = [f for f in claude_files(root) if events_with_ours(read_json(f, {}), "claude")]
    target = have_any[0] if have_any else root / ".claude" / "settings.local.json"
    cfg = read_json(target, {})
    added = add_hooks(cfg, claude_cmd(bus_rel), "claude")
    if added:
        b = backup(target, bak)
        write_json(target, cfg)
        log.append(f"Claude 钩子加到 .claude/{target.name}：{', '.join(added)}" + (f"（原件备份 {Path(b).name}）" if b else ""))
    else:
        log.append(f"Claude 钩子已齐（.claude/{target.name}）")

    # Codex：只补缺的，已有的一字不动（改了命令 Codex 会要求重新信任）
    cx = root / ".codex" / "hooks.json"
    existed = cx.exists()
    cfg = read_json(cx, {})
    added = add_hooks(cfg, codex_cmd(root, bus_rel), "codex")
    if added:
        b = backup(cx, bak)
        write_json(cx, cfg)
        log.append(f"Codex 钩子加到 .codex/hooks.json：{', '.join(added)}" + (f"（原件备份 {Path(b).name}）" if b else ""))
    else:
        log.append("Codex 钩子已齐（.codex/hooks.json）")

    pats = [bus_rel.rstrip("/") + "/", ".claude/settings.local.json"]
    if not existed:
        pats.append(".codex/hooks.json")
    ex = git_exclude(root, pats)
    if ex:
        log.append("加进 .git/info/exclude：" + ", ".join(ex))

    with _reg_lock:
        rows = load_registry()
        if not any(r["root"].lower() == str(root).lower() for r in rows):
            rows.append({"root": str(root)})
            save_registry(rows)
    st = project_status(root)
    if len(st["codex_trusted"]) < 3:
        log.append("⚠ 还差一步：在 Codex 里打开这个项目，进钩子管理（/hooks）把 3 个 msgbus 钩子设为信任。")
    log.append("正在跑的 Claude 会话一般不用重开就能接上；如果没接上，重开一次会话。")
    return {"log": log, "status": st, "selftest": selftest(root)}


def uninstall(root: Path, purge: bool) -> dict:
    log = []
    bus_rel = find_bus_dir(root)
    bak = root / (bus_rel or ".msgbus") / "bak"
    for f in claude_files(root) + [root / ".codex" / "hooks.json"]:
        cfg = read_json(f, None)
        if cfg is None:
            continue
        agent = "codex" if f.parent.name == ".codex" else "claude"
        n = remove_hooks(cfg, agent)
        if n:
            backup(f, bak if not purge else DATA / "bak")
            if f.parent.name == ".codex" and not cfg.get("hooks"):
                f.unlink()
                log.append("删除 .codex/hooks.json（只剩 msgbus 钩子）")
            else:
                if not cfg.get("hooks"):
                    cfg["hooks"] = {}
                write_json(f, cfg)
                log.append(f"从 {f.parent.name}/{f.name} 去掉 {n} 个钩子")
    if purge and bus_rel:
        shutil.rmtree(root / bus_rel)
        log.append(f"删除 {bus_rel}/（含消息记录）")
    elif bus_rel:
        log.append(f"保留 {bus_rel}/（消息记录与备份都在里面）")
    return {"log": log or ["没有找到 msgbus 钩子"], "status": project_status(root)}


def run_bus(root: Path, args: list[str], stdin: str | None = None, cwd: Path | None = None,
            timeout=60) -> subprocess.CompletedProcess:
    bus_rel = find_bus_dir(root)
    if not bus_rel:
        raise ValueError("这个项目还没装总线")
    env = dict(os.environ, PYTHONUTF8="1")
    for k in ("CLAUDE_CODE_SESSION_ID", "CODEX_SESSION_ID", "CODEX_THREAD_ID"):
        env.pop(k, None)
    return subprocess.run([sys.executable, str(root / bus_rel / "bus.py")] + args,
                          input=(stdin or "").encode("utf-8"), capture_output=True,
                          cwd=str(cwd or root), env=env, timeout=timeout)


def selftest(root: Path) -> dict:
    """在项目的真总线上走一遍：两个假会话上线 → 互发 → 钩子收到 → 清掉痕迹。"""
    bus_rel = find_bus_dir(root)
    if not bus_rel:
        return {"ok": False, "steps": ["没装总线"]}
    bus_dir = root / bus_rel
    tag = uuid.uuid4().hex[:8]
    a, b = f"selftest-a-{tag}", f"selftest-b-{tag}"
    steps, ok = [], True

    def hook(agent, sid, ev):
        inp = json.dumps({"session_id": sid, "cwd": str(root), "hook_event_name": ev})
        r = run_bus(root, ["hook", agent], stdin=inp)
        return r.stdout.decode("utf-8", "replace")

    try:
        out = hook("claude", a, "SessionStart")
        good = "消息总线" in out
        steps.append(("✓" if good else "✗") + " Claude 钩子能跑、上线时注入用法说明")
        ok &= good
        hook("codex", b, "SessionStart")
        r = run_bus(root, ["--as", f"claude:{a}", "send", "--to", b, "--no-wake", f"自检 {tag}"])
        good = r.returncode == 0
        steps.append(("✓" if good else "✗") + " Claude → Codex 发送" + ("" if good else "：" + r.stderr.decode("utf-8", "replace")[-200:]))
        ok &= good
        out = hook("codex", b, "PostToolUse")
        good = f"自检 {tag}" in out
        steps.append(("✓" if good else "✗") + " Codex 钩子收到并注入")
        ok &= good
        out2 = hook("codex", b, "PostToolUse")
        good = f"自检 {tag}" not in out2
        steps.append(("✓" if good else "✗") + " 已读不重复推")
        ok &= good
        r = run_bus(root, ["--as", f"codex:{b}", "send", "--to", a, f"回信 {tag}"])
        out = hook("claude", a, "UserPromptSubmit")
        good = f"回信 {tag}" in out
        steps.append(("✓" if good else "✗") + " Codex → Claude 回信")
        ok &= good
        # 待命：模拟 Claude 闲下来（Stop 钩子后台跑），再发一条，看它是否以退出码 2 结束并带出内容
        env = dict(os.environ, PYTHONUTF8="1")
        for k in ("CLAUDE_CODE_SESSION_ID", "CODEX_SESSION_ID", "CODEX_THREAD_ID"):
            env.pop(k, None)
        lp = subprocess.Popen([sys.executable, str(bus_dir / "bus.py"), "hook", "claude", "--rewake", "--max-secs", "20"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(root), env=env)
        lp.stdin.write(json.dumps({"session_id": a, "cwd": str(root), "hook_event_name": "Stop"}).encode())
        lp.stdin.close()
        time.sleep(2)
        run_bus(root, ["--as", f"codex:{b}", "send", "--to", a, "--no-wake", f"叫醒 {tag}"])
        try:
            _, err = lp.communicate(timeout=15)
            good = lp.returncode == 2 and f"叫醒 {tag}" in err.decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            lp.kill()
            good = False
        steps.append(("✓" if good else "✗") + " Claude 闲着时待命钩子被消息叫醒（退出码 2）")
        ok &= good
        inp = json.dumps({"session_id": f"outside-{tag}", "cwd": str(Path.home()), "hook_event_name": "SessionStart"})
        r = run_bus(root, ["hook", "claude"], stdin=inp, cwd=Path.home())
        good = not r.stdout.strip()
        steps.append(("✓" if good else "✗") + " 项目以外的会话不理")
        ok &= good
    except Exception as e:
        steps.append(f"✗ 出错：{e!r}")
        ok = False
    finally:
        # 清痕迹：假会话的在线记录与读到位置。自检消息留在信箱里不删 ——
        # 重写信箱会和正在追加的会话抢，可能冲掉别人的消息；它们只发给假会话，真会话收不到，界面也不显示
        for d in ("sessions", "cursor", "listen"):
            for p in (bus_dir / d).glob(f"*selftest-*-{tag}*"):
                p.unlink(missing_ok=True)
            for p in (bus_dir / d).glob(f"*outside-{tag}*"):
                p.unlink(missing_ok=True)
    return {"ok": ok, "steps": steps}


# ---------- 消息 / 会话 / 发送 ----------

def read_messages(root: Path, since: int) -> dict:
    bus_rel = find_bus_dir(root)
    if not bus_rel:
        return {"messages": [], "offset": 0}
    p = root / bus_rel / "messages.jsonl"
    try:
        size = p.stat().st_size
    except FileNotFoundError:
        return {"messages": [], "offset": 0}
    if since < 0 or since > size:        # 首次或信箱被改短：取最后 300 行
        data = p.read_bytes()
        lines = data.splitlines(keepends=True)[-300:]
        start = size - sum(len(l) for l in lines)
    else:
        with open(p, "rb") as f:
            f.seek(since)
            data = f.read(size - since)
        lines = data.splitlines(keepends=True)
        start = since
    out, consumed = [], 0
    for l in lines:
        if not l.endswith(b"\n"):
            break
        consumed += len(l)
        try:
            m = json.loads(l)
        except Exception:
            continue
        if not str(m.get("from", {}).get("sid", "")).startswith("selftest-"):
            out.append(m)
    return {"messages": out, "offset": start + consumed, "reset": since < 0 or since > size}


ROLE_WINDOW = 24 * 3600


def session_roles(root: Path) -> dict:
    """按最近 24 小时的派活关系给每个会话定「主 / 辅」：
    主 = 往外派活的一方（指挥）；辅 = 接别人派的活的一方（干活），或被某个会话认领的 Codex 对话。
    两样都有时按「现在还没回来的活」判：有派出去没回的 → 主；有手上没交的 → 辅。"""
    bus_rel = find_bus_dir(root)
    if not bus_rel:
        return {}
    p = root / bus_rel / "messages.jsonl"
    try:
        lines = p.read_bytes().splitlines()
    except FileNotFoundError:
        return {}
    now = time.time()
    tasks, done = {}, set()
    for raw in lines:
        try:
            m = json.loads(raw)
        except Exception:
            continue
        if now - m.get("ts", 0) > ROLE_WINDOW or str(m.get("from", {}).get("sid", "")).startswith("selftest-"):
            continue
        if m.get("kind") == "task":
            tasks[m["id"]] = m
        elif m.get("kind") == "result" and m.get("reply_to"):
            done.add(m["reply_to"])
    # 总线新开的 Codex 对话：任务记在 workers/<任务号>.json 里的对话号上
    workers = {}
    for w in (root / bus_rel / "workers").glob("*.json") if (root / bus_rel / "workers").exists() else []:
        d = read_json(w, {})
        if d.get("task") and d.get("thread"):
            workers.setdefault(d["task"], []).append("codex-" + d["thread"])
    st: dict[str, dict] = {}

    def s(k):
        return st.setdefault(k, {"out": 0, "out_open": 0, "in": 0, "in_open": 0, "boss": None, "helpers": set()})
    for tid, m in tasks.items():
        f = m["from"]
        fk = f"{f['agent']}-{f['sid']}"
        tos = list(m.get("to_keys") or []) + workers.get(tid, [])
        open_ = tid not in done
        s(fk)["out"] += 1
        s(fk)["out_open"] += open_
        for tk in tos:
            s(tk)["in"] += 1
            s(tk)["in_open"] += open_
            if open_ or not s(tk)["boss"]:
                s(tk)["boss"] = f.get("name")
            s(fk)["helpers"].add(tk)
    out = {}
    for k, v in st.items():
        if v["out_open"] and not v["in_open"]:
            role = "main"
        elif v["in_open"] and not v["out_open"]:
            role = "aux"
        elif v["out_open"] and v["in_open"]:
            role = "main" if v["out_open"] >= v["in_open"] else "aux"
        else:
            role = "main" if v["out"] >= v["in"] and v["out"] else ("aux" if v["in"] else "")
        if role == "main":
            note = f"派出 {v['out_open']} 件在做" if v["out_open"] else f"24 小时内派过 {v['out']} 件"
        elif role == "aux":
            boss = str(v["boss"] or "").rsplit(" [", 1)[0]
            note = (f"替 {boss} 干活（{v['in_open']} 件没交）" if v["in_open"] else f"替 {boss} 干过活") if boss else "接过活"
        else:
            note = ""
        out[k] = {"role": role, "role_note": note}
    return out


def sessions(root: Path) -> list[dict]:
    r = run_bus(root, ["who", "--json"])
    try:
        rows = json.loads(r.stdout.decode("utf-8"))
    except Exception:
        return []
    now = time.time()
    roles = session_roles(root)
    for x in rows:
        x["listening"] = now - x.get("listening", 0) <= 150
        x.update(roles.get(f"{x.get('agent')}-{x.get('sid')}", {}))
        if not x.get("role") and x.get("owner_name"):        # 被认领的 Codex 对话：没活时也算辅
            x["role"], x["role_note"] = "aux", "归 " + str(x["owner_name"]).rsplit(" [", 1)[0] + " 用"
        if x.get("role_set") in ("main", "aux"):            # 用户手动设的优先
            auto_note = x.get("role_note") or ""
            x["role"] = x["role_set"]
            x["role_note"] = ("手动设为主会话" if x["role_set"] == "main" else "手动设为辅会话") + \
                (f"（{auto_note}）" if auto_note else "")
            x["role_manual"] = True
    return [{k: v for k, v in x.items() if k in ("agent", "sid", "short", "name", "title", "alias", "age", "originator",
                                                  "listening", "owner_name", "managed", "role", "role_note",
                                                  "role_manual", "role_set", "ignore")}
            for x in rows if x.get("agent") != "user"]


def send(root: Path, to: str, text: str, wake: bool, kind: str = "msg", who: str = "ui") -> dict:
    cmd = "task" if kind == "task" else "send"
    args = ["--as", f"user:{who}", cmd, "--to", to, "--multi"] + ([] if wake else ["--no-wake"]) + ["-"]
    r = run_bus(root, args, stdin=text, timeout=180)
    return {"ok": r.returncode == 0,
            "output": (r.stdout + r.stderr).decode("utf-8", "replace").strip()}


def suggest_projects() -> list[str]:
    """从最近的 Claude / Codex 会话里捡工作目录，当添加项目时的候选。"""
    seen: dict[str, float] = {}
    for d in (CLAUDE_HOME / "projects").glob("*"):
        files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:1]
        for f in files:
            try:
                with open(f, "rb") as fh:
                    for _ in range(40):
                        line = fh.readline()
                        if not line:
                            break
                        if b'"cwd"' in line:
                            cwd = json.loads(line).get("cwd")
                            if cwd:
                                seen[cwd] = max(seen.get(cwd, 0), f.stat().st_mtime)
                            break
            except Exception:
                pass
    for i in range(14):
        day = datetime.fromtimestamp(time.time() - i * 86400)
        for f in (CODEX_HOME / "sessions" / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}").glob("rollout-*.jsonl"):
            try:
                with open(f, "rb") as fh:
                    cwd = json.loads(fh.readline()).get("payload", {}).get("cwd")
                if cwd:
                    seen[cwd] = max(seen.get(cwd, 0), f.stat().st_mtime)
            except Exception:
                pass
    out, norm = [], set()
    for cwd, _ in sorted(seen.items(), key=lambda kv: -kv[1]):
        p = Path(cwd)
        key = str(p).lower()
        # 跳过临时 worktree（git 的 .git 是文件而不是目录的，多半是 worktree）
        if key in norm or not p.is_dir() or (p / ".git").is_file() or "worktree" in key:
            continue
        norm.add(key)
        out.append(str(p))
    return out[:30]


# ---------- 信件动画 ----------

SETTINGS = DATA / "settings.json"
FX = TOOL / "fx.py"


def load_settings() -> dict:
    s = {"fx": True}
    try:
        s.update(json.loads(SETTINGS.read_text(encoding="utf-8")))
    except Exception:
        pass
    return s


def save_settings(s: dict) -> None:
    write_json(SETTINGS, s)


def fx_directions(m: dict) -> list[str]:
    """要放哪几封：c2x Claude→Codex、x2c Codex→Claude、u2c 你→Claude、u2x 你→Codex。
    同类之间、自检不放；你群发给两边就各放一封。"""
    fa = m.get("from", {}).get("agent")
    if str(m.get("from", {}).get("sid", "")).startswith("selftest-"):
        return []
    if m.get("to_keys"):
        rec = {k.split("-", 1)[0] for k in m["to_keys"]}
    else:
        to = str(m.get("to", "")).lower()
        rec = {"claude", "codex"} if to == "all" else {to.split(":", 1)[0]}
    if fa == "claude":
        return ["c2x"] if "codex" in rec else []
    if fa == "codex":
        return ["x2c"] if "claude" in rec else []
    if fa == "user":
        return [d for d, a in (("u2c", "claude"), ("u2x", "codex")) if a in rec]
    return []


class FxWatcher:
    """盯着各项目信箱，有 Claude↔Codex 的消息就排队放一封信件动画（一次一封，最多排 6 封）。"""

    def __init__(self):
        self.offsets: dict[str, int] = {}
        self.queue: list[list[str]] = []
        self.cv = threading.Condition()

    def start(self):
        threading.Thread(target=self._watch, daemon=True, name="fx-watch").start()
        threading.Thread(target=self._play, daemon=True, name="fx-play").start()

    def play(self, direction: str, kind: str = "msg", label: str = ""):
        with self.cv:
            if len(self.queue) < 6:
                self.queue.append(["--dir", direction, "--kind", kind, "--label", label])
                self.cv.notify()

    def _play(self):
        while True:
            with self.cv:
                while not self.queue:
                    self.cv.wait()
                args = self.queue.pop(0)
            try:
                subprocess.run([sys.executable, str(FX)] + args, timeout=20,
                               env=dict(os.environ, PYTHONUTF8="1"), capture_output=True)
            except Exception:
                pass

    def _watch(self):
        while True:
            time.sleep(0.6)
            try:
                if sys.platform != "win32" or not load_settings().get("fx", True):
                    self.offsets.clear()          # 关着的时候不补放，重新打开从当下算起
                    continue
                for r in load_registry():
                    root = Path(r["root"])
                    bus_rel = find_bus_dir(root) if root.is_dir() else None
                    if not bus_rel:
                        continue
                    p = root / bus_rel / "messages.jsonl"
                    try:
                        size = p.stat().st_size
                    except FileNotFoundError:
                        continue
                    key = str(p)
                    if key not in self.offsets or size < self.offsets[key]:
                        self.offsets[key] = size          # 第一次见到：从当下开始，不补放旧消息
                        continue
                    if size == self.offsets[key]:
                        continue
                    with open(p, "rb") as f:
                        f.seek(self.offsets[key])
                        chunk = f.read(size - self.offsets[key])
                    end = chunk.rfind(b"\n") + 1
                    self.offsets[key] += end
                    for line in chunk[:end].splitlines():
                        try:
                            m = json.loads(line)
                        except Exception:
                            continue
                        for d in fx_directions(m):
                            if d.startswith("u"):
                                name = "手机" if m["from"].get("sid") == "phone" else "电脑"
                            else:
                                name = str(m["from"].get("name", "")).rsplit(" [", 1)[0]
                            self.play(d, m.get("kind", "msg"), name)
            except Exception:
                pass


FX_WATCHER = FxWatcher()


# ---------- 会话记录 + 直接回复 ----------

def session_rec(root: Path, agent: str, sid: str) -> dict:
    bus_rel = find_bus_dir(root)
    return read_json(root / bus_rel / "sessions" / f"{agent}-{sid}.json", {}) if bus_rel else {}


def transcript(root: Path, agent: str, sid: str, since: int) -> dict:
    rec = session_rec(root, agent, sid)
    return transcripts.read(agent, sid, since, rec.get("transcript"))


codex_exe = paths.codex_exe


def reply(root: Path, agent: str, sid: str, text: str, kind: str, who: str) -> dict:
    """在会话视图里回复 / 派活。
    Codex 的「回复」用 codex queue 交成一条真正的用户输入；Claude 没有外部注入用户输入的口子，走总线（钩子送达、闲着会被叫醒）。
    派活两边都走总线 task（有任务号、done 回报）。"""
    rec = session_rec(root, agent, sid)
    short = sid[-6:] if agent == "codex" else sid[:6]
    target = f"{agent}:{short}"
    if kind == "task":
        return send(root, target, text, True, "task", who)
    if agent in ("claude", "cursor"):
        r = send(root, target, text, False, "msg", who)
        r["via"] = "bus"
        return r
    exe = codex_exe()
    if not exe:
        return {"ok": False, "output": "找不到 codex.exe"}
    r = subprocess.run([exe, "queue", "--thread", sid, "--message", text],
                       capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
    out = (r.stdout + r.stderr).decode("utf-8", "replace").strip()
    if r.returncode == 0:
        FX_WATCHER.play("u2x", "msg", "手机" if who == "phone" else "电脑")
    return {"ok": r.returncode == 0, "via": "codex-queue", "output": out or "已交给 Codex",
            "name": rec.get("title") or target}


# ---------- 两个桌面窗口在屏幕上的左右位置（网页据此把会话列表摆在同一侧） ----------

_layout_cache = {"t": 0.0, "v": None}


def window_layout() -> dict:
    """{claude_side: left|right, source: windows|default, claude/codex: 窗口矩形或 None}，缓存 2 秒。"""
    now = time.time()
    if _layout_cache["v"] and now - _layout_cache["t"] < 2:
        return _layout_cache["v"]
    v = {"claude_side": "left", "source": "default", "claude": None, "codex": None}
    if sys.platform == "win32":
        try:
            from . import fx
            fx._dpi_aware()
            w = fx.find_windows()
            v["claude"], v["codex"] = w.get("claude"), w.get("codex")
            cx = lambda r: (r[0] + r[2]) / 2
            if v["claude"] and v["codex"]:
                v["claude_side"] = "left" if cx(v["claude"]) <= cx(v["codex"]) else "right"
                v["source"] = "windows"
            elif v["claude"] or v["codex"]:
                # 只开了一个：它在屏幕哪半边，它的列表就放哪边
                sl, _, sr, _ = fx.screen_rect()
                mid = (sl + sr) / 2
                if v["claude"]:
                    v["claude_side"] = "left" if cx(v["claude"]) <= mid else "right"
                else:
                    v["claude_side"] = "right" if cx(v["codex"]) <= mid else "left"
                v["source"] = "windows"
        except Exception:
            pass
    _layout_cache.update(t=now, v=v)
    return v


# ---------- 手机端：局域网监听 + 扫码配对 ----------

import secrets
import socket

DEVICES = DATA / "devices.json"
PAIR_TTL = 180
TOKEN_DAYS = 30
COOKIE = "msgbus_tok"
_pairs: dict[str, dict] = {}          # 配对码 → {exp, used_by}
_dev_lock = threading.Lock()
LAN = {"srv": None, "ip": None, "port": None}
PORT = {"n": 8765}
# 手机（局域网）能用的地址；其余只认本机
PHONE_GET = {"/", "/api/ping", "/api/me", "/api/projects", "/api/status", "/api/messages", "/api/sessions",
             "/api/settings", "/api/codex", "/api/tasks", "/api/transcript", "/api/layout"}
PHONE_POST = {"/api/send", "/api/reply", "/api/export", "/api/role", "/api/codex/resume"}


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))          # 不真发包，只为拿到默认路由那块网卡的地址
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return socket.gethostbyname(socket.gethostname())


def _h(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


def load_devices() -> list[dict]:
    return read_json(DEVICES, [])


def device_of(tok: str | None) -> dict | None:
    if not tok:
        return None
    hv, now = _h(tok), time.time()
    with _dev_lock:
        devs = load_devices()
        for d in devs:
            if secrets.compare_digest(d["hash"], hv) and d["expires"] > now:
                if now - d.get("last_seen", 0) > 60:
                    d["last_seen"] = now
                    write_json(DEVICES, devs)
                return d
    return None


def start_lan() -> dict:
    if not LAN["srv"]:
        ip = lan_ip()
        srv = Server((ip, PORT["n"]), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True, name="lan").start()
        LAN.update(srv=srv, ip=ip, port=PORT["n"])
        s = load_settings()
        s["lan"] = True
        save_settings(s)
    return {"running": True, "ip": LAN["ip"], "port": LAN["port"]}


def stop_lan() -> None:
    srv = LAN["srv"]
    if srv:
        LAN["srv"] = None
        threading.Thread(target=srv.shutdown, daemon=True).start()
    s = load_settings()
    s["lan"] = False
    save_settings(s)


def qr_svg(text: str) -> str | None:
    try:
        import io
        import qrcode
        import qrcode.image.svg
        img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
        buf = io.BytesIO()
        img.save(buf)
        return buf.getvalue().decode("utf-8")
    except Exception:
        return None


def new_pair() -> dict:
    start_lan()
    now = time.time()
    for c in [c for c, v in _pairs.items() if v["exp"] < now]:
        _pairs.pop(c, None)
    code = secrets.token_urlsafe(18)
    _pairs[code] = {"exp": now + PAIR_TTL, "used_by": None}
    url = f"http://{LAN['ip']}:{LAN['port']}/pair?c={code}"
    return {"code": code, "url": url, "svg": qr_svg(url), "expires_in": PAIR_TTL}


def redeem_pair(code: str, ua: str) -> str | None:
    p = _pairs.get(code or "")
    if not p or p["used_by"] or p["exp"] < time.time():
        return None
    tok = secrets.token_urlsafe(32)
    name = "手机"
    ual = ua.lower()
    for k, v in (("iphone", "iPhone"), ("ipad", "iPad"), ("android", "Android"), ("harmony", "鸿蒙")):
        if k in ual:
            name = v
            break
    dev = {"id": secrets.token_hex(4), "hash": _h(tok), "name": name, "ua": ua[:160],
           "created": time.time(), "last_seen": time.time(), "expires": time.time() + TOKEN_DAYS * 86400}
    with _dev_lock:
        devs = load_devices()
        devs.append(dev)
        write_json(DEVICES, devs)
    p["used_by"] = dev["id"]
    return tok


def devices_public() -> list[dict]:
    return [{k: d[k] for k in ("id", "name", "created", "last_seen", "expires")} for d in load_devices()
            if d["expires"] > time.time()]


def revoke(dev_id: str | None) -> int:
    with _dev_lock:
        devs = load_devices()
        keep = [d for d in devs if dev_id and d["id"] != dev_id]
        write_json(DEVICES, keep)
        return len(devs) - len(keep)


PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>会话消息总线</title><body style="font:16px/1.6 system-ui,sans-serif;max-width:420px;margin:60px auto;padding:0 16px;text-align:center;color:#1c2127">
<div style="font-size:42px">📨</div><h2>{title}</h2><p style="color:#6a737d">{body}</p></body>"""


# ---------- HTTP ----------

# 浏览器刷新 / 切页 / 手机锁屏会中途断开请求，这时往回写包就会碰到这几种错，属正常现象
_GONE = (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)


class Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], _GONE):
            return  # 对方已经走了，不用在窗口里刷一屏报错
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    server_version = "agent-bridge/1"

    def log_message(self, fmt, *args):  # 安静点
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, e, code=400):
        if isinstance(e, _GONE):
            return  # 连接已断，回不了错误包
        self._json({"error": str(e)}, code)

    def _html(self, html: str, code=200, headers=()):
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _local(self) -> bool:
        return self.client_address[0] in ("127.0.0.1", "::1") and self.server.server_address[0] in ("127.0.0.1", "::1")

    def _cookie_tok(self) -> str | None:
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return v
        return None

    def _gate(self, path: str, method: str) -> bool:
        """本机放行；局域网来的要有效设备令牌，且只能用手机那几个地址。返回 False 表示已回绝。"""
        self.device = None
        if self._local():
            return True
        if method == "GET" and path == "/pair":
            return True
        dev = device_of(self._cookie_tok())
        if not dev:
            if path == "/":
                self._html(PAGE.format(title="还没配对", body="在电脑上打开会话消息总线，⋯ 菜单 →「手机端」，用手机扫二维码。"), 401)
            else:
                self._err("没有配对，或设备令牌已失效 / 被电脑端移除", 401)
            return False
        allowed = PHONE_GET if method == "GET" else PHONE_POST
        if path not in allowed:
            self._err("手机端不能做这个操作（只能在电脑上做）", 403)
            return False
        self.device = dev
        return True

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if not self._gate(u.path, "GET"):
            return
        try:
            if u.path == "/pair":
                tok = redeem_pair(q.get("c", ""), self.headers.get("User-Agent") or "")
                if not tok:
                    return self._html(PAGE.format(title="二维码已失效", body="配对码 3 分钟内有效、只能用一次。请在电脑上重新生成二维码再扫。"), 403)
                cookie = f"{COOKIE}={tok}; Path=/; Max-Age={TOKEN_DAYS * 86400}; HttpOnly; SameSite=Lax"
                return self._html('<meta http-equiv="refresh" content="0;url=/">配对成功，正在打开…', 200,
                                  [("Set-Cookie", cookie)])
            elif u.path == "/api/me":
                d = getattr(self, "device", None)
                self._json({"local": self._local(), "device": d and {"id": d["id"], "name": d["name"]}})
            elif u.path == "/api/claude-sync":
                from . import claude_sync
                self._json(claude_sync.plan())
            elif u.path == "/api/codex-sync":
                from . import codex_sync
                self._json(codex_sync.plan(float(q.get("days", 14)), q.get("root") or None))
            elif u.path == "/api/layout":
                self._json(window_layout())
            elif u.path == "/api/transcript":
                self._json(transcript(norm_root(q["root"]), q["agent"], q["sid"], int(q.get("since", -1))))
            elif u.path == "/api/lan":
                self._json({"running": bool(LAN["srv"]), "ip": LAN["ip"], "port": LAN["port"],
                            "devices": devices_public()})
            elif u.path == "/api/pair/status":
                p = _pairs.get(q.get("c", ""))
                self._json({"used": bool(p and p["used_by"]), "expired": (not p) or p["exp"] < time.time(),
                            "device": p and p["used_by"]})
            elif u.path == "/":
                body = INDEX.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/api/ping":
                self._json({"ok": True, "tool": str(TOOL)})
            elif u.path == "/api/projects":
                rows = []
                for r in load_registry():
                    p = Path(r["root"])
                    rows.append(project_status(p) if p.is_dir() else {"root": r["root"], "name": p.name, "exists": False})
                self._json(rows)
            elif u.path == "/api/suggest":
                self._json(suggest_projects())
            elif u.path == "/api/status":
                self._json(project_status(norm_root(q["root"])))
            elif u.path == "/api/messages":
                self._json(read_messages(norm_root(q["root"]), int(q.get("since", -1))))
            elif u.path == "/api/sessions":
                self._json(sessions(norm_root(q["root"])))
            elif u.path == "/api/settings":
                self._json(load_settings())
            elif u.path == "/api/codex":
                r = run_bus(norm_root(q["root"]), ["codex-status", "--json"])
                self._json(json.loads(r.stdout.decode("utf-8") or "{}"))
            elif u.path == "/api/tasks":
                r = run_bus(norm_root(q["root"]), ["tasks", "--json"])
                rows = json.loads(r.stdout.decode("utf-8") or "[]")
                self._json([x for x in rows if x["status"] == "等待中" or time.time() - x["ts"] < 86400][-30:])
            else:
                self._err("没有这个地址", 404)
        except Exception as e:
            self._err(e)

    def do_POST(self):
        if self.headers.get("X-Msgbus") != "1":
            return self._err("缺少 X-Msgbus 头", 403)
        path = urlparse(self.path).path
        if not self._gate(path, "POST"):
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            if path == "/api/export":
                root = norm_root(body["root"])
                out_dir = root / (find_bus_dir(root) or ".msgbus") / "exports"
                r = subprocess.run([sys.executable, str(TOOL / "codex_bridge.py"), "export", body["sid"], "--out", str(out_dir)],
                                   capture_output=True, timeout=120, env=dict(os.environ, PYTHONUTF8="1"))
                out = r.stdout.decode("utf-8", "replace").strip()
                if r.returncode != 0:
                    raise ValueError((out + r.stderr.decode("utf-8", "replace")).strip()[-400:])
                self._json(json.loads(out.splitlines()[-1]))
            elif path == "/api/codex/resume":
                r = run_bus(norm_root(body["root"]), ["codex-resume"])
                self._json({"ok": r.returncode == 0, "output": (r.stdout + r.stderr).decode("utf-8", "replace").strip()})
            elif path == "/api/role":
                who = "phone" if getattr(self, "device", None) else "ui"
                r = run_bus(norm_root(body["root"]), ["--as", f"user:{who}", "set-role",
                                                      f"{body['agent']}:{body['sid']}", body.get("role") or "auto"])
                out = (r.stdout + r.stderr).decode("utf-8", "replace").strip()
                if r.returncode != 0:
                    raise ValueError(out or "设置失败")
                self._json({"ok": True, "output": out})
            elif path == "/api/reply":
                text = (body.get("text") or "").strip()
                if not text:
                    raise ValueError("正文是空的")
                if body.get("agent") not in ("claude", "codex", "cursor") or not body.get("sid"):
                    raise ValueError("缺少会话")
                self._json(reply(norm_root(body["root"]), body["agent"], body["sid"], text, body.get("kind") or "msg",
                                 "phone" if getattr(self, "device", None) else "ui"))
            elif path == "/api/claude-sync":
                from . import claude_sync
                self._json(claude_sync.run())
            elif path == "/api/codex-sync":
                from . import codex_sync
                self._json(codex_sync.run(float(body.get("days") or 14), body.get("root") or None))
            elif path == "/api/pair/new":
                self._json(new_pair())
            elif path == "/api/lan/stop":
                stop_lan()
                self._json({"ok": True})
            elif path == "/api/devices/revoke":
                self._json({"removed": revoke(body.get("id"))})
            elif path == "/api/projects/add":
                root = norm_root(body["root"])
                with _reg_lock:
                    rows = load_registry()
                    if not any(r["root"].lower() == str(root).lower() for r in rows):
                        rows.append({"root": str(root)})
                        save_registry(rows)
                self._json(project_status(root))
            elif path == "/api/projects/remove":
                with _reg_lock:
                    key = str(Path(body["root"])).lower()   # 统一分隔符；目录可能已删，不走 norm_root
                    rows = [r for r in load_registry() if str(Path(r["root"])).lower() != key]
                    save_registry(rows)
                self._json({"ok": True})
            elif path == "/api/install":
                self._json(install(norm_root(body["root"])))
            elif path == "/api/uninstall":
                self._json(uninstall(norm_root(body["root"]), bool(body.get("purge"))))
            elif path == "/api/settings":
                s = load_settings()
                if "fx" in body:
                    s["fx"] = bool(body["fx"])
                save_settings(s)
                self._json(s)
            elif path == "/api/fx/test":
                FX_WATCHER.play(body.get("dir") or "c2x", body.get("kind") or "task", "试放")
                self._json({"ok": True})
            elif path == "/api/selftest":
                self._json(selftest(norm_root(body["root"])))
            elif path == "/api/send":
                text = (body.get("text") or "").strip()
                if not text:
                    raise ValueError("正文是空的")
                self._json(send(norm_root(body["root"]), body.get("to") or "all", text, bool(body.get("wake", True)),
                                body.get("kind") or "msg", "phone" if getattr(self, "device", None) else "ui"))
            else:
                self._err("没有这个地址", 404)
        except Exception as e:
            self._err(e)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="agent_bridge", description="agent-bridge：Claude ↔ Codex 会话消息总线管理界面")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args(argv)
    url = f"http://127.0.0.1:{a.port}/"
    PORT["n"] = a.port
    try:
        srv = Server(("127.0.0.1", a.port), Handler)
    except OSError:
        # 端口被占：多半是已经开着一个，直接打开它
        print(f"端口 {a.port} 已被占用，可能已经在运行：{url}")
        if not a.no_browser:
            webbrowser.open(url)
        return
    print(f"agent-bridge 已启动：{url}  （Ctrl+C 退出；关掉界面不影响会话之间互发消息）", flush=True)
    FX_WATCHER.start()      # Claude↔Codex 有消息时放信件动画（界面 ⋯ 菜单里可关）
    if load_settings().get("lan") and devices_public():
        try:                # 上次开着手机端、且还有配过的手机：接着开
            start_lan()
            print(f"手机端已开：http://{LAN['ip']}:{LAN['port']}/（只认已配对的手机）")
        except OSError as e:
            print(f"手机端没能打开：{e}")
    if not a.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
