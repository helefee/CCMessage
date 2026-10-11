# -*- coding: utf-8 -*-
"""读 Claude / Codex / Cursor 会话记录，整理成界面能直接画的条目。

条目：{"pos": 字节位置, "ts": 秒, "role": "user"|"assistant"|"tool", "text": …}
  - user：人在会话里说的话（系统注入的 AGENTS.md / system-reminder / 环境上下文 都去掉）
  - assistant：模型的回复正文（思考过程不要）
  - tool：一次工具调用的一行摘要（工具名 + 命令 / 说明），结果不要
  - tool 还带 name / arg / id / input（输入的头）；tool_out：工具的输出（按 id 合到那次调用上，界面不单独画）
  - bus_in：经消息总线送到这个会话的一条消息（钩子注入 / 闲着被叫醒 / codex queue 提醒），
    带 key（去重用：同一条可能同时记在几处）、kind、frm、user（是不是用户本人从网页 / 手机发的）、via、to、mid、text
  - tool 里如果是总线命令（bus.py send / task / notice / done），多带 bus：[{kind, to, text}]，界面画成发出去的消息
  - subtask：Claude Code 派出去的一个子代理（Task / Agent 工具）：说明、类型、提示词，界面画成单独的卡片
  - subtask_upd：子任务的状态更新（启动回执 / 完成通知 / 交回来的报告），按 id（tool_use id）或 agent（子代理号）
    合到对应卡片上，界面不单独画
子代理自己的记录在 ~/.claude/projects/<项目>/<会话号>/subagents/agent-<子代理号>.jsonl，
旁边 agent-<子代理号>.meta.json 里 toolUseId 就是主记录里那次 Agent 调用的 id（见 subtask_info / read_subtask）。
首次读文件尾部 TAIL 字节；之后按字节位置增量读，只消费完整的行。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

TAIL = 3 * 1024 * 1024
MAX_TEXT = 6000
from .paths import CLAUDE_HOME, CODEX_HOME

SKIP_USER_PREFIX = ("<system-reminder", "<command-", "<local-command", "# AGENTS.md instructions", "<environment_context",
                    "<app-context", "<user_instructions", "<INSTRUCTIONS", "Caveat:", "<task-notification", "<permissions", "<turn_aborted", "<user_shell_command",
                    "[SYSTEM NOTIFICATION")


def _ts(s) -> float:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _clip(t: str) -> str:
    t = t.strip()
    return t if len(t) <= MAX_TEXT else t[:MAX_TEXT] + "\n…（后面省略）"


def _user_text(t: str) -> str | None:
    t = (t or "").strip()
    if not t or t.startswith(SKIP_USER_PREFIX):
        return None
    return t


# ---------- 找文件 ----------

def claude_file(sid: str, hint: str | None = None) -> Path | None:
    if hint and Path(hint).exists():
        return Path(hint)
    hits = list((CLAUDE_HOME / "projects").glob(f"*/{sid}.jsonl"))
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


def cursor_file(sid: str, hint: str | None = None) -> Path | None:
    if hint and Path(hint).exists():
        return Path(hint)
    hits = list((Path.home() / ".cursor" / "projects").glob(f"*/agent-transcripts/{sid}/{sid}.jsonl"))
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


def codex_file(sid: str) -> Path | None:
    for base in (CODEX_HOME / "sessions", CODEX_HOME / "archived_sessions"):
        hits = list(base.rglob(f"rollout-*{sid}.jsonl"))
        if hits:
            return max(hits, key=lambda p: p.stat().st_mtime)
    return None


# ---------- 解析一行 ----------

def _tool_args(inp):
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except Exception:
            inp = {"arg": inp}
    return inp if isinstance(inp, dict) else {}


def _tool_brief(inp) -> str:
    inp = _tool_args(inp)
    brief = (inp.get("description") or inp.get("cmd") or inp.get("command") or inp.get("file_path")
             or inp.get("pattern") or inp.get("prompt") or inp.get("url") or inp.get("query") or "")
    if isinstance(brief, list):
        brief = " ".join(map(str, brief))
    return " ".join(str(brief).split())[:160]


def _tool_line(name: str, inp) -> str:
    brief = _tool_brief(inp)
    return f"{name} · {brief}" if brief else name


TOOL_IN, TOOL_OUT = 800, 1200      # 工具的输入 / 输出只带个头，展开时看个大概
TOOL_PATCH = 8000                  # Codex 的 apply_patch 补丁多带点：网页上要按 diff 画出来、数 +N −M


def _cut(t: str, n: int) -> str:
    t = (t or "").rstrip()
    return t if len(t) <= n else t[:n] + f"\n…（共 {len(t)} 字，后面省略）"


def _tool_input(inp) -> str:
    """展开工具调用时显示的输入：命令类直接给命令，其余给缩进的 JSON。"""
    a = _tool_args(inp)
    for k in ("command", "cmd"):
        v = a.get(k)
        if isinstance(v, list):
            v = " ".join(map(str, v))
        if isinstance(v, str) and v.strip():
            return _cut(v, TOOL_IN)
    if set(a) == {"arg"}:
        t = str(a["arg"])
        return _cut(t, TOOL_PATCH if t.lstrip().startswith("*** Begin Patch") else TOOL_IN)
    if isinstance(a.get("input"), str) and a["input"].lstrip().startswith("*** Begin Patch"):
        return _cut(a["input"], TOOL_PATCH)
    try:
        return _cut(json.dumps(a, ensure_ascii=False, indent=1), TOOL_IN)
    except Exception:
        return ""


def _tool(name: str, inp, ts: float, tid=None) -> dict:
    e = {"ts": ts, "role": "tool", "text": _tool_line(name, inp), "name": name, "arg": _tool_brief(inp),
         "id": tid, "input": _tool_input(inp)}
    a = _tool_args(inp)
    bus = _bus_cmds(a.get("command") or a.get("cmd") or "")
    if bus:
        e["bus"] = bus
    return e


# ---------- 消息总线（bus.py）：收到的、发出去的 ----------
# 收到的消息在记录里不是用户那一轮，而是钩子塞进上下文的文字（hook_additional_context / hook_success 的 stdout），
# 或闲着被叫醒时那条 <task-notification>…<system-reminder>📨 msgbus：…；格式见 bus.py 的 render_one。

_BUS_HEAD = re.compile(r"^【(来自 |任务 #|公告 · |结果 · |用户本人 · )[^\n]*】$", re.M)
_BUS_TAIL = re.compile(r"\n（(做完回报：|这是用户本人)[^\n]*）\s*$")


def _bus_parse(text: str, ts: float) -> list[dict]:
    """把一段注入的文字拆成一条条总线消息。"""
    text = (text or "").split("</system-reminder>")[0]
    heads = list(_BUS_HEAD.finditer(text))
    out = []
    for i, h in enumerate(heads):
        body = text[h.end():heads[i + 1].start() if i + 1 < len(heads) else len(text)].strip("\n")
        body = re.split(r"\n…（截断；", body)[0]
        body = _BUS_TAIL.sub("", body).strip()
        hd = h.group(0)[1:-1]
        parts = [x.strip() for x in hd.split(" · ")]
        e = {"ts": ts, "role": "bus_in", "kind": "msg", "frm": "", "user": False, "via": "", "to": "", "mid": "",
             "text": _clip(body)}
        m = re.search(r"#([0-9a-f]{6,})", hd)
        if hd.startswith("任务 #"):
            e["kind"] = "task"
        elif hd.startswith("公告"):
            e["kind"] = "notice"
        elif hd.startswith("结果"):
            e["kind"] = "result"
        if m:
            e["mid"] = m.group(1)
        for x in parts:
            if x.startswith("来自 "):
                e["frm"] = x[3:]
            elif x.startswith("发给 "):
                e["to"] = x[3:]
            elif x.startswith("经 "):
                e["via"] = x[2:]
            elif "用户本人" in x:
                e["user"] = True
                v = re.search(r"经 ([^）]+)", x)
                if v:
                    e["via"] = v.group(1)
        if "[user:" in e["frm"]:             # 旧版 bus.py：用户发的也写成「来自 用户 [user:ui]」
            e["user"] = True
            e["via"] = "agent-bridge 手机端" if "[user:phone]" in e["frm"] else "agent-bridge 网页"
        if e["user"]:
            e["frm"] = "用户"
        e["key"] = (e["kind"] + ":" + e["mid"]) if e["mid"] else "h:" + hd + "|" + body[:60]
        out.append(e)
    return out


def _bus_codex_note(t: str, ts: float) -> list[dict]:
    """Codex 被 codex queue 叫醒时那条提醒（见 bus.py wake_codex）：只有预览，全文在 inbox 里。"""
    m = re.match(r"(📨 msgbus：|📋 msgbus 任务 #|✅ msgbus：)(.*)", t or "", re.S)
    if not m:
        return []
    first = m.group(2).split("\n", 1)[0]
    e = {"ts": ts, "role": "bus_in", "kind": "msg", "frm": "", "user": False, "via": "", "to": "", "mid": "", "note": True}
    if m.group(1).startswith("📋"):
        e["kind"] = "task"
        g = re.match(r"([0-9a-f]+)，(.*?) 派给你：(.*)", first, re.S)
        if g:
            e["mid"], e["frm"], e["text"] = g.group(1), g.group(2), g.group(3)
    elif m.group(1).startswith("✅"):
        e["kind"] = "result"
        g = re.match(r"你派的任务 #([0-9a-f]+) 有结果了（(.*?)）：(.*)", first, re.S)
        if g:
            e["mid"], e["frm"], e["text"] = g.group(1), g.group(2), g.group(3)
    else:
        g = re.match(r"(.*?) 给你发来一条消息（#([0-9a-f]+)）：(.*)", first, re.S)
        if g:
            e["frm"], e["mid"], e["text"] = g.group(1), g.group(2), g.group(3)
    if not e.get("text"):
        return []
    v = re.match(r"用户本人经 (.+)", e["frm"])
    if v:
        e.update(user=True, via=v.group(1), frm="用户")
    e["key"] = e["kind"] + ":" + e["mid"]
    return [e]


_BUS_SUBS = ("send", "task", "notice", "done")
_BUS_VALOPT = ("--to", "--as", "--wait", "--max-secs", "--files", "--branch", "--pr")


def _bus_cmds(cmd) -> list[dict]:
    """命令里的总线发信（bus.py send / task / notice / done）。拆不开的就算了，照普通命令画。"""
    if isinstance(cmd, list):
        cmd = " ".join(map(str, cmd))
    cmd = str(cmd or "")
    if "bus.py" not in cmd or not re.search(r"bus\.py[\"']?\s+(--as\s+\S+\s+)?(send|task|notice|done)\b", cmd):
        return []
    import shlex
    first_line = cmd.split("\n", 1)[0] if "<<" in cmd else cmd
    try:
        lx = shlex.shlex(first_line, posix=True, punctuation_chars=";&|<>()")
        lx.whitespace_split = True
        toks = list(lx)
    except Exception:
        return []
    out = []
    for i, t in enumerate(toks):
        if not t.rstrip("\"'").endswith("bus.py"):
            continue
        j, args = i + 1, []
        while j < len(toks) and not re.fullmatch(r"[;&|<>()]+", toks[j]):
            args.append(toks[j]); j += 1
        if args and args[-1].isdigit() and j < len(toks) and toks[j][:1] in "<>":
            args.pop()                          # 2>&1 里的 2
        while args[:1] == ["--as"]:
            args = args[2:]
        if not args or args[0] not in _BUS_SUBS:
            continue
        sub, rest, pos, to = args[0], args[1:], [], ""
        k = 0
        while k < len(rest):
            a = rest[k]
            if a in _BUS_VALOPT:
                if a == "--to" and k + 1 < len(rest):
                    to = rest[k + 1]
                k += 2
                continue
            if not a.startswith("--") or a == "-":
                pos.append(a)
            k += 1
        if sub == "done":
            if not pos:
                continue
            e = {"kind": "result", "to": "任务 #" + pos[0], "text": " ".join(pos[1:])}
        else:
            e = {"kind": {"send": "msg", "task": "task", "notice": "notice"}[sub], "to": to or ("所有会话" if sub == "notice" else ""),
                 "text": " ".join(pos)}
        if e["text"] in ("", "-"):
            h = re.search(r"<<-?\s*['\"]?(\w+)['\"]?\n(.*?)\n\1\b", cmd, re.S)
            e["text"] = h.group(2) if h else "（正文从标准输入读）"
        e["text"] = _cut(e["text"], 3000)
        out.append(e)
    return out


SUBTASK_TOOLS = ("Task", "Agent")
_NOTE_RE = re.compile(r"<(task-id|tool-use-id|status|summary)>(.*?)</\1>", re.S)
_STATUS = {"completed": "done", "failed": "failed", "error": "failed", "killed": "stopped", "stopped": "stopped",
           "cancelled": "stopped", "async_launched": "running", "running": "running"}


def _task_note(t: str) -> dict | None:
    """<task-notification> 完成通知 → subtask_upd。后台命令也发这个，界面按 id 对不上就不管。"""
    f = dict(_NOTE_RE.findall(t or ""))
    if not f.get("tool-use-id"):
        return None
    return {"role": "subtask_upd", "id": f["tool-use-id"].strip(), "agent": (f.get("task-id") or "").strip() or None,
            "status": _STATUS.get((f.get("status") or "").strip(), (f.get("status") or "").strip() or None),
            "summary": (f.get("summary") or "").strip()[:300]}


def _handback_text(t: str) -> str:
    """「Another Claude session sent a message: <agent-message …> … The report follows:」后面每行缩进两格的报告正文。"""
    k = t.find("The report follows:")
    body = t[k + len("The report follows:"):] if k >= 0 else t
    body = body.split("</agent-message>")[0]
    return "\n".join(x[2:] if x.startswith("  ") else x for x in body.strip("\n").split("\n")).strip()


def _subtask(x: dict, ts: float) -> dict:
    inp = x.get("input") if isinstance(x.get("input"), dict) else {}
    pr = str(inp.get("prompt") or "")
    return {"ts": ts, "role": "subtask", "id": x.get("id"), "description": str(inp.get("description") or "")[:200],
            "subagent_type": inp.get("subagent_type") or "general-purpose", "model": inp.get("model"),
            "bg": bool(inp.get("run_in_background")), "text": str(inp.get("description") or "子任务"),
            "prompt": pr if len(pr) <= 3000 else pr[:3000] + "\n…（后面省略）"}


def _result_text(c) -> str:
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(str(y.get("text", "")) for y in c if isinstance(y, dict) and y.get("type") == "text")
    return ""


def parse_claude(d: dict, side: bool = False) -> list[dict]:
    """side=True：读子代理自己的记录（每行都标着 isSidechain）。"""
    if d.get("isSidechain") and not side:
        return []
    t, msg, ts = d.get("type"), d.get("message") or {}, _ts(d.get("timestamp"))
    if t == "attachment":
        a = d.get("attachment") or {}
        if a.get("type") == "queued_command" and str(a.get("prompt") or "").startswith("<task-notification"):
            u = _task_note(a["prompt"])
            return [dict(u, ts=ts)] if u else []
        if a.get("type") == "hook_additional_context":
            c = a.get("content")
            return [e for x in (c if isinstance(c, list) else [c]) if "📨" in str(x or "") for e in _bus_parse(str(x), ts)]
        if a.get("type") == "hook_success" and "📨" in str(a.get("stdout") or ""):
            try:      # 老版本 Claude Code 只在这里记钩子的输出；新版本另有 hook_additional_context，界面按 key 去重
                ctx = (json.loads(a["stdout"]).get("hookSpecificOutput") or {}).get("additionalContext") or ""
            except Exception:
                return []
            return _bus_parse(ctx, ts) if "📨" in ctx else []
        return []
    if d.get("isMeta"):
        o = d.get("origin") or {}
        if t == "user" and o.get("kind") == "peer" and o.get("from"):
            # 后台子代理干完交回来的报告
            return [{"ts": ts, "role": "subtask_upd", "agent": o["from"], "status": "done",
                     "result": _clip(_handback_text(_result_text(msg.get("content"))))}]
        return []
    out = []
    if t == "user":
        c = msg.get("content")
        if isinstance(c, str):
            if c.startswith("<task-notification"):
                if "msgbus" in c[:400]:          # 闲着时被总线叫醒（Stop 钩子 asyncRewake）
                    return _bus_parse(c, ts)
                u = _task_note(c)
                return [dict(u, ts=ts)] if u else []
            u = _user_text(c)
            if u:
                out.append({"ts": ts, "role": "user", "text": _clip(u)})
                if side and d.get("parentUuid") is None:
                    out[-1]["is_prompt"] = True        # 子代理记录的第一句就是派活的提示词，卡片里已经单独显示
        elif isinstance(c, list):
            texts = [x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text"]
            u = _user_text("\n".join(texts))
            if u:
                out.append({"ts": ts, "role": "user", "text": _clip(u)})
            r = d.get("toolUseResult")
            for x in c:
                if not (isinstance(x, dict) and x.get("type") == "tool_result"):
                    continue
                if isinstance(r, dict) and r.get("agentId"):
                    st = "failed" if x.get("is_error") else _STATUS.get(r.get("status"), "running")
                    hb = (r.get("handbackReport") or {}).get("text") if isinstance(r.get("handbackReport"), dict) else None
                    res = hb or (_result_text(x.get("content")) if st != "running" else "")
                    out.append({"ts": ts, "role": "subtask_upd", "id": x.get("tool_use_id"), "agent": r["agentId"],
                                "status": st, "result": _clip(res) if res else None,
                                "duration": (r.get("totalDurationMs") or 0) / 1000 or None,
                                "tools": r.get("totalToolUseCount")})
                else:
                    # 普通工具的输出（界面展开那次调用时看）
                    out.append({"ts": ts, "role": "tool_out", "id": x.get("tool_use_id"), "err": bool(x.get("is_error")),
                                "out": _cut(_result_text(x.get("content")), TOOL_OUT)})
                    if x.get("is_error"):
                        # 不知道是不是子任务：带个 id 发过去，界面对不上就丢
                        out.append({"ts": ts, "role": "subtask_upd", "id": x.get("tool_use_id"), "status": "failed",
                                    "result": _clip(_result_text(x.get("content")))[:1500]})
    elif t == "assistant":
        for x in msg.get("content") or []:
            if not isinstance(x, dict):
                continue
            if x.get("type") == "text" and x.get("text", "").strip():
                out.append({"ts": ts, "role": "assistant", "text": _clip(x["text"])})
            elif x.get("type") == "tool_use" and x.get("name") in SUBTASK_TOOLS:
                out.append(_subtask(x, ts))
            elif x.get("type") == "tool_use":
                out.append(_tool(x.get("name", "工具"), x.get("input"), ts, x.get("id")))
    return out


def parse_cursor(d: dict) -> list[dict]:
    """Cursor 的记录每行 {"role", "message": {"content": [...]}}，没有时间戳；用户那句在 <user_query> 里。"""
    import re
    role, out = d.get("role"), []
    for x in (d.get("message") or {}).get("content") or []:
        if not isinstance(x, dict):
            continue
        if x.get("type") == "text" and (x.get("text") or "").strip():
            t = x["text"]
            if role == "user":
                q = re.search(r"<user_query>(.*?)</user_query>", t, re.S)
                u = _user_text(q.group(1) if q else re.sub(r"<timestamp>.*?</timestamp>", "", t, flags=re.S))
                if u:
                    out.append({"ts": 0, "role": "user", "text": _clip(u)})
            elif role == "assistant":
                out.append({"ts": 0, "role": "assistant", "text": _clip(t)})
        elif x.get("type") == "tool_use" and role == "assistant":
            inp = x.get("input") or {}
            if isinstance(inp, dict) and "path" in inp and "file_path" not in inp:
                inp = dict(inp, file_path=inp["path"])
            out.append(_tool(x.get("name", "工具"), inp, 0, x.get("id")))
    return out


def parse_codex(d: dict) -> list[dict]:
    if d.get("type") != "response_item":
        return []
    p, ts = d.get("payload") or {}, _ts(d.get("timestamp"))
    pt = p.get("type")
    if pt == "message" and p.get("role") in ("user", "assistant"):
        texts = [x.get("text", "") for x in p.get("content") or [] if isinstance(x, dict)
                 and x.get("type") in ("input_text", "output_text", "text")]
        text = "\n".join(texts)
        if p["role"] == "user":
            bus = _bus_codex_note(text.strip(), ts)
            if bus:
                return bus
            text = _user_text(text)
            if not text:
                return []
        elif not text.strip():
            return []
        return [{"ts": ts, "role": p["role"], "text": _clip(text)}]
    if pt in ("function_call", "custom_tool_call", "local_shell_call"):
        return [_tool(p.get("name") or pt, p.get("arguments") or p.get("input") or p.get("action"), ts, p.get("call_id"))]
    if pt in ("function_call_output", "custom_tool_call_output") and p.get("call_id"):
        o = p.get("output")
        if isinstance(o, dict):
            o = o.get("content") or o.get("output") or json.dumps(o, ensure_ascii=False)
        elif isinstance(o, list):
            o = _result_text(o) or "\n".join(str(y.get("text", "")) for y in o if isinstance(y, dict))
        o = str(o or "")
        if o.startswith("{"):
            try:                                        # {"output": "...", "metadata": {...}}
                j = json.loads(o)
                o = str(j.get("output", o)) if isinstance(j, dict) else o
            except Exception:
                pass
        return [{"ts": ts, "role": "tool_out", "id": p["call_id"], "err": False, "out": _cut(o, TOOL_OUT)}]
    return []


# ---------- 读 ----------

def read(agent: str, sid: str, since: int = -1, hint: str | None = None) -> dict:
    f = (claude_file(sid, hint) if agent == "claude" else cursor_file(sid, hint) if agent == "cursor"
         else codex_file(sid))
    if not f:
        return {"entries": [], "offset": 0, "missing": True}
    parse = {"claude": parse_claude, "cursor": parse_cursor}.get(agent, parse_codex)
    return _read_file(f, parse, since)


def _read_file(f: Path, parse, since: int = -1, tail: int = TAIL) -> dict:
    size = f.stat().st_size
    reset = since < 0 or since > size
    start = max(0, size - tail) if reset else since
    with open(f, "rb") as fh:
        fh.seek(start)
        data = fh.read(size - start)
    if reset and start > 0:                     # 从文件中间起读：丢掉第一行残片
        nl = data.find(b"\n")
        data, start = data[nl + 1:], start + nl + 1
    end = data.rfind(b"\n") + 1
    entries, pos = [], start
    for line in data[:end].splitlines(keepends=True):
        try:
            d = json.loads(line)
        except Exception:
            pos += len(line)
            continue
        for e in parse(d):
            e["pos"] = pos
            entries.append(e)
        pos += len(line)
    if reset:
        # 状态更新不占名额（界面不画它们），留着好把卡片的状态对上
        keep = [e for e in entries if e["role"] not in ("subtask_upd", "tool_out")][-400:]
        lo = keep[0]["pos"] if keep else 0
        entries = [e for e in entries if e["role"] == "subtask_upd" or e["pos"] >= lo]
    return {"entries": entries, "offset": start + end, "reset": reset, "file": str(f)}


# ---------- 子代理（Claude Code 的 Task / Agent） ----------

_AID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_meta_cache: dict[str, tuple[float, dict]] = {}
_scan_cache: dict[str, dict] = {}


def subagents_dir(sid: str, hint: str | None = None) -> Path | None:
    f = claude_file(sid, hint)
    return f.with_suffix("") / "subagents" if f else None


def _metas(d: Path) -> dict:
    """toolUseId → {agent, type, description, parent}，按文件夹修改时间缓存。"""
    try:
        mt = d.stat().st_mtime
    except Exception:
        return {}
    c = _meta_cache.get(str(d))
    if c and c[0] == mt:
        return c[1]
    out = {}
    for m in d.glob("agent-*.meta.json"):
        try:
            j = json.loads(m.read_text(encoding="utf-8"))
        except Exception:
            continue
        aid = m.name[len("agent-"):-len(".meta.json")]
        if j.get("toolUseId"):
            out[j["toolUseId"]] = {"agent": aid, "type": j.get("agentType"), "description": j.get("description"),
                                   "parent": j.get("parentAgentId"), "model": j.get("model")}
    _meta_cache[str(d)] = (mt, out)
    return out


def _scan(f: Path) -> dict:
    """子代理记录的统计：开始 / 最后活动时间、工具调用次数、最后一个工具、交回的报告。按字节位置增量读。"""
    st = _scan_cache.get(str(f)) or {"off": 0, "first": 0, "last": 0, "tools": 0, "last_tool": None,
                                     "handback": None, "last_text": None}
    try:
        size = f.stat().st_size
    except Exception:
        return st
    if size < st["off"]:
        st = {"off": 0, "first": 0, "last": 0, "tools": 0, "last_tool": None, "handback": None, "last_text": None}
    if size > st["off"]:
        with open(f, "rb") as fh:
            fh.seek(st["off"])
            data = fh.read(size - st["off"])
        end = data.rfind(b"\n") + 1
        for line in data[:end].splitlines():
            try:
                d = json.loads(line)
            except Exception:
                continue
            ts = _ts(d.get("timestamp"))
            if ts:
                st["first"] = st["first"] or ts
                st["last"] = max(st["last"], ts)
            if d.get("type") != "assistant":
                continue
            for x in (d.get("message") or {}).get("content") or []:
                if not isinstance(x, dict):
                    continue
                if x.get("type") == "tool_use":
                    if x.get("name") == "SubagentHandback":
                        inp = x.get("input") if isinstance(x.get("input"), dict) else {}
                        st["handback"] = str(inp.get("message") or inp.get("report") or "")
                    else:
                        st["tools"] += 1
                        st["last_tool"] = _tool_line(x.get("name", "工具"), x.get("input"))
                elif x.get("type") == "text" and x.get("text", "").strip():
                    st["last_text"] = x["text"]
        st["off"] += end
        _scan_cache[str(f)] = st
    return st


def subtask_info(sid: str, ids: list[str], hint: str | None = None) -> dict:
    """ids 每项是 tool_use id，或者「tool_use id:子代理号」（主记录里的启动回执给过子代理号时）。
    返回 {tool_use id: {agent, type, description, started, last, tools, last_tool, done, result, file}}。"""
    d = subagents_dir(sid, hint)
    if not d or not d.is_dir():
        return {}
    metas, out = _metas(d), {}
    for raw in ids[:80]:
        tid, _, aid = raw.partition(":")
        m = metas.get(tid) or {}
        aid = m.get("agent") or aid
        if not aid or not _AID.match(aid):
            continue
        f = d / f"agent-{aid}.jsonl"
        if not f.exists():
            continue
        st = _scan(f)
        res = st["handback"] or st["last_text"]
        out[tid] = {"agent": aid, "type": m.get("type"), "description": m.get("description"), "parent": m.get("parent"),
                    "started": st["first"], "last": st["last"], "tools": st["tools"], "last_tool": st["last_tool"],
                    "done": st["handback"] is not None,
                    "result": (res if len(res) <= MAX_TEXT else res[:MAX_TEXT] + "\n…（后面省略）") if res else None}
    return out


def read_subtask(sid: str, aid: str, since: int = -1, hint: str | None = None) -> dict:
    """某个子代理自己的记录，条目格式和主记录一样（增量读、子代理里再派的子任务也是 subtask 条目）。"""
    if not _AID.match(aid or ""):
        return {"entries": [], "offset": 0, "missing": True}
    d = subagents_dir(sid, hint)
    f = d / f"agent-{aid}.jsonl" if d else None
    if not f or not f.exists():
        return {"entries": [], "offset": 0, "missing": True}
    return _read_file(f, lambda x: parse_claude(x, side=True), since, tail=TAIL // 3)


# ---------- 子任务总表（右上角「子任务」面板用） ----------
# 主记录只读尾部 3MB，早先派的子任务会看不到。这里把整份主记录过一遍，只挑和子任务有关的行（先按字节粗筛，
# 再解析），按字节位置增量往后读并缓存，所以只有第一次稍慢。

_LIST_KEYS = (b'"Task"', b'"Agent"', b"task-notification", b'"agentId"', b'"peer"', b'"is_error":true')
_list_cache: dict[str, dict] = {}
_FINAL = ("done", "failed", "stopped")


def _list_merge(st: dict, e: dict) -> None:
    subs, by_agent, pend = st["subs"], st["by_agent"], st["pend"]
    if e["role"] == "subtask":
        if e.get("id") and e["id"] not in subs:
            it = {k: v for k, v in e.items() if k != "role"}
            it.update(status=None, agent=None, result=None, duration=None, tools=None)
            subs[e["id"]] = it
        return
    sid = e.get("id") if e.get("id") in subs else None
    if not sid and e.get("agent"):
        sid = by_agent.get(e["agent"])
    if not sid:
        if e.get("agent") and not e.get("id"):
            pend[e["agent"]] = dict(pend.get(e["agent"]) or {}, **{k: v for k, v in e.items() if v not in (None, "")})
        return
    s = subs[sid]
    if e.get("agent") and not s.get("agent"):
        s["agent"] = e["agent"]
        by_agent[e["agent"]] = sid
        p = pend.pop(e["agent"], None)
        if p:
            _list_merge(st, dict(p, id=sid))
    if e.get("status") and not (s.get("status") in _FINAL and e["status"] == "running"):
        s["status"] = e["status"]
    for k in ("result", "duration", "tools"):
        if e.get(k) not in (None, ""):
            s[k] = e[k]


def _list_scan(f: Path) -> dict:
    key = str(f)
    try:
        size = f.stat().st_size
    except Exception:
        return {"subs": {}}
    st = _list_cache.get(key)
    if not st or size < st["off"]:
        st = {"off": 0, "subs": {}, "by_agent": {}, "pend": {}}
    if size > st["off"]:
        off = st["off"]
        with open(f, "rb") as fh:
            fh.seek(off)
            for line in fh:
                if not line.endswith(b"\n"):         # 还没写完的最后一行，下次再读
                    break
                pos, off = off, off + len(line)
                if not any(k in line for k in _LIST_KEYS):
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                for e in parse_claude(d):
                    if e["role"] == "subtask":
                        e["pos"] = pos
                        e["batch"] = (d.get("message") or {}).get("id") or pos   # 同一条回复里派的算一批（并行）
                        _list_merge(st, e)
                    elif e["role"] == "subtask_upd":
                        _list_merge(st, e)
        st["off"] = off
        _list_cache[key] = st
    return st


def subtask_list(sid: str, hint: str | None = None) -> dict:
    """整个会话的子任务，新的在前。每项：id、description、subagent_type、bg、prompt、ts、batch、
    status（done/failed/stopped/running）、agent、result、duration、tools、started、last、last_tool。"""
    f = claude_file(sid, hint)
    if not f:
        return {"items": [], "missing": True}
    subs = _list_scan(f)["subs"]
    items = [dict(v) for v in reversed(list(subs.values()))]
    info = subtask_info(sid, [i["id"] + (":" + i["agent"] if i.get("agent") else "") for i in items[:80]], hint)
    for i in items:
        inf = info.get(i["id"]) or {}
        i["agent"] = i.get("agent") or inf.get("agent")
        i["started"], i["last"], i["last_tool"] = inf.get("started") or i.get("ts"), inf.get("last"), inf.get("last_tool")
        if i.get("tools") is None:
            i["tools"] = inf.get("tools")
        if i.get("status") not in _FINAL:
            i["status"] = "done" if inf.get("done") else "running"
        if not i.get("result") and i["status"] == "done":
            i["result"] = inf.get("result")
        if not i.get("description"):
            i["description"] = inf.get("description") or ""
    return {"items": items, "total": len(items)}
