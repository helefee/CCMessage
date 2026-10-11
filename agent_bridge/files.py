# -*- coding: utf-8 -*-
"""项目文件查看 / 文件差异 / 文件历史 / 逐行追溯（git blame）。

全部只读：只读文件，git 只用 ls-files / show / diff / log / blame / rev-parse / for-each-ref / status；不写文件、不改 git。
所有路径都限制在已登记的项目目录里：先 resolve（符号链接解析到真实位置）再检查还在项目目录下，防 ../ 和符号链接逃逸。
大仓库：文件列表按项目缓存（git 索引变了或 60 秒后重建），目录按需展开；git 命令都有超时。
"""
from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
MAX_TEXT = 1_000_000          # 文本超过这么大只给前面一段
MAX_IMAGE = 8_000_000
IMG = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
       ".bmp": "image/bmp", ".ico": "image/x-icon", ".svg": "image/svg+xml"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".idea", ".vs",
             "dist", "build", ".next", ".cache", "target", ".gradle", "Library", "Temp", "obj", ".temp", ".tmp"}
REF_OK = re.compile(r"^[\w./~^@{}-]{1,200}$")
_lock = threading.Lock()
_TREES: dict = {}


class Denied(ValueError):
    pass


def _git(root: Path, args: list[str], timeout: float = 20, limit: int | None = None, text: bool = True):
    git = shutil.which("git") or "git"
    try:
        r = subprocess.run([git, "--no-optional-locks", "-c", "core.quotepath=off", "-c", "color.ui=never", *args],
                           cwd=str(root), capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           creationflags=NO_WINDOW, env=dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0"))
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return 124, b"" if not text else "", str(e)
    out = r.stdout[:limit] if limit else r.stdout
    return r.returncode, (out.decode("utf-8", "replace") if text else out), r.stderr.decode("utf-8", "replace")


def _key(p: Path) -> str:
    s = str(p)
    return s.lower() if os.name == "nt" else s


def check_root(root: Path, registered: list[str]) -> Path:
    """root 必须是已登记的项目（比较真实路径）。"""
    real = root.resolve()
    if _key(real) not in {_key(Path(r).resolve()) for r in registered if r}:
        raise Denied("不是已登记的项目目录")
    return real


def safe_path(root: Path, rel: str) -> Path:
    """项目里的一个路径（相对路径，或项目目录下的绝对路径）；resolve 之后必须还在项目里。"""
    rel = (rel or "").strip().strip('"')
    if "\x00" in rel:
        raise Denied("路径不对")
    p = Path(rel)
    if not p.is_absolute():
        p = root / rel.replace("\\", "/").lstrip("/")
    real = p.resolve()
    try:
        real.relative_to(root)
    except ValueError:
        raise Denied("路径不在项目目录里")
    return real


def rel_of(root: Path, p: Path) -> str:
    return p.relative_to(root).as_posix()


_ISGIT: dict = {}


def _is_git(root: Path) -> bool:
    k = _key(root)
    hit = _ISGIT.get(k)
    if hit and time.time() - hit[1] < 60:
        return hit[0]
    rc, out, _ = _git(root, ["rev-parse", "--show-toplevel"], timeout=8)
    _ISGIT[k] = (rc == 0, time.time())
    return rc == 0


_REPOS: dict = {}


def _repo(root: Path, p: Path) -> tuple[Path | None, str]:
    """文件所在的 git 仓库（项目目录本身，或项目里面套着的子仓库）和它在仓库里的相对路径；不在仓库里 → (None, "")。
    仓库根必须在项目目录里面或就是项目目录（项目在别人仓库里面时也认，但路径只用项目里的这个文件）。"""
    d = p if p.is_dir() else p.parent
    k = _key(d)
    with _lock:
        hit = _REPOS.get(k)
    if hit is None or time.time() - hit[1] > 120:
        top = None
        if d.is_dir():
            rc, out, _ = _git(d, ["rev-parse", "--show-toplevel"], timeout=8)
            if rc == 0 and out.strip():
                top = Path(out.strip()).resolve()
        hit = (top, time.time())
        with _lock:
            _REPOS[k] = hit
    top = hit[0]
    if top is None:
        return None, ""
    try:
        return top, p.relative_to(top).as_posix()
    except ValueError:
        return None, ""


# ---------- 文件列表（按项目缓存） ----------

def _stamp(root: Path) -> float:
    try:
        return (root / ".git" / "index").stat().st_mtime
    except OSError:
        return 0.0


def _walk(root: Path, cap: int = 200_000) -> list[str]:
    out = []
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS and not d.startswith(".git")]
        rel = os.path.relpath(dp, root).replace("\\", "/")
        for f in fns:
            out.append(f if rel == "." else f"{rel}/{f}")
            if len(out) >= cap:
                return out
    return out


def all_files(root: Path) -> dict:
    """{"files": [相对路径…], "git": bool, "truncated": bool, "ms": 用时}；git 仓库用 ls-files（尊重 .gitignore）。"""
    k = _key(root)
    st = _stamp(root)
    with _lock:
        hit = _TREES.get(k)
        if hit and hit["stamp"] == st and time.time() - hit["t"] < (60 if hit["v"]["git"] else 300):
            return hit["v"]
    t0 = time.time()
    files, is_git = None, False
    rc, out, _ = _git(root, ["ls-files", "-z", "--cached", "--others", "--exclude-standard"], timeout=30)
    if rc == 0:
        is_git = True
        seen, files = set(), []
        for f in out.split("\x00"):
            if f and f not in seen:
                seen.add(f)
                files.append(f)
        # 已删除但还在索引里的不列（工作区没有）：只在数量不大时检查，大仓库靠打开时报「不存在」
        if len(files) < 20000:
            files = [f for f in files if os.path.lexists(os.path.join(root, f))]
    else:
        files = _walk(root)
    files.sort(key=str.lower)
    cap = 300_000
    v = {"files": files[:cap], "git": is_git, "truncated": len(files) > cap, "ms": int((time.time() - t0) * 1000)}
    with _lock:
        _TREES[k] = {"stamp": st, "t": time.time(), "v": v}
    return v


def _ls_dir(root: Path, d: str) -> dict:
    """不是 git 仓库：直接读这一层目录（不用先把整个项目扫一遍）。"""
    t0 = time.time()
    base = safe_path(root, d) if d else root
    dirs, files = [], []
    try:
        with os.scandir(base) as it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        if e.name not in SKIP_DIRS and not e.name.startswith(".git"):
                            dirs.append({"name": e.name, "n": None})
                    else:
                        files.append(e.name)
                except OSError:
                    pass
    except OSError:
        pass
    dirs.sort(key=lambda x: x["name"].lower())
    files.sort(key=str.lower)
    return {"dir": d.strip("/"), "dirs": dirs, "files": files[:5000], "more": max(0, len(files) - 5000), "total": None,
            "git": False, "ms": int((time.time() - t0) * 1000)}


def ls(root: Path, d: str = "") -> dict:
    """一层目录：子目录（带文件数）+ 文件。"""
    hit = _TREES.get(_key(root))
    if not (hit and hit["v"]["git"]) and not _is_git(root):
        return _ls_dir(root, d)
    allf = all_files(root)
    pre = (d.strip("/") + "/") if d.strip("/") else ""
    dirs: dict = {}
    files = []
    for f in allf["files"]:
        if not f.startswith(pre):
            continue
        rest = f[len(pre):]
        i = rest.find("/")
        if i < 0:
            files.append(rest)
        else:
            dirs[rest[:i]] = dirs.get(rest[:i], 0) + 1
    return {"dir": pre.rstrip("/"), "dirs": [{"name": k, "n": n} for k, n in sorted(dirs.items(), key=lambda x: x[0].lower())],
            "files": files[:5000], "more": max(0, len(files) - 5000), "total": len(allf["files"]), "git": allf["git"],
            "ms": allf["ms"]}


def _fuzzy(q: str, s: str) -> int | None:
    """子序列匹配打分（越小越好）；文件名里连续命中、开头命中加分。None = 不匹配。"""
    ql, sl = q.lower(), s.lower()
    base = sl.rsplit("/", 1)[-1]
    if ql in base:
        return base.index(ql) + len(base) // 4
    if ql in sl:
        return 50 + sl.index(ql) // 4
    i, score, last = 0, 100, -1
    for ch in ql:
        j = sl.find(ch, i)
        if j < 0:
            return None
        score += (j - last - 1) if last >= 0 else j // 4
        last, i = j, j + 1
    return score + len(sl) // 10


def find(root: Path, q: str, limit: int = 60) -> dict:
    q = (q or "").strip().replace("\\", "/")
    allf = all_files(root)["files"]
    if not q:
        return {"items": []}
    hits = []
    for f in allf:
        sc = _fuzzy(q, f)
        if sc is not None:
            hits.append((sc, f))
    hits.sort()
    return {"items": [f for _, f in hits[:limit]], "total": len(hits)}


# ---------- 读文件 ----------

def decode(b: bytes) -> tuple[str, str]:
    if b.startswith(b"\xef\xbb\xbf"):
        return b[3:].decode("utf-8", "replace"), "utf-8-sig"
    if b.startswith(b"\xff\xfe") or b.startswith(b"\xfe\xff"):
        return b.decode("utf-16", "replace"), "utf-16"
    try:
        return b.decode("utf-8"), "utf-8"
    except UnicodeDecodeError as e:
        if e.start > len(b) - 4:      # 截断在一个字的中间
            return b[:e.start].decode("utf-8", "replace"), "utf-8"
    try:
        return b.decode("gb18030"), "gbk"
    except UnicodeDecodeError:
        return b.decode("latin-1"), "latin-1"


def _binary(b: bytes) -> bool:
    return b"\x00" in b[:8000]


def show(root: Path, ref: str, rel: str) -> bytes | None:
    if not REF_OK.match(ref or ""):
        raise Denied("版本名不对")
    rc, out, _ = _git(root, ["show", f"{ref}:{rel}"], timeout=20, limit=MAX_TEXT * 4, text=False)
    return out if rc == 0 else None


def read(root: Path, rel: str, ref: str | None = None) -> dict:
    p = safe_path(root, rel)
    rel = rel_of(root, p)
    size, mtime = None, None
    if ref:
        repo, rr = _repo(root, p)
        b = show(repo, ref, rr) if repo else None
        if b is None:
            return {"ok": False, "path": rel, "error": f"{ref} 里没有这个文件"}
        size = len(b)
    else:
        if not p.is_file():
            return {"ok": False, "path": rel, "error": "文件不存在（或是目录）"}
        st = p.stat()
        size, mtime = st.st_size, st.st_mtime
        ext = p.suffix.lower()
        if ext in IMG:
            if size > MAX_IMAGE:
                return {"ok": True, "path": rel, "kind": "image", "size": size, "error": "图片太大，不预览"}
            data = p.read_bytes()
            return {"ok": True, "path": rel, "kind": "image", "size": size, "mtime": mtime,
                    "data": f"data:{IMG[ext]};base64," + base64.b64encode(data).decode()}
        with open(p, "rb") as f:
            b = f.read(MAX_TEXT + 1)
    if _binary(b):
        return {"ok": True, "path": rel, "kind": "binary", "size": size, "mtime": mtime}
    truncated = len(b) > MAX_TEXT
    text, enc = decode(b[:MAX_TEXT])
    out = {"ok": True, "path": rel, "kind": "text", "size": size, "mtime": mtime, "encoding": enc, "truncated": truncated,
           "text": text, "ref": ref}
    if not ref:
        out["marks"] = marks(root, p)
    return out


# ---------- 差异 ----------

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _hunks(diff: str) -> list[list[int]]:
    """unified diff（-U0）→ [[旧起, 旧行数, 新起, 新行数], …]"""
    out = []
    for line in diff.splitlines():
        m = HUNK.match(line)
        if m:
            out.append([int(m.group(1)), int(m.group(2) or 1), int(m.group(3)), int(m.group(4) or 1)])
    return out


def _tracked(root: Path, rel: str) -> bool:      # root = 仓库根，rel = 仓库里的路径
    rc, out, _ = _git(root, ["ls-files", "--error-unmatch", "--", rel], timeout=10)
    return rc == 0


def marks(root: Path, p: Path) -> dict:
    """文件查看器行号旁的改动标记（相对 HEAD）：{"add": [行号…], "mod": […], "del": [在这行后面删了…]}；没跟踪的整个算新增。"""
    repo, rel = _repo(root, p)
    if not repo:
        return {}
    if not _tracked(repo, rel):
        return {"untracked": True}
    rc, out, _ = _git(repo, ["diff", "--no-ext-diff", "-U0", "HEAD", "--", rel], timeout=20, limit=4_000_000)
    if rc != 0:
        return {}
    add, mod, dele = [], [], []
    for os_, ol, ns, nl in _hunks(out):
        if nl == 0:
            dele.append(ns)
        elif ol == 0:
            add.extend(range(ns, ns + nl))
        else:
            mod.extend(range(ns, ns + nl))
    return {"add": add[:20000], "mod": mod[:20000], "del": dele[:5000]}


def diff(root: Path, rel: str, a: str = "HEAD", b: str = "WORKTREE") -> dict:
    """一个文件在 a 和 b 之间的差异：两边全文 + 行块（hunks）。b=WORKTREE 是工作区。"""
    p = safe_path(root, rel)
    shown = rel_of(root, p)
    for r in (a, b):
        if r != "WORKTREE" and not REF_OK.match(r or ""):
            raise Denied("版本名不对")
    repo, rel = _repo(root, p)
    if not repo and (a != "EMPTY" or b != "WORKTREE"):
        return {"ok": False, "path": shown, "error": "这个文件不在 git 仓库里，没有版本可比"}
    if a != "EMPTY":
        rc, _, _ = _git(repo, ["rev-parse", "--verify", "--quiet", a + "^{commit}"], timeout=10)
        if rc != 0:
            return {"ok": False, "error": f"找不到版本 {a}"}
    old_b = b"" if a == "EMPTY" else (show(repo, a, rel) or b"")
    if b == "WORKTREE":
        new_b = p.read_bytes()[:MAX_TEXT * 4] if p.is_file() else b""
    else:
        new_b = show(repo, b, rel) or b""
    if _binary(old_b) or _binary(new_b):
        return {"ok": True, "path": shown, "binary": True, "a": a, "b": b}
    old, enc_a = decode(old_b)
    new, enc_b = decode(new_b)
    too_big = len(old) > MAX_TEXT or len(new) > MAX_TEXT
    hunks = None
    if a != "EMPTY":
        args = ["diff", "--no-ext-diff", "-U0", "--diff-algorithm=histogram", a] + ([] if b == "WORKTREE" else [b]) + ["--", rel]
        if b == "WORKTREE" and not _tracked(repo, rel):
            hunks = None
        else:
            rc, out, _ = _git(repo, args, timeout=30, limit=8_000_000)
            if rc == 0:
                hunks = _hunks(out)
    if hunks is None:     # 一边是空的（新增 / 删除 / 没跟踪）
        ol = len(old.splitlines()) if old else 0
        nl = len(new.splitlines()) if new else 0
        hunks = [[1 if ol else 0, ol, 1 if nl else 0, nl]] if (ol or nl) and old != new else []
    return {"ok": True, "path": shown, "a": a, "b": b, "old": old[:MAX_TEXT], "new": new[:MAX_TEXT], "hunks": hunks,
            "truncated": too_big, "encoding": enc_b or enc_a}


def refs(root: Path, rel: str | None = None) -> dict:
    if rel:                         # 项目里套着的子仓库：按这个文件所在的仓库列
        repo, _ = _repo(root, safe_path(root, rel))
        if not repo:
            return {"git": False, "branches": [], "commits": []}
        root = repo
    elif not _is_git(root):
        return {"git": False, "branches": [], "commits": []}
    rc, out, _ = _git(root, ["for-each-ref", "--sort=-committerdate", "--count=40", "--format=%(refname:short)", "refs/heads", "refs/remotes"], timeout=10)
    br = [x for x in out.split() if x and not x.endswith("/HEAD")] if rc == 0 else []
    rc, out, _ = _git(root, ["log", "-n", "30", "--format=%h%x1f%s%x1f%ar"], timeout=10)
    cm = [dict(zip(("h", "s", "ago"), l.split("\x1f"))) for l in out.splitlines() if l.strip()] if rc == 0 else []
    rc, head, _ = _git(root, ["rev-parse", "--abbrev-ref", "HEAD"], timeout=5)
    return {"git": True, "head": head.strip(), "branches": br, "commits": cm}


def log(root: Path, rel: str, n: int = 60) -> dict:
    p = safe_path(root, rel)
    repo, rel = _repo(root, p)
    if not repo:
        return {"ok": False, "error": "这个文件不在 git 仓库里，没有历史"}
    rc, out, err = _git(repo, ["log", "--follow", "-n", str(n), "--format=%H%x1f%h%x1f%an%x1f%aI%x1f%s", "--numstat", "--", rel], timeout=30)
    if rc != 0:
        return {"ok": False, "error": (err.strip().splitlines() or ["git log 出错"])[-1][:200]}
    items, cur = [], None
    for line in out.splitlines():
        if "\x1f" in line:
            h, sh, an, ad, s = (line.split("\x1f") + [""] * 5)[:5]
            cur = {"h": h, "short": sh, "author": an, "date": ad, "subject": s, "add": 0, "del": 0, "path": rel}
            items.append(cur)
        elif cur and line.strip():
            parts = line.split("\t")
            if len(parts) >= 3:
                cur["add"] += int(parts[0]) if parts[0].isdigit() else 0
                cur["del"] += int(parts[1]) if parts[1].isdigit() else 0
                cur["path"] = parts[2].split(" => ")[-1].strip("{}") if "=>" not in parts[2] else rel
    return {"ok": True, "path": rel, "items": items}


def blame(root: Path, rel: str) -> dict:
    p = safe_path(root, rel)
    repo, rel = _repo(root, p)
    if not repo:
        return {"ok": False, "error": "这个文件不在 git 仓库里，没有追溯"}
    rc, out, err = _git(repo, ["blame", "--porcelain", "-w", "--", rel], timeout=40, limit=20_000_000)
    if rc != 0:
        return {"ok": False, "error": (err.strip().splitlines() or ["git blame 出错"])[-1][:200]}
    info, lines, cur = {}, [], None
    for line in out.splitlines():
        if not line:
            continue
        if line[0] == "\t":
            lines.append(cur)
            continue
        parts = line.split(" ")
        if len(parts[0]) == 40 and all(c in "0123456789abcdef" for c in parts[0]) and len(parts) >= 3:
            cur = parts[0]
            info.setdefault(cur, {})
        elif cur and parts[0] in ("author", "author-time", "summary"):
            info[cur][parts[0]] = line.split(" ", 1)[1] if " " in line else ""
    commits = {h[:8]: {"author": v.get("author", ""), "time": int(v.get("author-time", "0") or 0), "summary": v.get("summary", "")}
               for h, v in info.items()}
    return {"ok": True, "path": rel, "lines": [h[:8] for h in lines], "commits": commits}


# ---------- 在编辑器 / 资源管理器里打开（只是启动外部程序看，不写任何东西） ----------

def _vscode() -> str | None:
    for c in (shutil.which("code"), shutil.which("code.cmd"),
              os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
              r"C:\Program Files\Microsoft VS Code\Code.exe", "/usr/bin/code", "/usr/share/code/code",
              "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"):
        if c and os.path.exists(c):
            return c
    return None


def has_vscode() -> bool:
    return _vscode() is not None


def open_external(root: Path, rel: str, how: str, line: int | None = None) -> dict:
    p = safe_path(root, rel)
    if not p.exists():
        raise Denied("文件不存在")
    if how == "code":
        exe = _vscode()
        if not exe:
            return {"ok": False, "error": "没找到 VS Code"}
        target = f"{p}:{int(line)}" if line else str(p)
        args = ["cmd", "/c", exe, "-g", target] if exe.lower().endswith(".cmd") else [exe, "-g", target]
        subprocess.Popen(args, cwd=str(root), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=NO_WINDOW, close_fds=True)
        return {"ok": True}
    if how == "reveal":
        if os.name == "nt":
            subprocess.Popen(["explorer.exe", f"/select,{p}"], close_fds=True)
        elif shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", str(p.parent)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif shutil.which("open"):
            subprocess.Popen(["open", "-R", str(p)])
        else:
            return {"ok": False, "error": "这台电脑上没有可用的文件管理器"}
        return {"ok": True}
    raise Denied("不支持的打开方式")
