# -*- coding: utf-8 -*-
"""Codex 换号 / 换供应商后历史对话「不见了」的归桶修复（借鉴 cc-switch 的 codex_history_migration）。

Codex 的对话列表按两样东西筛：
- model_provider：每个对话记着建它时用的供应商 id（rollout 文件里 session_meta 那行 + state_*.sqlite 的 threads 表）。
  config.toml 的 model_provider 一改（哪怕只是 openai → OpenAI 大小写），老对话就全落在别的桶里，列表里看不见。
- creator_account_id / creator_user_id：state 库里记的建对话的账号。换了 ChatGPT 账号，老账号的对话不归你。

这里把老对话统一归到当前供应商、当前账号：
- 写之前：state 库整份备份（sqlite 在线备份）+ 每个要改的 rollout 文件原样备份 + 账本 ledger.json（每条线程原来的值）；
- 改 rollout 文件前后各核一次大小和修改时间，中途被 Codex 写过就跳过这个文件（不抢写）；改完把修改时间还原，列表顺序不变；
- creator_account_id 为空的老对话（这个字段出现之前建的）不动 —— 它们本来就不按账号筛；
- 可以按账本整次还原：线程字段改回原值；rollout 文件之后没再动过就换回备份，动过了就只把那一行的供应商改回去。

Codex 开着时不能改（它退出时会把内存里的状态写回），由 apps.run_or_defer 排到它退出之后做。

    python -m agent_bridge codex-unify [--dry-run] [--no-account]
    python -m agent_bridge codex-unify --list            历次账本
    python -m agent_bridge codex-unify --restore <账本>
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import sys
import tomllib
from datetime import datetime
from pathlib import Path

from . import paths

LEDGERS = paths.HOME / "codex-unify-bak"
DEFAULT_PROVIDER = "openai"        # config.toml 没写 model_provider 时 Codex 用的内建 id


# ---------- 当前状态 ----------

def current_provider() -> str:
    try:
        cfg = tomllib.loads((paths.CODEX_HOME / "config.toml").read_text(encoding="utf-8"))
        return str(cfg.get("model_provider") or DEFAULT_PROVIDER)
    except Exception:
        return DEFAULT_PROVIDER


def current_account() -> dict:
    """{account_id, user_id}：从 auth.json 现读（user_id 在 id_token 的声明里）。没登录 ChatGPT 就都是 None。"""
    try:
        t = json.loads((paths.CODEX_HOME / "auth.json").read_text(encoding="utf-8")).get("tokens") or {}
    except Exception:
        return {"account_id": None, "user_id": None}
    uid = None
    try:
        part = t["id_token"].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        a = claims.get("https://api.openai.com/auth") or {}
        uid = a.get("chatgpt_user_id") or a.get("user_id")
    except Exception:
        pass
    return {"account_id": t.get("account_id"), "user_id": uid}


def state_db() -> Path | None:
    """最新一代的 state_<N>.sqlite。"""
    c = []
    for f in paths.CODEX_HOME.glob("state_*.sqlite"):
        try:
            c.append((int(f.stem.split("_")[1]), f))
        except Exception:
            pass
    return max(c)[1] if c else None


def _ro(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=10)


def _cols(conn, table="threads") -> set[str]:
    return {r[1] for r in conn.execute(f"pragma table_info({table})")}


def _rollouts():
    for base in (paths.CODEX_HOME / "sessions", paths.CODEX_HOME / "archived_sessions"):
        if base.exists():
            yield from base.rglob("rollout-*.jsonl")


def _meta_provider(f: Path) -> tuple[str | None, str | None]:
    """(线程 id, session_meta 里的 model_provider)：只读第一行。"""
    try:
        with open(f, "rb") as fh:
            m = json.loads(fh.readline())
        if m.get("type") == "session_meta":
            p = m.get("payload") or {}
            return p.get("id"), p.get("model_provider")
    except Exception:
        pass
    return None, None


# ---------- 预演 ----------

def plan(fix_account: bool = True) -> dict:
    cur, acct = current_provider(), current_account()
    db = state_db()
    threads, by_provider, by_account = [], {}, {}
    if db:
        conn = _ro(db)
        cols = _cols(conn)
        has_acct = {"creator_account_id", "creator_user_id"} <= cols
        sel = "id, model_provider, title, archived, rollout_path, updated_at" + (
            ", creator_account_id, creator_user_id" if has_acct else "")
        for r in conn.execute(f"select {sel} from threads"):
            row = dict(zip(sel.replace(" ", "").split(","), r))
            p_bad = (row["model_provider"] or DEFAULT_PROVIDER) != cur
            a_bad = bool(fix_account and has_acct and acct["account_id"] and row.get("creator_account_id")
                         and row["creator_account_id"] != acct["account_id"])
            if p_bad:
                by_provider[row["model_provider"]] = by_provider.get(row["model_provider"], 0) + 1
            if a_bad:
                by_account[row["creator_account_id"]] = by_account.get(row["creator_account_id"], 0) + 1
            if p_bad or a_bad:
                threads.append({**row, "fix_provider": p_bad, "fix_account": a_bad})
        conn.close()
    # 库里没有、但 rollout 文件头还记着别的供应商的（老版本 Codex 留下的）也一起改
    in_db = {t["id"] for t in threads}
    files = []
    for f in _rollouts():
        tid, prov = _meta_provider(f)
        if prov and prov != cur:
            files.append({"path": str(f), "thread": tid, "provider": prov, "in_db": tid in in_db})
    threads.sort(key=lambda t: -(t.get("updated_at") or 0))
    return {"ok": True, "db": str(db) if db else None, "provider": cur, "account": acct["account_id"],
            "by_provider": by_provider, "by_account": by_account, "threads": threads, "files": files,
            "archived": sum(1 for t in threads if t.get("archived"))}


# ---------- 执行 ----------

def _rewrite_meta(text: str, to: str | None, only_from: set[str] | None, back: dict | None = None) -> tuple[str, int]:
    """把 session_meta 行的 model_provider 改成 to（only_from 给了就只改这些来源）；back={行号: 原值} 时按它改回。"""
    out, n = [], 0
    for i, seg in enumerate(text.splitlines(keepends=True)):
        body = seg.rstrip("\r\n")
        nl = seg[len(body):]
        if '"session_meta"' in body and '"model_provider"' in body:
            try:
                v = json.loads(body)
                pl = v.get("payload") if v.get("type") == "session_meta" else None
                if isinstance(pl, dict) and "model_provider" in pl:
                    if back is not None:
                        if str(i) in back:
                            pl["model_provider"] = back[str(i)]
                            body, n = json.dumps(v, ensure_ascii=False, separators=(",", ":")), n + 1
                    elif pl["model_provider"] != to and (only_from is None or pl["model_provider"] in only_from):
                        pl["model_provider"] = to
                        body, n = json.dumps(v, ensure_ascii=False, separators=(",", ":")), n + 1
            except Exception:
                pass
        out.append(body + nl)
    return "".join(out), n


def _orig_meta(text: str) -> dict:
    """{行号: 原 model_provider}：还原时用。"""
    r = {}
    for i, line in enumerate(text.splitlines()):
        if '"session_meta"' in line and '"model_provider"' in line:
            try:
                v = json.loads(line)
                if v.get("type") == "session_meta":
                    r[str(i)] = v["payload"]["model_provider"]
            except Exception:
                pass
    return r


def _stat(f: Path) -> list:
    s = f.stat()
    return [s.st_size, s.st_mtime_ns]


def _atomic_write(f: Path, data: str, keep_mtime_ns: int) -> None:
    tmp = f.with_name(f.name + ".agent-bridge.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(data)
    os.replace(tmp, f)
    os.utime(f, ns=(keep_mtime_ns, keep_mtime_ns))       # 修改时间还原：Codex 按它排序，不让老对话跳到最前


def run(dry: bool = False, fix_account: bool = True, only: list[str] | None = None) -> dict:
    """only：只改这几条线程（id）；不给就全改。"""
    p = plan(fix_account)
    if only is not None:
        keep = set(only)
        p["threads"] = [t for t in p["threads"] if t["id"] in keep]
        p["files"] = [f for f in p["files"] if f["thread"] in keep]
    if dry or not (p["threads"] or p["files"]):
        return dict(p, threads_n=len(p["threads"]), files_n=len(p["files"]), dry=dry, done=0)
    cur, acct = p["provider"], current_account()
    led_dir = LEDGERS / datetime.now().strftime("%Y%m%d-%H%M%S")
    led_dir.mkdir(parents=True)
    ledger = {"time": datetime.now().isoformat(timespec="seconds"), "to_provider": cur, "to_account": acct,
              "db": p["db"], "threads": [], "files": [], "skipped": []}

    # 1) rollout 文件：库里登记的路径 + 扫出来的文件，去重
    targets = {}
    for t in p["threads"]:
        if t["fix_provider"] and t.get("rollout_path"):
            targets[os.path.normcase(t["rollout_path"])] = t["rollout_path"]
    for f in p["files"]:
        targets.setdefault(os.path.normcase(f["path"]), f["path"])
    for i, fp in enumerate(sorted(targets.values())):
        f = Path(fp)
        if not f.exists():
            continue
        before = _stat(f)
        raw = f.read_bytes()
        text = raw.decode("utf-8")
        new, n = _rewrite_meta(text, cur, None)
        if not n:
            continue
        if _stat(f) != before:
            ledger["skipped"].append({"path": fp, "why": "改的时候 Codex 正在写它"})
            continue
        bak = led_dir / "files" / f"{i:04d}-{f.name}"
        bak.parent.mkdir(parents=True, exist_ok=True)
        bak.write_bytes(raw)
        if _stat(f) != before:
            ledger["skipped"].append({"path": fp, "why": "改的时候 Codex 正在写它"})
            continue
        _atomic_write(f, new, before[1])
        ledger["files"].append({"path": fp, "backup": bak.name, "orig": _orig_meta(text), "after": _stat(f)})

    # 2) state 库：先整份在线备份，再在一个事务里改
    if p["db"] and p["threads"]:
        db = Path(p["db"])
        src = sqlite3.connect(db, timeout=10)
        dst = sqlite3.connect(led_dir / db.name)
        src.backup(dst)
        dst.close()
        cols = _cols(src)
        with src:
            for t in p["threads"]:
                orig = {"model_provider": t["model_provider"]}
                sets, vals = [], []
                if t["fix_provider"]:
                    sets.append("model_provider=?"), vals.append(cur)
                if t["fix_account"]:
                    orig.update(creator_account_id=t.get("creator_account_id"), creator_user_id=t.get("creator_user_id"))
                    sets.append("creator_account_id=?"), vals.append(acct["account_id"])
                    if "creator_user_id" in cols and acct["user_id"]:
                        sets.append("creator_user_id=?"), vals.append(acct["user_id"])
                src.execute(f"update threads set {', '.join(sets)} where id=?", (*vals, t["id"]))
                ledger["threads"].append({"id": t["id"], "orig": orig, "title": t.get("title")})
        src.close()
    (led_dir / "ledger.json").write_text(json.dumps(ledger, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": True, "dry": False, "ledger": led_dir.name, "threads": len(ledger["threads"]),
            "files": len(ledger["files"]), "skipped": len(ledger["skipped"]), "done": len(ledger["threads"]),
            "provider": cur}


# ---------- 账本与还原 ----------

def list_ledgers() -> list[dict]:
    out = []
    if not LEDGERS.exists():
        return out
    for d in sorted(LEDGERS.iterdir(), reverse=True):
        try:
            led = json.loads((d / "ledger.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append({"id": d.name, "time": led.get("time"), "to_provider": led.get("to_provider"),
                    "threads": len(led.get("threads", [])), "files": len(led.get("files", [])),
                    "restored": led.get("restored")})
    return out


def restore(ledger_id: str) -> dict:
    d = LEDGERS / ledger_id
    if ".." in ledger_id or not (d / "ledger.json").exists():
        return {"ok": False, "error": f"没有这个账本：{ledger_id}"}
    led = json.loads((d / "ledger.json").read_text(encoding="utf-8"))
    files_back = partial = 0
    for f in led.get("files", []):
        fp = Path(f["path"])
        if not fp.exists():
            continue
        if _stat(fp) == f["after"]:
            # 改完之后没动过：整份换回备份
            mt = f["after"][1]
            shutil.copyfile(d / "files" / f["backup"], fp)
            os.utime(fp, ns=(mt, mt))
            files_back += 1
        else:
            # 之后又聊过：只把 session_meta 那几行的供应商改回去，新内容留着
            before = _stat(fp)
            new, n = _rewrite_meta(fp.read_text(encoding="utf-8"), None, None, back=f["orig"])
            if n and _stat(fp) == before:
                _atomic_write(fp, new, before[1])
                partial += 1
    rows = 0
    if led.get("db") and led.get("threads") and Path(led["db"]).exists():
        conn = sqlite3.connect(led["db"], timeout=10)
        with conn:
            for t in led["threads"]:
                ks = list(t["orig"])
                conn.execute(f"update threads set {', '.join(k + '=?' for k in ks)} where id=?",
                             (*[t["orig"][k] for k in ks], t["id"]))
                rows += 1
        conn.close()
    led["restored"] = datetime.now().isoformat(timespec="seconds")
    (d / "ledger.json").write_text(json.dumps(led, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": True, "restored": rows, "files": files_back, "partial": partial}


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    if "--list" in argv:
        for l in list_ledgers():
            print(f"{l['id']}  → {l['to_provider']}  线程 {l['threads']}  文件 {l['files']}"
                  + (f"  （{l['restored']} 已还原）" if l.get("restored") else ""))
        return 0
    from . import apps
    if "--restore" in argv:
        lid = argv[argv.index("--restore") + 1]
        r = apps.run_or_defer("codex", "codex-restore", {"ledger": lid}, f"还原 Codex 归桶 {lid}")
        print(r.get("hint") or json.dumps(r, ensure_ascii=False))
        return 0
    fix_account = "--no-account" not in argv
    p = plan(fix_account)
    print(f"当前供应商：{p['provider']}；当前账号：{p['account'] or '（没登录 ChatGPT）'}")
    print(f"要归桶的线程 {len(p['threads'])} 条（其中已归档 {p['archived']}）：按原供应商 {p['by_provider']}，按原账号 {p['by_account']}")
    print(f"rollout 文件头要改的 {len(p['files'])} 个")
    if "--dry-run" in argv or not (p["threads"] or p["files"]):
        return 0
    r = apps.run_or_defer("codex", "codex-unify", {"fix_account": fix_account}, "Codex 历史归桶")
    print(r.get("hint") or f"✓ 改了线程 {r['threads']} 条、文件 {r['files']} 个（跳过 {r['skipped']}），账本 {r['ledger']}。重启 Codex 生效。")
    return 0
