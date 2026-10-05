# -*- coding: utf-8 -*-
"""会话管理器：把 Claude 桌面端 / Claude Code 和 Codex 的会话列在一张表里（借鉴 cc-switch 的会话管理）。

- 列表：每条会话标出「当前账号下看不看得见」以及看不见的原因（在别的账号里、没登记、在别的供应商桶、已归档）；
- 搜索：标题 / 第一句话 / 目录；也可以全文搜（在会话记录里找一段字，限时）；
- 恢复命令：claude --resume <会话> / codex resume <线程>；导出 Markdown；
- 批量归档 / 取消归档 / 删除：删除是挪进 ~/.agent-bridge/trash/<批次>/，带清单，可整批还原。
  改桌面端数据的操作都经 apps.run_or_defer：桌面端开着就排到它退出之后做。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from . import claude_sync, codex_unify, paths, transcripts

TRASH = paths.HOME / "trash"
HEAD = 256 * 1024
_title_cache: dict[str, tuple[float, str, str | None]] = {}     # 路径 → (mtime, 第一句话, cwd)


def _first_user(f: Path, agent: str) -> tuple[str, str | None]:
    """会话记录开头的第一句人话 + 工作目录。按 (路径, 修改时间) 缓存。"""
    try:
        mt = f.stat().st_mtime
    except OSError:
        return "", None
    c = _title_cache.get(str(f))
    if c and c[0] == mt:
        return c[1], c[2]
    text, cwd = "", None
    try:
        with open(f, "rb") as fh:
            data = fh.read(HEAD)
        for line in data.splitlines():
            try:
                d = json.loads(line)
            except Exception:
                continue
            if agent == "claude":
                cwd = cwd or d.get("cwd")
                es = transcripts.parse_claude(d)
            else:
                if d.get("type") == "session_meta":
                    cwd = (d.get("payload") or {}).get("cwd")
                es = transcripts.parse_codex(d)
            u = next((e["text"] for e in es if e["role"] == "user"
                      and not e["text"].startswith("This session is being continued from a previous conversation")), None)
            if u:
                text = " ".join(u.split())[:200]
                break
    except Exception:
        pass
    _title_cache[str(f)] = (mt, text, cwd)
    return text, cwd


def _q(s: str) -> str:
    return '"' + s.replace('"', '\\"') + '"' if re.search(r"[\s&()]", s or "") else s


# ---------- Claude ----------

def _claude_registry() -> dict[str, list[dict]]:
    """cliSessionId → [{folder key, path, data}]（每个账号文件夹各一份登记）。"""
    reg: dict[str, list[dict]] = {}
    root = claude_sync.sessions_root()
    if not root.exists():
        return reg
    for acct in root.iterdir():
        if not acct.is_dir():
            continue
        for org in acct.iterdir():
            if not org.is_dir():
                continue
            try:
                idx = set(json.loads((org / "archived-sessions.idx").read_text(encoding="utf-8")).get("archived") or [])
            except Exception:
                idx = set()
            for f in org.glob("local_*.json"):
                try:
                    d = json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    continue
                d["_archived"] = bool(d.get("isArchived")) or f.stem in idx
                reg.setdefault(d.get("cliSessionId") or f.stem, []).append(
                    {"key": f"{acct.name}/{org.name}", "path": str(f), "data": d})
    return reg


def claude_sessions() -> list[dict]:
    cur = claude_sync.current_key()
    reg = _claude_registry()
    out, seen = [], set()
    for f in (paths.CLAUDE_HOME / "projects").glob("*/*.jsonl"):
        sid = f.stem
        seen.add(sid)
        regs = reg.get(sid, [])
        mine = next((r for r in regs if r["key"] == cur), None)
        best = mine or max(regs, key=lambda r: r["data"].get("lastActivityAt", 0), default=None)
        d = best["data"] if best else {}
        first, cwd = _first_user(f, "claude")
        st = f.stat()
        if mine:
            why = "已归档" if d.get("_archived") else None
        elif regs:
            why = "在别的账号里"
        else:
            why = "没登记（命令行 / 子代理开的）"
        cwd = d.get("cwd") or cwd
        out.append({
            "agent": "claude", "id": sid, "title": d.get("title") or first or "（无标题）", "first": first,
            "cwd": cwd, "last": max(d.get("lastActivityAt", 0) / 1000, st.st_mtime), "size": st.st_size,
            "archived": bool(d.get("_archived")), "starred": bool(d.get("isStarred")),
            "registered": bool(regs), "accounts": sorted({r["key"].split("/")[0][:8] for r in regs}),
            "visible": bool(mine) and not d.get("_archived"), "why": why, "file": str(f),
            "resume": (f"cd {_q(cwd)} && " if cwd else "") + f"claude --resume {sid}",
        })
    # 登记了但记录文件已经不在的（点开也续不上）
    for sid, regs in reg.items():
        if sid in seen:
            continue
        d = max(regs, key=lambda r: r["data"].get("lastActivityAt", 0))["data"]
        out.append({"agent": "claude", "id": sid, "title": d.get("title") or "（无标题）", "first": "", "cwd": d.get("cwd"),
                    "last": d.get("lastActivityAt", 0) / 1000, "size": 0, "archived": bool(d.get("_archived")),
                    "starred": bool(d.get("isStarred")), "registered": True,
                    "accounts": sorted({r["key"].split("/")[0][:8] for r in regs}), "visible": False,
                    "why": "记录文件已不在", "file": None, "resume": None, "orphan": True})
    return out


# ---------- Codex ----------

def codex_sessions() -> list[dict]:
    db = codex_unify.state_db()
    if not db:
        return []
    cur, acct = codex_unify.current_provider(), codex_unify.current_account()["account_id"]
    conn = codex_unify._ro(db)
    cols = codex_unify._cols(conn)
    want = [c for c in ("id", "title", "cwd", "updated_at", "archived", "model_provider", "rollout_path",
                        "first_user_message", "thread_source", "creator_account_id", "is_pinned", "name") if c in cols]
    rows = [dict(zip(want, r)) for r in conn.execute(f"select {', '.join(want)} from threads")]
    conn.close()
    out = []
    for r in rows:
        f = Path(r["rollout_path"]) if r.get("rollout_path") else None
        exists = bool(f and f.exists())
        why = None
        if not exists:
            why = "记录文件已不在"
        elif (r.get("model_provider") or "openai") != cur:
            why = f"在供应商桶「{r.get('model_provider')}」里（当前是「{cur}」）"
        elif acct and r.get("creator_account_id") and r["creator_account_id"] != acct:
            why = "建它的是别的 ChatGPT 账号"
        elif r.get("archived"):
            why = "已归档"
        first = " ".join((r.get("first_user_message") or "").split())[:200]
        out.append({
            "agent": "codex", "id": r["id"], "title": r.get("name") or r.get("title") or first or "（无标题）",
            "first": first, "cwd": r.get("cwd"), "last": r.get("updated_at") or 0,
            "size": f.stat().st_size if exists else 0, "archived": bool(r.get("archived")),
            "starred": bool(r.get("is_pinned")), "registered": True, "provider": r.get("model_provider"),
            "source": r.get("thread_source"), "visible": why is None, "why": why,
            "file": str(f) if exists else None, "resume": (f"cd {_q(r['cwd'])} && " if r.get("cwd") else "") + f"codex resume {r['id']}",
        })
    return out


def list_all() -> dict:
    t0 = time.time()
    items = claude_sessions() + codex_sessions()
    items.sort(key=lambda x: -(x["last"] or 0))
    from . import apps
    return {"ok": True, "items": items, "ms": int((time.time() - t0) * 1000), "apps": apps.status(),
            "claude_account": claude_sync.current_key(), "codex_provider": codex_unify.current_provider()}


def fulltext(q: str, limit_s: float = 20) -> dict:
    """在会话记录里找一段字（大小写不敏感，按字节找 UTF-8），限时；返回命中的会话号。"""
    if not q.strip():
        return {"hits": [], "partial": False}
    needle = q.strip().lower().encode("utf-8")
    t0, hits, partial = time.time(), [], False
    files = [("claude", f) for f in (paths.CLAUDE_HOME / "projects").glob("*/*.jsonl")] + \
            [("codex", f) for f in codex_unify._rollouts()]
    files.sort(key=lambda x: -x[1].stat().st_mtime)
    for agent, f in files:
        if time.time() - t0 > limit_s:
            partial = True
            break
        try:
            with open(f, "rb") as fh:
                while chunk := fh.read(8 * 1024 * 1024):
                    if needle in chunk.lower():
                        sid = f.stem if agent == "claude" else (codex_unify._meta_provider(f)[0] or f.stem[-36:])
                        hits.append({"agent": agent, "id": sid})
                        break
        except OSError:
            continue
    return {"hits": hits, "partial": partial, "ms": int((time.time() - t0) * 1000)}


def export_md(agent: str, sid: str) -> dict:
    """整份会话导出成 Markdown（人话 + 回复 + 工具一行摘要），存到 ~/.agent-bridge/exports/。"""
    f = transcripts.claude_file(sid) if agent == "claude" else transcripts.codex_file(sid)
    if not f:
        return {"ok": False, "error": "找不到会话记录文件"}
    parse = transcripts.parse_claude if agent == "claude" else transcripts.parse_codex
    first, cwd = _first_user(f, agent)
    lines = [f"# {first[:60] or sid}", "", f"- 来源：{'Claude' if agent == 'claude' else 'Codex'} `{sid}`",
             f"- 目录：`{cwd or '—'}`", f"- 记录：`{f}`", f"- 导出：{datetime.now():%Y-%m-%d %H:%M}", ""]
    tools = 0
    with open(f, "rb") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            for e in parse(d):
                if e["role"] == "tool":
                    tools += 1
                    lines.append(f"> 🔧 {e['text']}")
                    continue
                if tools:
                    lines.append("")
                    tools = 0
                when = datetime.fromtimestamp(e["ts"]).strftime("%m-%d %H:%M") if e.get("ts") else ""
                lines += [f"## {'🧑 我' if e['role'] == 'user' else '🤖 回复'}  {when}", "", e["text"], ""]
    out = paths.HOME / "exports" / f"{agent}-{sid[:8]}-{datetime.now():%Y%m%d-%H%M%S}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    return {"ok": True, "path": str(out), "name": out.name, "text": "\n".join(lines)}


# ---------- 批量操作（要在桌面端退出后做；由 apps.run_or_defer 调） ----------

def _claude_set_archived(sid: str, on: bool) -> int:
    """只改当前账号里的登记：isArchived + archived-sessions.idx 两处一起改。"""
    cur = claude_sync.current_key()
    n = 0
    for r in _claude_registry().get(sid, []):
        if r["key"] != cur:
            continue
        p = Path(r["path"])
        d = json.loads(p.read_text(encoding="utf-8"))
        d["isArchived"] = on
        p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        idx = p.parent / "archived-sessions.idx"
        try:
            j = json.loads(idx.read_text(encoding="utf-8"))
        except Exception:
            j = {"v": 1, "archived": []}
        a = [x for x in j.get("archived", []) if x != p.stem] + ([p.stem] if on else [])
        if a or idx.exists():
            idx.write_text(json.dumps({**j, "archived": a}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        n += 1
    return n


def _codex_dated_dir(f: Path) -> Path:
    m = re.match(r"rollout-(\d{4})-(\d{2})-(\d{2})T", f.name)
    base = paths.CODEX_HOME / "sessions"
    return base / m.group(1) / m.group(2) / m.group(3) if m else base


def _codex_set_archived(conn, tid: str, on: bool) -> int:
    """和 Codex 自己归档一样：文件挪到 archived_sessions/（取消归档挪回 sessions/年/月/日/），库里改 archived 与路径。"""
    r = conn.execute("select rollout_path from threads where id=?", (tid,)).fetchone()
    if not r:
        return 0
    f = Path(r[0]) if r[0] else None
    new = None
    if f and f.exists():
        dst = (paths.CODEX_HOME / "archived_sessions") if on else _codex_dated_dir(f)
        dst.mkdir(parents=True, exist_ok=True)
        if f.parent.resolve() != dst.resolve():
            new = dst / f.name
            shutil.move(str(f), new)
    cols = codex_unify._cols(conn)
    sets, vals = ["archived=?"], [1 if on else 0]
    if "archived_at" in cols:
        sets.append("archived_at=?"), vals.append(int(time.time()) if on else None)
    if new:
        sets.append("rollout_path=?"), vals.append(str(new))
    conn.execute(f"update threads set {', '.join(sets)} where id=?", (*vals, tid))
    return 1


def batch(action: str, items: list[dict]) -> dict:
    """action: archive / unarchive / delete；items: [{agent, id}]。只处理一种 agent（由调用方按桌面端分开排队）。"""
    done, errors = 0, []
    trash_dir, manifest = None, []
    if action == "delete":
        trash_dir = TRASH / f"{datetime.now():%Y%m%d-%H%M%S}-{items[0]['agent'] if items else 'x'}"
        while trash_dir.exists():                 # 同一秒两批（Claude 一批、Codex 一批）别撞成一个目录
            trash_dir = trash_dir.with_name(trash_dir.name + "x")
        trash_dir.mkdir(parents=True)
    conn = None
    db = codex_unify.state_db()
    if any(i["agent"] == "codex" for i in items) and db:
        conn = sqlite3.connect(db, timeout=10)
    try:
        for it in items:
            try:
                if it["agent"] == "claude":
                    if action in ("archive", "unarchive"):
                        done += bool(_claude_set_archived(it["id"], action == "archive"))
                    elif action == "delete":
                        manifest.append(_claude_delete(it["id"], trash_dir))
                        done += 1
                elif conn is not None:
                    with conn:
                        if action in ("archive", "unarchive"):
                            done += _codex_set_archived(conn, it["id"], action == "archive")
                        elif action == "delete":
                            manifest.append(_codex_delete(conn, it["id"], trash_dir))
                            done += 1
            except Exception as e:
                errors.append(f"{it['agent']}:{it['id'][:8]} {type(e).__name__}: {e}")
    finally:
        if conn is not None:
            conn.close()
    if trash_dir:
        (trash_dir / "manifest.json").write_text(json.dumps(
            {"time": datetime.now().isoformat(timespec="seconds"), "items": manifest}, ensure_ascii=False, indent=1),
            encoding="utf-8")
    return {"ok": not errors, "done": done, "errors": errors, "trash": trash_dir.name if trash_dir else None}


def _move(src: Path, trash_dir: Path, moved: list) -> None:
    rel = f"{len(moved):04d}-{src.name}"
    shutil.move(str(src), trash_dir / rel)
    moved.append({"from": str(src), "to": rel})


def _claude_delete(sid: str, trash_dir: Path) -> dict:
    moved: list = []
    for f in (paths.CLAUDE_HOME / "projects").glob(f"*/{sid}.jsonl"):
        _move(f, trash_dir, moved)
        side = f.with_suffix("")                 # 同名目录：子代理记录、工具结果
        if side.is_dir():
            _move(side, trash_dir, moved)
    for r in _claude_registry().get(sid, []):    # 所有账号里的登记都挪走，免得换号同步又补回来
        _move(Path(r["path"]), trash_dir, moved)
    return {"agent": "claude", "id": sid, "moved": moved}


def _codex_delete(conn, tid: str, trash_dir: Path) -> dict:
    moved: list = []
    cur = conn.execute("select * from threads where id=?", (tid,))
    names = [c[0] for c in cur.description]
    row = cur.fetchone()
    if row and row[names.index("rollout_path")] and Path(row[names.index("rollout_path")]).exists():
        _move(Path(row[names.index("rollout_path")]), trash_dir, moved)
    if row:
        conn.execute("delete from threads where id=?", (tid,))
    return {"agent": "codex", "id": tid, "moved": moved, "row": dict(zip(names, row)) if row else None}


def trash_list() -> list[dict]:
    out = []
    if not TRASH.exists():
        return out
    for d in sorted(TRASH.iterdir(), reverse=True):
        try:
            m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append({"id": d.name, "time": m.get("time"), "restored": m.get("restored"),
                    "items": [{"agent": i["agent"], "id": i["id"]} for i in m.get("items", [])]})
    return out


def trash_restore(batch_id: str) -> dict:
    d = TRASH / batch_id
    if ".." in batch_id or not (d / "manifest.json").exists():
        return {"ok": False, "error": f"回收站里没有这一批：{batch_id}"}
    m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    restored, errors = 0, []
    db = codex_unify.state_db()
    conn = sqlite3.connect(db, timeout=10) if db and any(i["agent"] == "codex" for i in m["items"]) else None
    try:
        for it in m["items"]:
            for mv in it["moved"]:
                src, dst = d / mv["to"], Path(mv["from"])
                if not src.exists():
                    continue
                if dst.exists():
                    errors.append(f"{dst} 已经有了，没覆盖")
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), dst)
            if it["agent"] == "codex" and it.get("row") and conn is not None:
                row = it["row"]
                with conn:
                    have = codex_unify._cols(conn)
                    ks = [k for k in row if k in have]
                    conn.execute(f"insert or ignore into threads ({', '.join(ks)}) values ({', '.join('?' * len(ks))})",
                                 [row[k] for k in ks])
            restored += 1
    finally:
        if conn is not None:
            conn.close()
    m["restored"] = datetime.now().isoformat(timespec="seconds")
    (d / "manifest.json").write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": not errors, "restored": restored, "errors": errors}
