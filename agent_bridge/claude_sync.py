# -*- coding: utf-8 -*-
"""Claude 桌面端会话列表跨账号同步。

桌面端的会话列表按「账号 / 组织」分文件夹记：<Claude 数据目录>/claude-code-sessions/<账号>/<组织>/local_*.json，
只显示当前登录的那个文件夹；对话内容本身在 ~/.claude/projects/**/<会话号>.jsonl，本地、不分账号。
切换账号后旧会话「不见了」只是列表文件在别的文件夹里。这里把别的文件夹里的会话列表文件补齐复制到目标文件夹：

- 只补缺的（同名文件已有就跳过），不覆盖、不删除；
- 对话记录文件已经不在的会话不复制（点开也续不上）；
- 写之前把目标文件夹整个备份到 ~/.agent-bridge/claude-sync-bak/<时间>/。

目标缺省是「最近有会话在写的那个文件夹」（= 当前登录的账号 / 组织）。桌面端要重启才会看到新补进来的会话。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

from . import paths


def claude_app_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Claude"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "Claude"


def sessions_root() -> Path:
    return claude_app_dir() / "claude-code-sessions"


def folders() -> list[dict]:
    """所有「账号 / 组织」文件夹：会话数、最近一次写入时间。"""
    root = sessions_root()
    out = []
    if not root.exists():
        return out
    for acct in root.iterdir():
        if not acct.is_dir():
            continue
        for org in acct.iterdir():
            if not org.is_dir():
                continue
            files = list(org.glob("local_*.json"))
            latest = max((f.stat().st_mtime for f in files), default=0)
            out.append({"key": f"{acct.name}/{org.name}", "path": str(org), "count": len(files), "latest": latest})
    return sorted(out, key=lambda x: -x["latest"])


def current_key() -> str | None:
    """当前登录的账号 / 组织：~/.claude.json 的 oauthAccount；没有就 None（退回「最近有写入的文件夹」）。"""
    try:
        o = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8")).get("oauthAccount") or {}
    except Exception:
        return None
    a, g = o.get("accountUuid"), o.get("organizationUuid")
    if a and g and (sessions_root() / a / g).is_dir():
        return f"{a}/{g}"
    return None


def _transcript_exists(cli_sid: str | None) -> bool:
    if not cli_sid:
        return False
    return any((paths.CLAUDE_HOME / "projects").glob(f"*/{cli_sid}.jsonl"))

_KEEP = ("isArchived", "isStarred", "lastFocusedAt")
# 旧账号被停用时留下的报错（「organization has disabled …」），带到新账号里会一直挂着
_DROP = ("error", "errorAt", "priorErrorMark")


def _fresh(d: dict) -> dict:
    return {k: v for k, v in d.items() if k not in _DROP}


def plan(target: str | None = None) -> dict:
    fs = folders()
    if not fs:
        return {"ok": False, "error": f"没找到 Claude 桌面端的会话列表目录：{sessions_root()}"}
    target = target or current_key()
    tgt = next((f for f in fs if f["key"] == target), None) if target else fs[0]
    if not tgt:
        return {"ok": False, "error": f"没有这个账号 / 组织文件夹：{target}"}
    tdir = Path(tgt["path"])
    have = {f.name for f in tdir.glob("local_*.json")}
    # 在当前账号里删过的（桌面端留 deleted_<会话号> 墓碑）不复活
    tomb = {f.name[len("deleted_"):] for f in tdir.glob("deleted_*")}
    have_cli = set()
    for f in tdir.glob("local_*.json"):
        try:
            have_cli.add(json.loads(f.read_text(encoding="utf-8")).get("cliSessionId"))
        except Exception:
            pass
    # 同一个会话可能在好几个旧账号文件夹里都有登记（换过不止一次号），挑 lastActivityAt 最新的那份；
    # 当前账号里已有、但别处有更新版本的，记进 update（之前按文件夹顺序取第一份，会补进旧版本）
    best: dict[str, tuple[Path, dict, str]] = {}
    for f in fs:
        if f["key"] == tgt["key"]:
            continue
        try:   # 归档有两处记：登记里的 isArchived 和文件夹里的 archived-sessions.idx
            idx = set(json.loads((Path(f["path"]) / "archived-sessions.idx").read_text(encoding="utf-8")).get("archived") or [])
        except Exception:
            idx = set()
        for src in Path(f["path"]).glob("local_*.json"):
            try:
                d = json.loads(src.read_text(encoding="utf-8"))
            except Exception:
                continue
            if src.stem in idx:
                d["isArchived"] = True
            old = best.get(src.name)
            if old is None or d.get("lastActivityAt", 0) > old[1].get("lastActivityAt", 0):
                best[src.name] = (src, d, f["key"])
    add, update, skip_missing = [], [], 0
    for name, (src, d, frm) in best.items():
        item = {"src": str(src), "name": name, "title": d.get("title") or "（无标题）",
                "cwd": d.get("cwd"), "from": frm, "archived": bool(d.get("isArchived")),
                "last": d.get("lastActivityAt", 0) / 1000}
        if name in have:
            try:
                cur = json.loads((tdir / name).read_text(encoding="utf-8"))
            except Exception:
                continue
            if d.get("lastActivityAt", 0) > cur.get("lastActivityAt", 0):
                update.append(item)
            continue
        if d.get("cliSessionId") in tomb or name[len("local_"):-len(".json")] in tomb:
            continue
        if d.get("cliSessionId") in have_cli:
            continue
        if not _transcript_exists(d.get("cliSessionId")):
            skip_missing += 1
            continue
        add.append(item)
    update.sort(key=lambda x: -x["last"])
    add.sort(key=lambda x: -x["last"])
    return {"ok": True, "target": tgt, "folders": fs, "add": add, "update": update, "skip_missing": skip_missing}


def run(target: str | None = None, dry: bool = False, only: list[str] | None = None) -> dict:
    """only：只补这几个（local_*.json 文件名）；不给就全补。"""
    p = plan(target)
    if p.get("ok") and only is not None:
        keep = set(only)
        p["add"] = [a for a in p["add"] if a["name"] in keep]
    if not p["ok"] or dry or not (p["add"] or p["update"]):
        return dict(p, done=0, updated=0, dry=dry)
    tdir = Path(p["target"]["path"])
    bak = _backup(tdir)
    done = 0
    for a in p["add"]:
        dst = tdir / a["name"]
        if dst.exists():
            continue
        dst.write_text(json.dumps(_fresh(json.loads(Path(a["src"]).read_text(encoding="utf-8"))),
                                  ensure_ascii=False), encoding="utf-8")
        done += 1
    updated = 0
    for a in p["update"]:
        dst = tdir / a["name"]
        cur = json.loads(dst.read_text(encoding="utf-8"))
        new = _fresh(json.loads(Path(a["src"]).read_text(encoding="utf-8")))
        for k in _KEEP:                      # 在当前账号里做过的收藏 / 归档 / 点开时间，留着
            if k in cur:
                new[k] = cur[k]
        dst.write_text(json.dumps(new, ensure_ascii=False), encoding="utf-8")
        updated += 1
    _write_ledger(bak, tdir, "sync", added=[a["name"] for a in p["add"]], updated=[a["name"] for a in p["update"]])
    return dict(p, done=done, updated=updated, dry=False, backup=str(bak))


# ---------- 备份账本与还原 ----------
# 每次写之前把目标文件夹整个拷进 claude-sync-bak/<时间>/<账号>/<组织>/，旁边 ledger.json 记这次补了哪些、刷新了哪些。
# 还原：补进来的挪进回收站，刷新过的换回备份里的版本；别的（之后你自己新开的会话）不动。

BAK_ROOT = paths.HOME / "claude-sync-bak"


def _backup(tdir: Path) -> Path:
    bak = BAK_ROOT / datetime.now().strftime("%Y%m%d-%H%M%S")
    while bak.exists():
        bak = bak.with_name(bak.name + "x")
    shutil.copytree(tdir, bak / tdir.parent.name / tdir.name)
    return bak


def _write_ledger(bak: Path, tdir: Path, what: str, **kw) -> None:
    led = {"what": what, "time": datetime.now().isoformat(timespec="seconds"), "target": str(tdir),
           "key": f"{tdir.parent.name}/{tdir.name}", **kw}
    (bak / "ledger.json").write_text(json.dumps(led, ensure_ascii=False, indent=1), encoding="utf-8")


def list_backups() -> list[dict]:
    out = []
    if not BAK_ROOT.exists():
        return out
    for b in sorted(BAK_ROOT.iterdir(), reverse=True):
        if not b.is_dir() or not b.name[:8].isdigit():
            continue
        try:
            led = json.loads((b / "ledger.json").read_text(encoding="utf-8"))
        except Exception:
            led = None
        dirs = [d for a in b.iterdir() if a.is_dir() for d in a.iterdir() if d.is_dir()]
        out.append({"id": b.name, "ledger": led, "key": led["key"] if led else (f"{dirs[0].parent.name}/{dirs[0].name}" if dirs else None),
                    "count": sum(1 for d in dirs for _ in d.glob("local_*.json")),
                    "added": len(led.get("added", [])) if led else None, "updated": len(led.get("updated", [])) if led else None})
    return out


def restore(backup_id: str) -> dict:
    """把某次同步撤回去。没有账本的老备份：只补回现在缺的登记，不覆盖、不删。"""
    b = BAK_ROOT / backup_id
    if not b.is_dir() or "/" in backup_id or "\\" in backup_id or ".." in backup_id:
        return {"ok": False, "error": f"没有这个备份：{backup_id}"}
    dirs = [d for a in b.iterdir() if a.is_dir() for d in a.iterdir() if d.is_dir()]
    if len(dirs) != 1:
        return {"ok": False, "error": "备份目录结构不对（应该正好一个 账号/组织）"}
    src = dirs[0]
    tdir = sessions_root() / src.parent.name / src.name
    try:
        led = json.loads((b / "ledger.json").read_text(encoding="utf-8"))
    except Exception:
        led = None
    pre = _backup(tdir)                       # 还原之前也先备份一次，还原错了还能再还原回来
    trash = paths.HOME / "trash" / f"claude-restore-{datetime.now():%Y%m%d-%H%M%S}"
    removed = restored = 0
    if led:
        for n in led.get("added", []):
            f = tdir / n
            if f.exists():
                trash.mkdir(parents=True, exist_ok=True)
                shutil.move(str(f), trash / n)
                removed += 1
        names = led.get("updated", [])
    else:
        # 没账本的老备份不知道那次改了哪些：只补回现在缺的登记，不覆盖现有的（覆盖会把之后的新进度改回旧的）
        names = [f.name for f in src.glob("local_*.json") if not (tdir / f.name).exists()]
    for n in names:
        if (src / n).exists():
            shutil.copy2(src / n, tdir / n)
            restored += 1
    _write_ledger(pre, tdir, "restore", source=backup_id, removed=removed, restored=restored)
    return {"ok": True, "removed": removed, "restored": restored, "backup": str(pre),
            "trash": str(trash) if removed else None}


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    dry = "--dry-run" in argv
    tgt = None
    if "--to" in argv:
        tgt = argv[argv.index("--to") + 1]
    if "--list" in argv:
        for f in folders():
            t = datetime.fromtimestamp(f["latest"]).strftime("%m-%d %H:%M") if f["latest"] else "—"
            print(f"{f['key']}  会话 {f['count']:>3}  最近 {t}")
        return 0
    if "--backups" in argv:
        for b in list_backups():
            what = (f"补 {b['added']} / 刷新 {b['updated']}" if b["ledger"] and b["ledger"]["what"] == "sync"
                    else "还原前的备份" if b["ledger"] else "老备份（没账本）")
            print(f"{b['id']}  {b['key'][:17] if b['key'] else '?'}  会话 {b['count']:>3}  {what}")
        return 0
    from . import apps
    if "--restore" in argv:
        bid = argv[argv.index("--restore") + 1]
        r = apps.run_or_defer("claude", "claude-restore", {"backup": bid}, f"撤回 Claude 同步 {bid}")
        print(r.get("hint") or json.dumps({k: v for k, v in r.items() if k != "deferred"}, ensure_ascii=False))
        return 0
    only = argv[argv.index("--only") + 1].split(",") if "--only" in argv else None
    r = run(tgt, True, only)
    if not r["ok"]:
        print("✗ " + r["error"])
        return 1
    print(f"目标（当前账号 / 组织）：{r['target']['key']}（已有 {r['target']['count']} 个会话）")
    print(f"可以补进来 {len(r['add'])} 个；已有但别处更新、要刷新 {len(r['update'])} 个；对话记录已不在、跳过 {r['skip_missing']} 个")
    for a in r["add"][:40]:
        t = datetime.fromtimestamp(a["last"]).strftime("%m-%d %H:%M") if a["last"] else "—"
        print(f"  {t}  {a['title'][:40]}{'（已归档）' if a['archived'] else ''}  ·  {a['cwd']}")
    if len(r["add"]) > 40:
        print(f"  … 还有 {len(r['add']) - 40} 个")
    if dry:
        print("（演练，没写文件；去掉 --dry-run 真正补齐）")
        return 0
    if not (r["add"] or r["update"]):
        return 0
    # 桌面端开着时直接写，会在它退出时被内存里的旧登记盖回去：排到它退出之后
    r = apps.run_or_defer("claude", "claude-sync", {"target": tgt, "only": only}, "同步 Claude 会话到当前账号")
    if r.get("deferred"):
        print(r["hint"])
    else:
        print(f"✓ 已补齐 {r['done']} 个、刷新 {r['updated']} 个，原目录备份在 {r['backup']}（撤回：--restore {Path(r['backup']).name}）。"
              "打开 Claude 桌面端就能在列表里看到。")
    return 0
