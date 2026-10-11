# -*- coding: utf-8 -*-
"""GitHub PR / issue 预览、PR 详情和 diff、项目的 PR 列表、本地改动（git status / diff）。

全部只读：只用 `gh api`（GET）、`gh pr view / diff / list`，以及 git 的 status / diff / log / merge-base / remote；
不推送、不评论、不批准、不合并。gh 用的是本机已登录的账号（`gh auth status` 里当前那个）；
没装 gh、没登录、看不了（私有仓库没权限）时返回 {"ok": False, "error": …}，界面退回成普通链接。
结果按引用缓存：开着的 PR 2 分钟，已合并 / 已关闭的 1 小时，出错的 1 分钟。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_lock = threading.Lock()
_CACHE: dict = {}
MAX_DIFF = 3_000_000          # 太大的 diff 只给前面这么多字节

REF_RE = re.compile(r"^\s*(?:https?://github\.com/)?([\w.-]+)/([\w.-]+?)(?:\.git)?(?:#|/(?:pull|pulls|issues)/)(\d+)\b")


def parse_ref(s: str) -> tuple[str, str, int] | None:
    m = REF_RE.match(s or "")
    return (m.group(1), m.group(2), int(m.group(3))) if m else None


def _gh_exe() -> str | None:
    p = shutil.which("gh")
    if p:
        return p
    for c in (r"C:\Program Files\GitHub CLI\gh.exe", r"C:\Program Files (x86)\GitHub CLI\gh.exe",
              os.path.expandvars(r"%LOCALAPPDATA%\Programs\GitHub CLI\gh.exe"), "/usr/bin/gh", "/usr/local/bin/gh", "/opt/homebrew/bin/gh"):
        if c and os.path.exists(c):
            return c
    return None


def _run(argv: list[str], cwd: str | None = None, timeout: float = 25, limit: int | None = None) -> tuple[int, str, str]:
    try:
        r = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           creationflags=NO_WINDOW, env=dict(os.environ, GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1",
                                                             NO_COLOR="1", GIT_TERMINAL_PROMPT="0", LC_ALL="C.UTF-8"))
    except FileNotFoundError:
        return 127, "", "找不到程序"
    except subprocess.TimeoutExpired:
        return 124, "", "超时"
    out = r.stdout[:limit] if limit else r.stdout
    return r.returncode, out.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")


def _gh(args: list[str], **kw) -> tuple[int, str, str]:
    exe = _gh_exe()
    if not exe:
        return 127, "", "这台电脑没装 GitHub CLI（gh）"
    return _run([exe, *args], **kw)


def _why(err: str) -> str:
    e = (err or "").strip()
    if "auth login" in e or "not logged" in e.lower() or "authentication" in e.lower():
        return "gh 还没登录（gh auth login）"
    if "Could not resolve to a" in e or "Not Found" in e or "HTTP 404" in e:
        return "找不到，或者当前 gh 账号没有权限看（私有仓库）"
    if "HTTP 403" in e or "rate limit" in e.lower():
        return "GitHub 暂时不让查（限流 / 没权限）"
    return e.splitlines()[-1][:200] if e else "gh 出错"


def _cached(key, ttl_of, fn):
    now = time.time()
    with _lock:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < hit[1]:
            return hit[2]
    v = fn()
    with _lock:
        _CACHE[key] = (now, ttl_of(v), v)
        if len(_CACHE) > 500:
            for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:100]:
                _CACHE.pop(k, None)
    return v


def _ttl(v: dict) -> float:
    if not v.get("ok"):
        return 60
    return 120 if v.get("state") in ("open", "draft") else 3600


def ref_info(ref: str, fresh: bool = False) -> dict:
    """卡片用的摘要：类型、状态、标题、作者、时间、+/-、文件数。"""
    p = parse_ref(ref)
    if not p:
        return {"ok": False, "error": "不认识的引用"}
    o, r, n = p
    key = ("ref", o.lower(), r.lower(), n)
    if fresh:
        with _lock:
            _CACHE.pop(key, None)

    def get():
        rc, out, err = _gh(["api", f"repos/{o}/{r}/issues/{n}"])
        if rc != 0:
            return {"ok": False, "ref": f"{o}/{r}#{n}", "error": _why(err)}
        d = json.loads(out)
        base = {"ok": True, "ref": f"{o}/{r}#{n}", "repo": f"{o}/{r}", "number": n, "title": d.get("title"),
                "url": d.get("html_url"), "author": (d.get("user") or {}).get("login"),
                "avatar": (d.get("user") or {}).get("avatar_url"), "created": d.get("created_at"),
                "updated": d.get("updated_at"), "closed": d.get("closed_at"), "comments": d.get("comments", 0)}
        if not d.get("pull_request"):
            st = "closed" if d.get("state") == "closed" else "open"
            return dict(base, kind="issue", state=st, reason=d.get("state_reason"))
        rc, out, err = _gh(["api", f"repos/{o}/{r}/pulls/{n}"])
        if rc != 0:
            return dict(base, kind="pr", state=d.get("state"))
        pr = json.loads(out)
        st = "merged" if pr.get("merged") else "closed" if pr.get("state") == "closed" else "draft" if pr.get("draft") else "open"
        return dict(base, kind="pr", state=st, merged=pr.get("merged_at"), additions=pr.get("additions"),
                    deletions=pr.get("deletions"), files=pr.get("changed_files"), commits=pr.get("commits"),
                    head=(pr.get("head") or {}).get("ref"), base=(pr.get("base") or {}).get("ref"))
    return _cached(key, _ttl, get)


PR_FIELDS = ("number,title,body,state,isDraft,url,author,createdAt,updatedAt,mergedAt,closedAt,headRefName,baseRefName,"
             "additions,deletions,changedFiles,commits,files,reviews,comments,statusCheckRollup,labels,reviewDecision,mergeable")


def pr_detail(ref: str) -> dict:
    p = parse_ref(ref)
    if not p:
        return {"ok": False, "error": "不认识的 PR 引用"}
    o, r, n = p

    def get():
        rc, out, err = _gh(["pr", "view", str(n), "-R", f"{o}/{r}", "--json", PR_FIELDS], timeout=40)
        if rc != 0:
            return {"ok": False, "error": _why(err)}
        d = json.loads(out)
        checks = []
        for c in d.get("statusCheckRollup") or []:
            checks.append({"name": c.get("name") or c.get("context"), "workflow": c.get("workflowName"),
                           "status": c.get("status") or c.get("state"), "conclusion": c.get("conclusion") or c.get("state"),
                           "url": c.get("detailsUrl") or c.get("targetUrl")})
        st = "merged" if d.get("mergedAt") else "closed" if d.get("state") == "CLOSED" else "draft" if d.get("isDraft") else "open"
        return {"ok": True, "ref": f"{o}/{r}#{n}", "repo": f"{o}/{r}", "number": n, "state": st, "title": d.get("title"),
                "body": d.get("body") or "", "url": d.get("url"), "author": (d.get("author") or {}).get("login"),
                "created": d.get("createdAt"), "updated": d.get("updatedAt"), "merged": d.get("mergedAt"), "closed": d.get("closedAt"),
                "head": d.get("headRefName"), "base": d.get("baseRefName"), "additions": d.get("additions"),
                "deletions": d.get("deletions"), "files_n": d.get("changedFiles"), "review_decision": d.get("reviewDecision"),
                "mergeable": d.get("mergeable"), "labels": [x.get("name") for x in d.get("labels") or []],
                "commits": [{"oid": (c.get("oid") or "")[:7], "head": c.get("messageHeadline"),
                             "author": ((c.get("authors") or [{}])[0]).get("login") or ((c.get("authors") or [{}])[0]).get("name"),
                             "date": c.get("committedDate")} for c in d.get("commits") or []],
                "files": [{"path": f.get("path"), "additions": f.get("additions"), "deletions": f.get("deletions")} for f in d.get("files") or []],
                "reviews": [{"author": (x.get("author") or {}).get("login"), "state": x.get("state"), "body": x.get("body") or "",
                             "date": x.get("submittedAt")} for x in d.get("reviews") or []],
                "comments": [{"author": (x.get("author") or {}).get("login"), "body": x.get("body") or "", "date": x.get("createdAt")}
                             for x in d.get("comments") or []],
                "checks": checks}
    return _cached(("pr", o.lower(), r.lower(), n), lambda v: 60 if v.get("ok") and v["state"] in ("open", "draft") else (900 if v.get("ok") else 30), get)


def pr_diff(ref: str) -> dict:
    p = parse_ref(ref)
    if not p:
        return {"ok": False, "error": "不认识的 PR 引用"}
    o, r, n = p

    def get():
        rc, out, err = _gh(["pr", "diff", str(n), "-R", f"{o}/{r}", "--color=never"], timeout=60, limit=MAX_DIFF + 1)
        if rc != 0:
            return {"ok": False, "error": _why(err)}
        return {"ok": True, "diff": out[:MAX_DIFF], "truncated": len(out) > MAX_DIFF}
    return _cached(("diff", o.lower(), r.lower(), n), lambda v: 60 if v.get("ok") else 30, get)


# ---------- 项目（本地 git 仓库） ----------

def _git(root: Path, args: list[str], **kw) -> tuple[int, str, str]:
    git = shutil.which("git") or "git"
    return _run([git, "--no-optional-locks", "-c", "core.quotepath=off", "-c", "color.ui=never", *args], cwd=str(root), **kw)


GH_REMOTE = re.compile(r"github\.com[:/]+([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")


def repo_of(root: Path) -> str | None:
    """项目 git 远程（origin 优先）对应的 GitHub owner/repo。"""
    rc, out, _ = _git(root, ["remote", "-v"])
    if rc != 0:
        return None
    best = None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            m = GH_REMOTE.search(parts[1])
            if m and (best is None or parts[0] == "origin"):
                best = f"{m.group(1)}/{m.group(2)}"
                if parts[0] == "origin":
                    break
    return best


def pr_list(root: Path, state: str = "all") -> dict:
    repo = repo_of(root)
    if not repo:
        return {"ok": False, "error": "这个项目没有 GitHub 远程仓库（git remote）"}
    if state not in ("open", "closed", "merged", "all"):
        state = "all"

    def get():
        rc, out, err = _gh(["pr", "list", "-R", repo, "--state", state, "--limit", "40", "--json",
                            "number,title,state,isDraft,author,updatedAt,createdAt,mergedAt,additions,deletions,changedFiles,headRefName,baseRefName,url"], timeout=40)
        if rc != 0:
            return {"ok": False, "repo": repo, "error": _why(err)}
        rows = []
        for d in json.loads(out):
            st = "merged" if d.get("mergedAt") else "closed" if d.get("state") == "CLOSED" else "draft" if d.get("isDraft") else "open"
            rows.append({"ref": f"{repo}#{d['number']}", "number": d["number"], "title": d.get("title"), "state": st,
                         "author": (d.get("author") or {}).get("login"), "updated": d.get("updatedAt"), "merged": d.get("mergedAt"),
                         "additions": d.get("additions"), "deletions": d.get("deletions"), "files": d.get("changedFiles"),
                         "head": d.get("headRefName"), "base": d.get("baseRefName"), "url": d.get("url")})
        return {"ok": True, "repo": repo, "items": rows}
    return _cached(("list", str(root).lower(), state), lambda v: 60 if v.get("ok") else 20, get)


def _branch(root: Path) -> str | None:
    rc, out, _ = _git(root, ["rev-parse", "--abbrev-ref", "HEAD"])
    return out.strip() if rc == 0 else None


def bases(root: Path) -> list[str]:
    """能拿来比的基准：HEAD + 常见主干（本地 / origin 的）。"""
    out = ["HEAD"]
    rc, txt, _ = _git(root, ["for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes"])
    names = txt.split() if rc == 0 else []
    for n in ("origin/main", "main", "origin/master", "master", "origin/develop", "develop"):
        if n in names and n not in out:
            out.append(n)
    br = _branch(root)
    if br and br != "HEAD" and f"origin/{br}" in names and f"origin/{br}" not in out:
        out.append(f"origin/{br}")
    return out


def local_changes(root: Path, base: str = "HEAD") -> dict:
    """工作区相对 HEAD（或某个分支和 HEAD 的分叉点）的改动：文件列表 + 统一 diff（含没跟踪的新文件）。"""
    if not (root / ".git").exists():
        rc, _, _ = _git(root, ["rev-parse", "--git-dir"])
        if rc != 0:
            return {"ok": False, "error": "这个项目不是 git 仓库"}
    if base != "HEAD" and not re.fullmatch(r"[\w./-]+", base or ""):
        return {"ok": False, "error": "基准分支名不对"}
    rc, st, err = _git(root, ["status", "--porcelain=v1", "-b", "-uall"])
    if rc != 0:
        return {"ok": False, "error": err.strip()[:200] or "git status 出错"}
    head_line, files, untracked = "", [], []
    for line in st.splitlines():
        if line.startswith("##"):
            head_line = line[3:]
            continue
        code, path = line[:2], line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip('"')
        files.append({"code": code, "path": path})
        if code == "??":
            untracked.append(path)
    against = "HEAD"
    if base and base != "HEAD":
        rc, mb, err = _git(root, ["merge-base", base, "HEAD"])
        if rc != 0:
            return {"ok": False, "error": f"找不到 {base} 和当前分支的分叉点"}
        against = mb.strip()
    rc, diff, err = _git(root, ["diff", against, "--no-ext-diff", "-M"], timeout=60, limit=MAX_DIFF + 1)
    if rc != 0:
        rc2, _, _ = _git(root, ["rev-parse", "--verify", "HEAD"])
        if rc2 != 0:     # 还没有提交：跟空树比
            rc, diff, err = _git(root, ["diff", "--cached", "--no-ext-diff"], timeout=60, limit=MAX_DIFF + 1)
    # 没跟踪的新文件也当成新增画出来（只读文本、小文件，最多 30 个）
    extra = []
    for p in untracked[:30]:
        f = root / p
        try:
            if not f.is_file() or f.stat().st_size > 200_000:
                extra.append(f"diff --git a/{p} b/{p}\nnew file mode 100644\n--- /dev/null\n+++ b/{p}\n@@ 文件太大或不是普通文件，没有展开 @@\n")
                continue
            b = f.read_bytes()
            if b"\x00" in b[:8000]:
                extra.append(f"diff --git a/{p} b/{p}\nnew file mode 100644\nBinary files /dev/null and b/{p} differ\n")
                continue
            lines = b.decode("utf-8", "replace").splitlines()
            extra.append(f"diff --git a/{p} b/{p}\nnew file mode 100644\n--- /dev/null\n+++ b/{p}\n@@ -0,0 +1,{len(lines)} @@\n"
                         + "".join("+" + x + "\n" for x in lines))
        except Exception:
            continue
    text = diff + "".join(extra)
    rc, stat, _ = _git(root, ["diff", against, "--shortstat"])
    rc, log, _ = _git(root, ["log", "--oneline", "-n", "30", f"{against}..HEAD"]) if against != "HEAD" else (0, "", "")
    return {"ok": True, "root": str(root), "branch": _branch(root), "status": head_line, "base": base, "against": against[:12],
            "bases": bases(root), "files": files, "untracked": len(untracked), "diff": text[:MAX_DIFF],
            "truncated": len(text) > MAX_DIFF, "shortstat": stat.strip(), "commits": [l for l in log.splitlines() if l.strip()],
            "repo": repo_of(root)}


def review_prompt(ref: str | None, local: dict | None, note: str = "") -> str:
    """发给会话的「请审查」正文（只读审查，不许推送 / 评论 / 批准 / 合并）。"""
    rule = "只读审查：不要改代码、不要推送、不要在 GitHub 上评论 / 批准 / 合并。看完把问题按严重程度（必须改 / 建议改 / 小问题）列出来，写清文件和行号，回复我。"
    if ref:
        info = ref_info(ref)
        o, r, n = parse_ref(ref)
        head = f"请帮我审查 PR {o}/{r}#{n}"
        if info.get("ok"):
            head += f"：{info.get('title')}"
            if info.get("head"):
                head += f"（{info.get('head')} → {info.get('base')}，+{info.get('additions')} −{info.get('deletions')}，{info.get('files')} 个文件）"
        how = f"看改动：gh pr view {n} -R {o}/{r}；gh pr diff {n} -R {o}/{r}" + (f"\n{info['url']}" if info.get("url") else "")
        txt = f"{head}\n{how}\n{rule}"
    else:
        base = (local or {}).get("base") or "HEAD"
        cmd = "git diff HEAD" if base == "HEAD" else f"git diff $(git merge-base {base} HEAD)"
        files = (local or {}).get("files") or []
        lst = "\n".join(f"- {f['code'].strip() or 'M'} {f['path']}" for f in files[:40]) + ("\n- …" if len(files) > 40 else "")
        txt = (f"请帮我审查本地还没提交的改动（分支 {(local or {}).get('branch') or '?'}，对比 {base}；{(local or {}).get('shortstat') or ''}）。\n"
               f"看改动：{cmd}（没跟踪的新文件用 git status 看）\n改了这些文件：\n{lst}\n{rule}")
    if note.strip():
        txt += "\n补充：" + note.strip()
    return txt
