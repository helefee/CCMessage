# -*- coding: utf-8 -*-
"""Claude 桌面端会话列表跨账号同步。

桌面端的会话列表按「账号 / 组织」分文件夹记：<Claude 数据目录>/claude-code-sessions/<账号>/<组织>/local_*.json，
只显示当前登录的那个文件夹；对话内容本身在 ~/.claude/projects/**/<会话号>.jsonl，本地、不分账号。
切换账号后旧会话「不见了」只是列表文件在别的文件夹里。这里把别的文件夹里的会话列表文件补齐复制到目标文件夹：

- 只补缺的（同名文件已有就跳过），不覆盖、不删除；
- 对话记录文件已经不在的会话不复制（点开也续不上）；
- 写之前把目标文件夹整个备份到 ~/.agent-bridge/claude-sync-bak/<时间>/。

注意：桌面端和命令行（Claude Code CLI）是两套登录，可以登的不是同一个号：
- 桌面端：从它自己的 Local Storage（<Claude 数据目录>/Local Storage/leveldb）里找最近一条带账号 / 组织的记录；
- 命令行：~/.claude.json 的 oauthAccount。
桌面端的邮箱 / 名字从它的 IndexedDB（claude.ai 页面缓存的账号信息）里读，读不到就借命令行的（同一个号时）或以前记下的（推测）。
所以有两个「当前账号」，source='desktop' / 'cli' 分别对应，缺省按桌面端算（会话列表本来就是给桌面端看的）。
老实说：命令行自己（claude --resume）直接读 ~/.claude/projects，根本不看这里按账号分的登记；
「同步到 CLI 账号」只是把会话补进 CLI 那个号的文件夹，只有桌面端登的也是这个号时才看得到效果。

目标缺省是对应来源的当前账号 / 组织；都认不出来就退回「最近有会话在写的那个文件夹」。桌面端要重启才会看到新补进来的会话。
"""
from __future__ import annotations

import json
import os
import re
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


def _short(a: str | None) -> str:
    return (a or "")[:8]


def cli_account() -> dict:
    """命令行（Claude Code CLI）登录的号：~/.claude.json 的 oauthAccount。"""
    try:
        o = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8")).get("oauthAccount") or {}
    except Exception:
        o = {}
    a, g = o.get("accountUuid"), o.get("organizationUuid")
    name = o.get("displayName") or o.get("fullName")
    _remember(a, o.get("emailAddress"), name, "cli")      # 记下来：以后桌面端登这个号、又认不出邮箱时拿来推测
    return {"accountUuid": a, "organizationUuid": g, "email": o.get("emailAddress"), "name": name,
            "key": f"{a}/{g}" if a and g else None, "via": "claude.json" if a and g else None}


_UUID = rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_ACC_RE = re.compile(rb'"(?:accountUuid|account_uuid)"\s*:\s*"(' + _UUID + rb')"')
_ORG_RE = re.compile(rb'"(?:organizationUuid|organization_uuid)"\s*:\s*"(' + _UUID + rb')"')
# eventTimestamp 在压缩块里常被截成「…Timestamp」，只认后半截
_TS_RE = re.compile(rb'Timestamp"\s*:\s*"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z)"')
_CUT_RE = re.compile(rb'"eventName"|"(?:accountUuid|account_uuid)"')


def _leveldb_pairs() -> list[tuple[str, float, str, str]]:
    """桌面端 Local Storage 里所有（时间戳, 文件修改时间, 账号, 组织）。
    按字节硬扫 *.log / *.ldb：被 snappy 压过的块大多对不上，对不上的就跳过。"""
    d = claude_app_dir() / "Local Storage" / "leveldb"
    out = []
    try:
        files = [f for f in d.iterdir() if f.suffix in (".log", ".ldb")]
    except Exception:
        return out
    for f in files:
        try:
            b, mt = f.read_bytes(), f.stat().st_mtime
        except Exception:
            continue
        for m in _ACC_RE.finditer(b):
            # 组织号在账号号前后 300 字节内找，碰到下一条事件 / 下一个账号字段就停，免得串到别的事件上
            after = b[m.end():m.end() + 300]
            c = _CUT_RE.search(after)
            after = after[:c.start()] if c else after
            before = b[max(0, m.start() - 300):m.start()]
            cuts = list(_CUT_RE.finditer(before))
            if cuts and cuts[-1].group(0) != b'"eventName"':
                before = before[cuts[-1].end():]
            og = _ORG_RE.search(after) or (list(_ORG_RE.finditer(before)) or [None])[-1]
            if not og:
                continue
            # 时间戳：同一条事件里，在账号字段前面（eventName 之后）
            ev = before
            k = ev.rfind(b'"eventName"')
            ev = ev[k:] if k >= 0 else ev
            ts = (list(_TS_RE.finditer(ev)) or [None])[-1]
            out.append((ts.group(1).decode() if ts else "", mt, m.group(1).decode(), og.group(1).decode()))
    return out


def desktop_account(profile: bool = True) -> dict:
    """桌面端登录的号：Local Storage 里时间戳最新的那条账号 / 组织；都没时间戳就取最近改过的文件里的。
    认不出来 key 为 None（调用方自己退回）。profile=True 时再补邮箱 / 名字（见 _profile）。"""
    ps = _leveldb_pairs()
    if not ps:
        return {"accountUuid": None, "organizationUuid": None, "email": None, "name": None, "key": None, "via": None}
    with_ts = [p for p in ps if p[0]]
    ts, _, a, g = max(with_ts) if with_ts else max(ps, key=lambda p: p[1])
    d = {"accountUuid": a, "organizationUuid": g, "email": None, "name": None,
         "key": f"{a}/{g}", "via": "leveldb", "seen": ts or None}
    return dict(d, **_profile(a)) if profile else d


# ---------- 账号的邮箱 / 名字 ----------
# 桌面端里 claude.ai 页面把账号信息（react-query 缓存）存在 IndexedDB：
#   <Claude 数据目录>/IndexedDB/https_claude.ai_0.indexeddb.blob/** 和 …indexeddb.leveldb/*.log|*.ldb
# 是 V8 序列化的对象：…"uuid" <账号号> "email_address" <邮箱> "full_name" <名字> "display_name" <名字>…
# 字段名后面紧跟类型字节（'"' 单字节串 / 'c' 双字节 UTF-16 / 'S' UTF-8）和长度，按这个读；
# 被 snappy 压过、对不上的就跳过。只认紧跟在账号号后面的这几个字段，不碰 cookie / 令牌。
# 认出来的（还有命令行登过的号）记进 ~/.agent-bridge/claude-accounts.json，以后认不出来时拿来推测。

_EMAIL_OK = re.compile(r"[^@\s\x00-\x1f]+@[^@\s\x00-\x1f]+\.[A-Za-z]{2,}")
NAMES_CACHE = paths.HOME / "claude-accounts.json"


def _v8_str(b: bytes, i: int) -> str | None:
    """b[i] 是类型字节，后面 varint 长度 + 内容。"""
    if i >= len(b):
        return None
    tag, i = b[i:i + 1], i + 1
    n = shift = 0
    while i < len(b):
        x = b[i]
        i += 1
        n |= (x & 0x7F) << shift
        shift += 7
        if not x & 0x80:
            break
    if not 0 < n <= 200:
        return None
    raw = b[i:i + n]
    try:
        t = {b'"': lambda r: r.decode("latin-1"), b"c": lambda r: r.decode("utf-16-le"),
             b"S": lambda r: r.decode("utf-8")}[tag](raw)
    except Exception:
        return None
    return t if t.isprintable() and len(raw) == n else None


def _field(b: bytes, start: int, name: bytes, span: int = 400) -> str | None:
    k = b.find(name, start, start + span)
    return _v8_str(b, k + len(name)) if k >= 0 else None


def _idb_profile(acct: str) -> dict | None:
    d = claude_app_dir() / "IndexedDB"
    files = []
    for sub in ("https_claude.ai_0.indexeddb.blob", "https_claude.ai_0.indexeddb.leveldb"):
        try:
            files += [f for f in (d / sub).rglob("*") if f.is_file() and (sub.endswith(".blob") or f.suffix in (".log", ".ldb"))]
        except Exception:
            pass
    needle = b'uuid"$' + acct.encode()
    best = None
    for f in files:
        try:
            if f.stat().st_size > 50_000_000:
                continue
            b, mt = f.read_bytes(), f.stat().st_mtime
        except Exception:          # 桌面端开着时有的文件锁着，读不了就算了
            continue
        i = b.find(needle)
        while i >= 0:
            j = i + len(needle)
            email = _field(b, j, b"email_address", 40)
            if email and _EMAIL_OK.fullmatch(email):
                name = _field(b, j, b"full_name") or _field(b, j, b"display_name")
                if not best or mt > best[0]:
                    best = (mt, email, name)
            i = b.find(needle, j)
    return {"email": best[1], "name": best[2]} if best else None


def _load_names() -> dict:
    try:
        return json.loads(NAMES_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _remember(acct: str, email: str | None, name: str | None, src: str) -> None:
    if not acct or not email:
        return
    c = _load_names()
    old = c.get(acct) or {}
    if old.get("email") == email and old.get("name") == (name or old.get("name")):
        return
    c[acct] = {"email": email, "name": name or old.get("name"), "from": src,
               "seen": datetime.now().isoformat(timespec="seconds")}
    try:
        NAMES_CACHE.parent.mkdir(parents=True, exist_ok=True)
        NAMES_CACHE.write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def _profile(acct: str | None) -> dict:
    """账号号 → {email, name, email_from, email_guess}。
    先看桌面端 IndexedDB（就是它现在登的号，可靠）；再看命令行是不是同一个号；都没有就查以前记下的（推测）。"""
    if not acct:
        return {"email": None, "name": None, "email_from": None, "email_guess": False}
    p = _idb_profile(acct)
    if p:
        _remember(acct, p["email"], p["name"], "desktop")
        return dict(p, email_from="desktop", email_guess=False)
    c = cli_account()
    if c["accountUuid"] == acct and c["email"]:
        return {"email": c["email"], "name": c.get("name"), "email_from": "cli", "email_guess": False}
    m = _load_names().get(acct)
    if m and m.get("email"):
        return {"email": m["email"], "name": m.get("name"), "email_from": "cache", "email_guess": True}
    return {"email": None, "name": None, "email_from": None, "email_guess": False}


def _with_folder(acc: dict) -> dict:
    k = acc.get("key")
    f = next((x for x in folders() if x["key"] == k), None) if k else None
    return dict(acc, exists=bool(f), count=f["count"] if f else 0, path=f["path"] if f else None)


def accounts() -> dict:
    """两套登录各自的当前账号：{"desktop": {...}, "cli": {...}}，带 key / exists（文件夹在不在）/ count / email / name。
    桌面端认不出来时按 current_key('desktop') 的退回规则补上，via 写明是怎么来的。"""
    d = desktop_account()
    if not d["key"]:
        k = current_key("desktop")
        if k:
            a, _, g = k.partition("/")
            c = cli_account()
            d = dict({"accountUuid": a, "organizationUuid": g, "key": k,
                      "via": "cli" if c["key"] == k else "newest-folder"}, **_profile(a))
    c = cli_account()
    if c["key"]:
        c.update(email_from="cli", email_guess=False)
    return {"desktop": _with_folder(d), "cli": _with_folder(c)}


def current_key(source: str = "desktop") -> str | None:
    """当前登录的账号 / 组织。
    source='desktop'（缺省，会话列表都是给桌面端看的）：桌面端 Local Storage 里认出来的号；
        认不出来就退回命令行的号（它的文件夹得在），再不行退回最近有写入的文件夹。
    source='cli'：~/.claude.json 的 oauthAccount，没登就 None。
    认出来的号哪怕还没有文件夹也照样返回（那就是「这个号在桌面端还没开过会话」）。"""
    if source == "cli":
        return cli_account()["key"]
    k = desktop_account(profile=False)["key"]
    if k:
        return k
    c = cli_account()["key"]
    if c and (sessions_root() / c).is_dir():
        return c
    fs = folders()
    return fs[0]["key"] if fs else None


def _transcript_exists(cli_sid: str | None) -> bool:
    if not cli_sid:
        return False
    return any((paths.CLAUDE_HOME / "projects").glob(f"*/{cli_sid}.jsonl"))

_KEEP = ("isArchived", "isStarred", "lastFocusedAt")
# 旧账号被停用时留下的报错（「organization has disabled …」），带到新账号里会一直挂着
_DROP = ("error", "errorAt", "priorErrorMark")


def _fresh(d: dict) -> dict:
    return {k: v for k, v in d.items() if k not in _DROP}


SOURCES = {"desktop": "桌面端", "cli": "命令行（CLI）"}


def plan(target: str | None = None, source: str = "desktop") -> dict:
    if source not in SOURCES:
        return {"ok": False, "error": f"不认识的来源：{source}（只能是 desktop / cli）", "source": source}
    acc = accounts()[source]
    base = {"source": source, "account": acc}
    fs = folders()
    if not fs:
        return dict(base, ok=False, error=f"没找到 Claude 桌面端的会话列表目录：{sessions_root()}")
    target = target or acc.get("key")
    if not target:
        return dict(base, ok=False, error=f"认不出{SOURCES[source]}现在登的是哪个号"
                    + ("（~/.claude.json 里没有登录信息，命令行先 claude 登一下）" if source == "cli" else ""))
    tgt = next((f for f in fs if f["key"] == target), None)
    if not tgt:
        who = f"{SOURCES[source]}的号 {_short(target)}/{_short(target.partition('/')[2])}"
        return dict(base, ok=False, error=f"{who} 还没有会话列表文件夹——这个号还没在 Claude 桌面端里开过 Claude Code 会话。"
                    "先用这个号在桌面端开一个 Claude Code 会话，再回来补齐。")
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
    add, update, skip_missing, skip_tomb, skip_have = [], [], 0, 0, 0
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
            skip_tomb += 1
            continue
        if d.get("cliSessionId") in have_cli:
            skip_have += 1
            continue
        if not _transcript_exists(d.get("cliSessionId")):
            skip_missing += 1
            continue
        add.append(item)
    update.sort(key=lambda x: -x["last"])
    add.sort(key=lambda x: -x["last"])
    return dict(base, ok=True, target=tgt, folders=fs, add=add, update=update, skip_missing=skip_missing,
                skip_tomb=skip_tomb, skip_have=skip_have, tombstones=len(tomb))


def run(target: str | None = None, dry: bool = False, only: list[str] | None = None,
        source: str = "desktop") -> dict:
    """only：只补这几个（local_*.json 文件名）；不给就全补。source：补到桌面端（缺省）还是命令行登的那个号。"""
    p = plan(target, source)
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
    _write_ledger(bak, tdir, "sync", source=source, added=[a["name"] for a in p["add"]], updated=[a["name"] for a in p["update"]])
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


def _acc_line(name: str, a: dict) -> str:
    if not a.get("key"):
        return f"{name}：认不出来"
    via = {"leveldb": "", "cli": "（认不出来，按命令行的号算）", "newest-folder": "（认不出来，按最近有写入的文件夹算）"}.get(a.get("via"), "")
    fold = f"已有 {a['count']} 个会话" if a.get("exists") else "还没有文件夹"
    who = " ".join(x for x in (a.get("name"), a.get("email")) if x)
    if who and a.get("email_guess"):
        who += "（推测：以前记下的）"
    return f"{name}：{a['key']}{'  ' + who if who else ''}  {fold}{via}"


def _print_plan(r: dict) -> None:
    print(f"目标（{SOURCES[r['source']]}当前账号 / 组织）：{r['target']['key']}（已有 {r['target']['count']} 个会话）")
    print(f"可以补进来 {len(r['add'])} 个；已有但别处更新、要刷新 {len(r['update'])} 个；"
          f"对话记录已不在、跳过 {r['skip_missing']} 个；在这个号里删过（墓碑）不复活 {r['skip_tomb']} 个；"
          f"同一会话已在、跳过 {r['skip_have']} 个")
    for a in r["add"][:40]:
        t = datetime.fromtimestamp(a["last"]).strftime("%m-%d %H:%M") if a["last"] else "—"
        print(f"  {t}  {a['title'][:40]}{'（已归档）' if a['archived'] else ''}  ·  {a['cwd']}")
    if len(r["add"]) > 40:
        print(f"  … 还有 {len(r['add']) - 40} 个")


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    dry = "--dry-run" in argv
    tgt = None
    if "--to" in argv:
        tgt = argv[argv.index("--to") + 1]
    src = argv[argv.index("--source") + 1] if "--source" in argv else None
    if src and src not in (*SOURCES, "both"):
        print(f"✗ --source 只能是 desktop / cli / both，不认识：{src}")
        return 2
    if "--list" in argv:
        acc = accounts()
        print(_acc_line("桌面端当前", acc["desktop"]))
        print(_acc_line("命令行当前", acc["cli"]))
        for f in folders():
            t = datetime.fromtimestamp(f["latest"]).strftime("%m-%d %H:%M") if f["latest"] else "—"
            tag = "、".join(n for n, s in (("桌面端", "desktop"), ("CLI", "cli")) if acc[s].get("key") == f["key"])
            print(f"{f['key']}  会话 {f['count']:>3}  最近 {t}{'  ← ' + tag + '当前' if tag else ''}")
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
    if dry:
        # 演练缺省两边都看；--to 指定了文件夹就只看一次（来源只影响显示）
        srcs = ["desktop", "cli"] if (src or "both") == "both" else [src]
        if tgt:
            srcs = srcs[:1]
        seen, bad = {}, 0
        for s in srcs:
            r = run(tgt, True, only, s)
            print(f"===== {SOURCES[s]} =====")
            print(_acc_line("当前账号", r["account"]))
            if not r["ok"]:
                print("✗ " + r["error"])
                bad += 1
            elif r["target"]["key"] in seen:
                print(f"（跟{SOURCES[seen[r['target']['key']]]}是同一个号 / 组织，结果同上）")
            else:
                seen[r["target"]["key"]] = s
                _print_plan(r)
            print()
        print("（演练，没写文件；去掉 --dry-run 真正补齐，真补时用 --source desktop 或 --source cli 选一边，缺省桌面端）")
        return 1 if bad == len(srcs) else 0
    if src == "both":
        print("✗ 真正补齐一次只能选一边：--source desktop 或 --source cli")
        return 2
    src = src or "desktop"
    r = run(tgt, True, only, src)
    print(_acc_line(f"{SOURCES[src]}当前账号", r["account"]))
    if not r["ok"]:
        print("✗ " + r["error"])
        return 1
    _print_plan(r)
    if not (r["add"] or r["update"]):
        return 0
    # 桌面端开着时直接写，会在它退出时被内存里的旧登记盖回去：排到它退出之后
    r = apps.run_or_defer("claude", "claude-sync", {"target": tgt or r["target"]["key"], "only": only, "source": src},
                          f"同步 Claude 会话到{SOURCES[src]}当前账号")
    if r.get("deferred"):
        print(r["hint"])
    elif not r.get("ok"):
        print("✗ " + r.get("error", "失败"))
    else:
        print(f"✓ 已补齐 {r['done']} 个、刷新 {r['updated']} 个，原目录备份在 {r['backup']}（撤回：--restore {Path(r['backup']).name}）。"
              "打开 Claude 桌面端就能在列表里看到。")
    return 0
