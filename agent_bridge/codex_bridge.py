#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 Codex 线程交给 Claude Code 接着做。

    python codex_bridge.py list                         最近的 Codex 线程（只列用户自己开的）
    python codex_bridge.py export <线程号或末几位> [--out 目录]   B：导出成交接 Markdown（对话 + 跑过的命令 + 当前 git 状态）
                                                        缺省导到线程工作目录的 .msgbus/exports/，没有就 ~/.agent-bridge/exports/
    python codex_bridge.py to-claude <线程号或末几位>     A：转成 Claude Code 会话记录，打印 claude --resume 命令

B 是推荐做法：不依赖任何没公开的格式，新开 Claude 会话说「读这个文件接着做」。
A 是实验性的：按本机 Claude Code 会话文件的样子拼一份记录（只有对话文字，工具过程折成说明），
Claude Code 升级改了格式就可能认不出来。
只用标准库。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

TOOL = Path(__file__).resolve().parent
DATA = Path(os.environ.get("AGENT_BRIDGE_HOME") or Path.home() / ".agent-bridge")
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def out_dir(cwd: str | None, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    if cwd:
        for bus in (".msgbus", ".temp/msgbus"):
            if (Path(cwd) / bus / "bus.py").exists():
                return Path(cwd) / bus / "exports"
    return DATA / "exports"
SKIP = ("# AGENTS.md instructions", "<environment_context", "<app-context", "<user_instructions", "<INSTRUCTIONS",
        "<permissions", "<skills_instructions", "<turn_aborted")
MAX_OUT_LINES = 40


# ---------- 找线程 ----------

def rollouts():
    for base in (CODEX_HOME / "sessions", CODEX_HOME / "archived_sessions"):
        yield from base.rglob("rollout-*.jsonl")


def meta_of(p: Path) -> dict:
    try:
        with open(p, "rb") as f:
            return json.loads(f.readline()).get("payload", {})
    except Exception:
        return {}


def titles() -> dict:
    out = {}
    try:
        for line in (CODEX_HOME / "session_index.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
                out[d["id"]] = d.get("thread_name")
            except Exception:
                pass
    except FileNotFoundError:
        pass
    return out


def find_thread(key: str) -> Path:
    key = key.lower()
    hits = [p for p in rollouts() if p.stem.lower().endswith(key) or key in p.stem.lower()]
    if not hits:
        sys.exit(f"找不到 Codex 线程「{key}」。先跑 list 看看。")
    ids = {p.stem[-36:] for p in hits}
    if len(ids) > 1:
        sys.exit(f"「{key}」对上了 {len(ids)} 个线程，写长一点：\n  " + "\n  ".join(sorted(ids)))
    return max(hits, key=lambda p: p.stat().st_mtime)


def cmd_list(args):
    t = titles()
    rows = []
    for p in rollouts():
        m = meta_of(p)
        if m.get("thread_source") != "user" or m.get("originator") == "codex_exec":
            continue
        rows.append((p.stat().st_mtime, m.get("id", p.stem[-36:]), m.get("cwd"), t.get(m.get("id"), "")))
    for mt, tid, cwd, title in sorted(rows, reverse=True)[:args.n]:
        print(f"{datetime.fromtimestamp(mt):%m-%d %H:%M}  {tid[-6:]}  {title or '（无标题）'}  ·  {cwd}")


# ---------- 解析成轮次 ----------

def _clean_user(t: str) -> str | None:
    t = (t or "").strip()
    return None if (not t or t.startswith(SKIP)) else t


def parse(p: Path) -> dict:
    """→ {meta, items:[{role:user|assistant|tool, text, ts, name?, args?, output?}]}"""
    items, calls = [], {}
    meta = {}
    for line in open(p, encoding="utf-8"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        ts = d.get("timestamp")
        if d.get("type") == "session_meta":
            meta = d.get("payload", {})
            continue
        if d.get("type") != "response_item":
            continue
        pl = d.get("payload") or {}
        pt = pl.get("type")
        if pt == "message" and pl.get("role") in ("user", "assistant"):
            text = "\n".join(x.get("text", "") for x in pl.get("content") or []
                             if isinstance(x, dict) and x.get("type") in ("input_text", "output_text", "text"))
            if pl["role"] == "user":
                text = _clean_user(text)
            if text and text.strip():
                items.append({"role": pl["role"], "text": text.strip(), "ts": ts})
        elif pt in ("function_call", "custom_tool_call", "local_shell_call"):
            raw = pl.get("arguments") or pl.get("input") or pl.get("action") or ""
            try:
                a = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                a = raw
            if isinstance(a, dict):
                cmd = a.get("cmd") or a.get("command") or a.get("input") or json.dumps(a, ensure_ascii=False)
            else:
                cmd = str(a)
            if isinstance(cmd, list):
                cmd = " ".join(map(str, cmd))
            it = {"role": "tool", "name": pl.get("name") or pt, "args": str(cmd), "output": None, "ts": ts}
            items.append(it)
            if pl.get("call_id"):
                calls[pl["call_id"]] = it
        elif pt in ("function_call_output", "custom_tool_call_output"):
            it = calls.get(pl.get("call_id"))
            if it is not None:
                o = pl.get("output")
                if isinstance(o, dict):
                    o = o.get("content") or json.dumps(o, ensure_ascii=False)
                it["output"] = str(o or "")
    return {"meta": meta, "items": items}


# ---------- 当前 git 状态 ----------

def git(cwd: Path, *a) -> str:
    try:
        r = subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, timeout=20)
        return r.stdout.decode("utf-8", "replace").strip()
    except Exception:
        return ""


def repo_roots(conv: dict) -> list[Path]:
    """对话里提到过的、在工作目录下的 git 仓 / worktree（最多 8 个）。"""
    cwd = Path(conv["meta"].get("cwd") or ".")
    blob = "\n".join(it.get("args", "") + "\n" + it.get("text", "") for it in conv["items"])
    pat = re.compile(re.escape(str(cwd)).replace(r"\\", r"[\\/]") + r"[\\/][^\s\"'`<>|;]+", re.I)
    roots: list[Path] = []
    cands = [cwd] + [Path(m.group(0).rstrip(".,)")) for m in pat.finditer(blob)]
    for c in cands:
        for q in [c, *c.parents]:
            if len(q.parts) < len(cwd.parts):
                break
            if (q / ".git").exists():
                if q not in roots:
                    roots.append(q)
                break
    return roots[:8]


def git_state(roots: list[Path]) -> str:
    if not roots:
        return "_（对话里没找到 git 仓；工作目录不是 git 仓）_\n"
    out = []
    for r in roots:
        if not r.exists():
            out.append(f"- `{r}` —— **目录已不存在**（worktree 可能已清理）")
            continue
        br = git(r, "rev-parse", "--abbrev-ref", "HEAD")
        head = git(r, "log", "-1", "--format=%h %s")
        st = git(r, "status", "--short")
        ahead = git(r, "rev-list", "--count", "origin/main..HEAD")
        out.append(f"- `{r}`\n  - 分支 `{br}`，HEAD `{head}`\n  - 比 origin/main 多 {ahead or '?'} 个提交；"
                   f"未提交改动 {len(st.splitlines()) if st else 0} 个" +
                   (("\n  ```\n  " + "\n  ".join(st.splitlines()[:30]) + "\n  ```") if st else ""))
    return "\n".join(out) + "\n"


# ---------- B：导出 Markdown ----------

def fmt_ts(ts) -> str:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M")
    except Exception:
        return ""


def cmd_export(args):
    p = find_thread(args.thread)
    conv = parse(p)
    m = conv["meta"]
    tid = m.get("id", p.stem[-36:])
    title = titles().get(tid) or "（无标题）"
    items = conv["items"]
    users = [i for i in items if i["role"] == "user"]
    tools = [i for i in items if i["role"] == "tool"]
    lines = [
        f"# Codex 线程交接：{title}",
        "",
        "> **给接手的 Claude：** 这是一个 Codex 线程的完整导出。先读「当前状态」核实分支 / 未提交改动 / PR，",
        "> 再从「最后几轮」看它停在哪一步，然后接着做。以文件和 git 为准，不要只信对话里的说法；",
        "> 项目规矩照 `CLAUDE.md` / `AGENTS.md`。有拿不准的先问用户。",
        "",
        f"- 线程：`{tid}`（Codex 末 6 位 `{tid[-6:]}`）",
        f"- 工作目录：`{m.get('cwd')}`",
        f"- 时间：{fmt_ts(items[0]['ts']) if items else ''} → {fmt_ts(items[-1]['ts']) if items else ''}",
        f"- 规模：你说了 {len(users)} 次，Codex 回复 {sum(1 for i in items if i['role'] == 'assistant')} 次，跑了 {len(tools)} 次命令",
        f"- 源文件：`{p}`",
        f"- 导出于：{datetime.now():%Y-%m-%d %H:%M}",
        "",
        "## 当前状态（导出时现查的 git）",
        "",
        git_state(repo_roots(conv)),
        "## 最后几轮",
        "",
    ]
    tail = [i for i in items if i["role"] != "tool"][-6:]
    for i in tail:
        who = "你" if i["role"] == "user" else "Codex"
        lines.append(f"**{who}**（{fmt_ts(i['ts'])}）：{i['text'][:1500]}\n")
    lines += ["## 完整过程", ""]
    for i in items:
        if i["role"] == "tool":
            out = (i.get("output") or "").strip().splitlines()
            more = f"\n…（输出共 {len(out)} 行，只留前 {MAX_OUT_LINES} 行）" if len(out) > MAX_OUT_LINES else ""
            lines.append(f"<details><summary>🔧 {i['name']} · <code>{_html(i['args'][:160])}</code></summary>\n\n"
                         f"```\n{i['args'][:4000]}\n```\n\n输出：\n\n```\n" + "\n".join(out[:MAX_OUT_LINES]) + more + "\n```\n</details>\n")
        else:
            who = "### 🧑 你" if i["role"] == "user" else "### 🤖 Codex"
            lines.append(f"{who}（{fmt_ts(i['ts'])}）\n\n{i['text']}\n")
    OUT = out_dir(m.get("cwd"), args.out)
    OUT.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|\s]+', "-", title)[:40].strip("-") or "codex"
    f = OUT / f"{datetime.now():%Y%m%d-%H%M}-{safe}-{tid[-6:]}.md"
    f.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"ok": True, "file": str(f), "title": title, "thread": tid,
                      "prompt": f"读 {f} ，这是 Codex 线程「{title}」的交接。先核实里面「当前状态」，再接着把它没做完的活做完。"},
                     ensure_ascii=False))


def _html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------- A：转成 Claude Code 会话 ----------

def claude_slug(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def iso(ts) -> str:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except Exception:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def cmd_to_claude(args):
    p = find_thread(args.thread)
    conv = parse(p)
    m = conv["meta"]
    tid = m.get("id", p.stem[-36:])
    title = titles().get(tid) or "Codex 线程"
    cwd = m.get("cwd") or os.getcwd()
    cwd = str(Path(cwd).resolve())
    # 合并成严格交替的 user / assistant：工具调用折成助手回复里的一段说明
    turns: list[list] = []            # [role, [片段…], ts]

    def push(role, text, ts):
        if turns and turns[-1][0] == role:
            turns[-1][1].append(text)
        else:
            turns.append([role, [text], ts])
    head = (f"（以下是从 Codex 线程「{title}」`{tid}` 导入的对话，工作目录 {cwd}。"
            f"Codex 跑过的命令只留了摘要；接着做之前先用 git / 文件核实当前状态。）")
    push("user", head, conv["items"][0]["ts"] if conv["items"] else None)
    for i in conv["items"]:
        if i["role"] == "tool":
            push("assistant", f"〔在 Codex 里跑过：{i['name']} `{i['args'][:300]}`〕", i["ts"])
        else:
            push(i["role"], i["text"], i["ts"])
    if turns and turns[0][0] == "user" and len(turns) > 1 and turns[1][0] == "user":
        pass
    if turns[-1][0] == "user":        # 最后一条要是助手，resume 时新问题才能接上
        push("assistant", "（导入到此为止。）", turns[-1][2])
    sid = str(uuid.uuid4())
    version = args.version
    recs, parent = [], None
    for role, parts, ts in turns:
        u = str(uuid.uuid4())
        text = "\n\n".join(parts)
        base = {"parentUuid": parent, "isSidechain": False, "userType": "external", "cwd": cwd, "sessionId": sid,
                "version": version, "gitBranch": "HEAD", "type": role, "uuid": u, "timestamp": iso(ts)}
        if role == "user":
            base["message"] = {"role": "user", "content": text}
        else:
            base["message"] = {"id": "msg_imported_" + u.replace("-", "")[:20], "type": "message", "role": "assistant",
                               "model": "imported-from-codex", "content": [{"type": "text", "text": text}],
                               "stop_reason": "end_turn", "stop_sequence": None,
                               "usage": {"input_tokens": 0, "output_tokens": 0}}
        recs.append(base)
        parent = u
    recs.append({"type": "custom-title", "customTitle": f"（从 Codex 导入）{title}", "sessionId": sid})
    d = CLAUDE_HOME / "projects" / claude_slug(cwd)
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{sid}.jsonl"
    with open(f, "w", encoding="utf-8", newline="\n") as fh:
        for r in recs:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(json.dumps({"ok": True, "session": sid, "file": str(f), "turns": len(turns), "cwd": cwd,
                      "resume": f'claude --resume {sid}'}, ensure_ascii=False))


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="把 Codex 线程交给 Claude Code")
    sub = ap.add_subparsers(dest="cmd", required=True)
    l = sub.add_parser("list")
    l.add_argument("-n", type=int, default=15)
    e = sub.add_parser("export")
    e.add_argument("thread")
    e.add_argument("--out", help="导出目录")
    t = sub.add_parser("to-claude")
    t.add_argument("thread")
    t.add_argument("--version", default="2.1.281", help="写进记录的 Claude Code 版本号")
    a = ap.parse_args()
    {"list": cmd_list, "export": cmd_export, "to-claude": cmd_to_claude}[a.cmd](a)


if __name__ == "__main__":
    main()
