# -*- coding: utf-8 -*-
"""把 Codex 桌面端里你自己开的对话同步进 Claude 桌面端的会话列表（和 Codex 自动导入 Claude 会话反过来）。

    python -m agent_bridge codex-sync [--dry-run] [--days 14] [--cwd 项目目录] [--to 账号/组织] [--only 末6位,末6位]

- 只同步用户自己开的 Codex 对话（thread_source=user、桌面端建的）；从 Claude 导进 Codex 的、后台 exec 开的、子代理都不同步，免得来回套娃。
- 每个 Codex 对话对应一个固定的 Claude 会话（记在 ~/.agent-bridge/codex_to_claude.json）。Codex 那边又聊了就覆盖更新；
  你已经在 Claude 里接着聊过的（记录被 Claude 改过）就不再覆盖，免得冲掉你的新内容。
- 写 ~/.claude/projects/<项目>/<会话>.jsonl（对话文字，工具过程折成说明）+ 桌面端列表条目 local_*.json（当前账号 / 组织）。
- 同时在 Codex 的导入记录里登记这份文件「已导入 = 原来那个 Codex 对话」，免得 Codex 又把它当成 Claude 会话导回去成一份重复的。
  写这两处之前都先备份。
- 桌面端只在启动时读列表：同步完要重启 Claude 桌面端才看得到。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import claude_sync, codex_bridge as cb, paths

MAP = paths.HOME / "codex_to_claude.json"
IMPORTS = cb.CODEX_HOME / "external_agent_session_imports.json"
KEEP_KEYS = ("model", "effort", "sessionSettings", "permissionMode", "remoteMcpServersConfig",
             "chromePermissionMode", "cliBinaryPin")
PREFIX = "（Codex）"


def _sha(f: Path) -> str:
    return hashlib.sha256(f.read_bytes()).hexdigest()


def _load(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save(p: Path, data) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def claude_version() -> str:
    """最近一份 Claude Code 会话记录里的版本号（写进新记录，免得太老被认不出）。"""
    fs = sorted((cb.CLAUDE_HOME / "projects").glob("*/*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)[:5]
    for f in fs:
        try:
            with open(f, "rb") as fh:
                for line in fh:
                    v = json.loads(line).get("version")
                    if v:
                        return v
        except Exception:
            continue
    return "2.1.281"


def imported_from_claude() -> set[str]:
    """从 Claude 导进 Codex 的线程（不同步回去，免得套娃）。我们自己登记的「同步过去的那份」不算。"""
    d = _load(IMPORTS, {})
    ours = {str(Path(v.get("file", ""))).lower() for v in _load(MAP, {}).values()}
    return {r.get("imported_thread_id") for r in d.get("records") or []
            if str(r.get("source_path", "")).removeprefix("\\\\?\\").lower() not in ours}


def candidates(days: float, cwd: str | None) -> list[dict]:
    t = cb.titles()
    skip = imported_from_claude()
    since = time.time() - days * 86400
    want = str(Path(cwd).resolve()).lower() if cwd else None
    best: dict[str, Path] = {}
    for p in cb.rollouts():
        if p.stat().st_mtime < since:
            continue
        m = cb.meta_of(p)
        tid = m.get("id") or p.stem[-36:]
        if m.get("thread_source") != "user" or m.get("originator") not in ("Codex Desktop", "codex_work_desktop"):
            continue
        if tid in skip:
            continue
        mc = str(m.get("cwd") or "")
        if want and not (mc.lower() == want or mc.lower().startswith(want + "\\") or mc.lower().startswith(want + "/")):
            continue
        if tid not in best or p.stat().st_mtime > best[tid].stat().st_mtime:
            best[tid] = p
    return [{"thread": tid, "path": p, "title": t.get(tid) or "（无标题）", "mtime": p.stat().st_mtime,
             "cwd": cb.meta_of(p).get("cwd")} for tid, p in sorted(best.items(), key=lambda kv: -kv[1].stat().st_mtime)]


def plan(days: float = 14, cwd: str | None = None, target: str | None = None) -> dict:
    fs = claude_sync.folders()
    if not fs:
        return {"ok": False, "error": f"没找到 Claude 桌面端的会话列表目录：{claude_sync.sessions_root()}"}
    tgt = next((f for f in fs if f["key"] == target), None) if target else fs[0]
    if not tgt:
        return {"ok": False, "error": f"没有这个账号 / 组织文件夹：{target}"}
    mp = _load(MAP, {})
    rows = []
    for c in candidates(days, cwd):
        rec = mp.get(c["thread"])
        state = "new"
        if rec:
            jf = Path(rec.get("file", ""))
            if jf.exists() and rec.get("sha") and _sha(jf) != rec["sha"]:
                state = "diverged"          # 已在 Claude 里接着聊过：不覆盖
            elif rec.get("src_mtime", 0) >= c["mtime"] and jf.exists():
                state = "same"
            else:
                state = "update"
        rows.append(dict(c, state=state, path=str(c["path"])))
    return {"ok": True, "target": tgt, "rows": rows,
            "counts": {k: sum(1 for r in rows if r["state"] == k) for k in ("new", "update", "same", "diverged")}}


def _desktop_entry(tdir: Path, cli_sid: str, cwd: str, title: str, mtime: float, old_name: str | None) -> str:
    tmpl = {}
    fs = sorted(tdir.glob("local_*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    if fs:
        tmpl = _load(fs[0], {})
    name = old_name or f"local_{uuid.uuid4()}.json"
    f = tdir / name
    cur = _load(f, {}) if f.exists() else {}
    d = {k: tmpl[k] for k in KEEP_KEYS if k in tmpl}
    d.update(cur)                     # 已有条目（用户可能改过标题 / 归档）尽量保留
    ms = int(mtime * 1000)
    d.update({"sessionId": name[:-5], "cliSessionId": cli_sid, "cwd": cwd, "originCwd": cwd,
              "lastActivityAt": ms, "lastFocusedAt": max(ms, cur.get("lastFocusedAt", 0))})
    d.setdefault("createdAt", ms)
    d.setdefault("isArchived", False)
    if cur.get("titleSource") != "user" or not cur.get("title"):
        d.update({"title": PREFIX + title, "titleSource": "user"})
    _save(f, d)
    return name


def _mark_imported(jsonl: Path, codex_tid: str, title: str) -> bool:
    """在 Codex 的导入记录里登记：这份 Claude 会话就是那个 Codex 对话，不用再导。"""
    d = _load(IMPORTS, None)
    if not isinstance(d, dict):
        return False
    src = "\\\\?\\" + str(jsonl.resolve())
    recs = [r for r in d.get("records") or [] if r.get("source_path") != src]
    recs.append({"source_path": src, "content_sha256": _sha(jsonl), "imported_thread_id": codex_tid,
                 "imported_at": int(time.time()), "source_modified_at": jsonl.stat().st_mtime_ns,
                 "connector_names": [], "title": title})
    d["records"] = recs
    _save(IMPORTS, d)
    return True


def run(days: float = 14, cwd: str | None = None, target: str | None = None, dry: bool = False,
        only: list[str] | None = None) -> dict:
    """only：只同步这几个 Codex 对话（完整号或末几位）；不给就同步全部可同步的。"""
    p = plan(days, cwd, target)
    todo = [r for r in p.get("rows", []) if r["state"] in ("new", "update")] if p["ok"] else []
    if only is not None:
        keys = [k.strip().lower() for k in only if k.strip()]
        todo = [r for r in todo if any(r["thread"].lower() == k or r["thread"].lower().endswith(k) for k in keys)]
    if not p["ok"] or dry or not todo:
        return dict(p, done=0, dry=dry)
    tdir = Path(p["target"]["path"])
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = paths.HOME / "codex-sync-bak" / stamp
    shutil.copytree(tdir, bak / tdir.parent.name / tdir.name)
    if IMPORTS.exists():
        shutil.copy2(IMPORTS, bak / IMPORTS.name)
    mp = _load(MAP, {})
    ver = claude_version()
    done, errs = 0, []
    for r in todo:
        rec = mp.get(r["thread"], {})
        try:
            out = cb.build_claude(Path(r["path"]), sid=rec.get("session"), version=ver, title_prefix=PREFIX)
            jf = Path(out["file"])
            name = _desktop_entry(tdir, out["session"], out["cwd"], out["title"], r["mtime"], rec.get("local"))
            _mark_imported(jf, r["thread"], out["title"])
            mp[r["thread"]] = {"session": out["session"], "file": str(jf), "sha": _sha(jf), "src_mtime": r["mtime"],
                               "local": name, "folder": p["target"]["key"], "title": out["title"], "synced": time.time()}
            done += 1
        except Exception as e:
            errs.append(f"{r['title']}：{e!r}")
    _save(MAP, mp)
    return dict(p, done=done, errors=errs, dry=False, backup=str(bak))


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    import argparse
    ap = argparse.ArgumentParser(prog="agent_bridge codex-sync", description="把 Codex 对话同步进 Claude 桌面端列表")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--days", type=float, default=14, help="只同步最近几天动过的（缺省 14）")
    ap.add_argument("--cwd", help="只同步这个项目目录下的")
    ap.add_argument("--to", help="写进哪个账号 / 组织文件夹（缺省当前在用的）")
    ap.add_argument("--only", help="只同步这几个 Codex 对话（号或末 6 位，逗号分隔）")
    a = ap.parse_args(argv)
    if not a.dry_run:
        from . import apps
        if apps.running("claude"):          # 开着写会在它退出时被盖回去
            r = apps.run_or_defer("claude", "codex-sync", {"days": a.days, "root": a.cwd,
                                                           "only": a.only.split(",") if a.only else None},
                                  "把 Codex 对话同步进 Claude")
            print(r["hint"])
            return 0
    r = run(a.days, a.cwd, a.to, a.dry_run, a.only.split(",") if a.only else None)
    if not r["ok"]:
        print(r["error"])
        return 1
    lab = {"new": "新同步", "update": "更新", "same": "没变", "diverged": "已在 Claude 里接着聊过，不覆盖"}
    for x in r["rows"]:
        print(f"[{lab[x['state']]}] {x['title']}  ·  {x['thread'][-6:]}  ·  {x['cwd']}")
    c = r["counts"]
    print(f"\n新 {c['new']} / 更新 {c['update']} / 没变 {c['same']} / 不覆盖 {c['diverged']}"
          + ("（试跑，没写）" if r["dry"] else f"；已写 {r['done']} 个，备份在 {r.get('backup', '-')}"))
    for e in r.get("errors") or []:
        print("✗ " + e)
    if r["done"]:
        print("重启 Claude 桌面端就能在会话列表里看到（标题前带「（Codex）」）。")
    return 0
