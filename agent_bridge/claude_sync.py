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


def _transcript_exists(cli_sid: str | None) -> bool:
    if not cli_sid:
        return False
    return any((paths.CLAUDE_HOME / "projects").glob(f"*/{cli_sid}.jsonl"))


def plan(target: str | None = None) -> dict:
    fs = folders()
    if not fs:
        return {"ok": False, "error": f"没找到 Claude 桌面端的会话列表目录：{sessions_root()}"}
    tgt = next((f for f in fs if f["key"] == target), None) if target else fs[0]
    if not tgt:
        return {"ok": False, "error": f"没有这个账号 / 组织文件夹：{target}"}
    tdir = Path(tgt["path"])
    have = {f.name for f in tdir.glob("local_*.json")}
    have_cli = set()
    for f in tdir.glob("local_*.json"):
        try:
            have_cli.add(json.loads(f.read_text(encoding="utf-8")).get("cliSessionId"))
        except Exception:
            pass
    add, skip_missing, seen = [], 0, set()
    for f in fs:
        if f["key"] == tgt["key"]:
            continue
        for src in Path(f["path"]).glob("local_*.json"):
            if src.name in have or src.name in seen:
                continue
            try:
                d = json.loads(src.read_text(encoding="utf-8"))
            except Exception:
                continue
            if d.get("cliSessionId") in have_cli:
                continue
            if not _transcript_exists(d.get("cliSessionId")):
                skip_missing += 1
                continue
            seen.add(src.name)
            add.append({"src": str(src), "name": src.name, "title": d.get("title") or "（无标题）",
                        "cwd": d.get("cwd"), "from": f["key"], "archived": bool(d.get("isArchived")),
                        "last": d.get("lastActivityAt", 0) / 1000})
    add.sort(key=lambda x: -x["last"])
    return {"ok": True, "target": tgt, "folders": fs, "add": add, "skip_missing": skip_missing}


def run(target: str | None = None, dry: bool = False) -> dict:
    p = plan(target)
    if not p["ok"] or dry or not p["add"]:
        return dict(p, done=0, dry=dry)
    tdir = Path(p["target"]["path"])
    bak = paths.HOME / "claude-sync-bak" / datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copytree(tdir, bak / tdir.parent.name / tdir.name)
    done = 0
    for a in p["add"]:
        dst = tdir / a["name"]
        if dst.exists():
            continue
        shutil.copy2(a["src"], dst)
        done += 1
    return dict(p, done=done, dry=False, backup=str(bak))


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
    r = run(tgt, dry)
    if not r["ok"]:
        print("✗ " + r["error"])
        return 1
    print(f"目标（当前账号 / 组织）：{r['target']['key']}（已有 {r['target']['count']} 个会话）")
    print(f"可以补进来 {len(r['add'])} 个；对话记录已不在、跳过 {r['skip_missing']} 个")
    for a in r["add"][:40]:
        t = datetime.fromtimestamp(a["last"]).strftime("%m-%d %H:%M") if a["last"] else "—"
        print(f"  {t}  {a['title'][:40]}{'（已归档）' if a['archived'] else ''}  ·  {a['cwd']}")
    if len(r["add"]) > 40:
        print(f"  … 还有 {len(r['add']) - 40} 个")
    if dry:
        print("（演练，没写文件；去掉 --dry-run 真正补齐）")
    elif r["done"]:
        print(f"✓ 已补齐 {r['done']} 个，原目录备份在 {r['backup']}。重启 Claude 桌面端就能在列表里看到。")
    return 0
