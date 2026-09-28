# -*- coding: utf-8 -*-
"""读 Claude / Codex 会话记录，整理成界面能直接画的条目。

条目：{"pos": 字节位置, "ts": 秒, "role": "user"|"assistant"|"tool", "text": …}
  - user：人在会话里说的话（系统注入的 AGENTS.md / system-reminder / 环境上下文 都去掉）
  - assistant：模型的回复正文（思考过程不要）
  - tool：一次工具调用的一行摘要（工具名 + 命令 / 说明），结果不要
首次读文件尾部 TAIL 字节；之后按字节位置增量读，只消费完整的行。
"""
from __future__ import annotations

import json
import os
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


def codex_file(sid: str) -> Path | None:
    for base in (CODEX_HOME / "sessions", CODEX_HOME / "archived_sessions"):
        hits = list(base.rglob(f"rollout-*{sid}.jsonl"))
        if hits:
            return max(hits, key=lambda p: p.stat().st_mtime)
    return None


# ---------- 解析一行 ----------

def _tool_line(name: str, inp) -> str:
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except Exception:
            inp = {"arg": inp}
    inp = inp if isinstance(inp, dict) else {}
    brief = (inp.get("description") or inp.get("cmd") or inp.get("command") or inp.get("file_path")
             or inp.get("pattern") or inp.get("prompt") or inp.get("url") or inp.get("query") or "")
    if isinstance(brief, list):
        brief = " ".join(map(str, brief))
    brief = " ".join(str(brief).split())
    return f"{name} · {brief[:160]}" if brief else name


def parse_claude(d: dict) -> list[dict]:
    if d.get("isSidechain") or d.get("isMeta"):
        return []
    t, msg, ts = d.get("type"), d.get("message") or {}, _ts(d.get("timestamp"))
    out = []
    if t == "user":
        c = msg.get("content")
        if isinstance(c, str):
            u = _user_text(c)
            if u:
                out.append({"ts": ts, "role": "user", "text": _clip(u)})
        elif isinstance(c, list):
            texts = [x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text"]
            u = _user_text("\n".join(texts))
            if u:
                out.append({"ts": ts, "role": "user", "text": _clip(u)})
    elif t == "assistant":
        for x in msg.get("content") or []:
            if not isinstance(x, dict):
                continue
            if x.get("type") == "text" and x.get("text", "").strip():
                out.append({"ts": ts, "role": "assistant", "text": _clip(x["text"])})
            elif x.get("type") == "tool_use":
                out.append({"ts": ts, "role": "tool", "text": _tool_line(x.get("name", "工具"), x.get("input"))})
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
            text = _user_text(text)
            if not text:
                return []
        elif not text.strip():
            return []
        return [{"ts": ts, "role": p["role"], "text": _clip(text)}]
    if pt in ("function_call", "custom_tool_call", "local_shell_call"):
        return [{"ts": ts, "role": "tool", "text": _tool_line(p.get("name") or pt, p.get("arguments") or p.get("input") or p.get("action"))}]
    return []


# ---------- 读 ----------

def read(agent: str, sid: str, since: int = -1, hint: str | None = None) -> dict:
    f = claude_file(sid, hint) if agent == "claude" else codex_file(sid)
    if not f:
        return {"entries": [], "offset": 0, "missing": True}
    size = f.stat().st_size
    reset = since < 0 or since > size
    start = max(0, size - TAIL) if reset else since
    with open(f, "rb") as fh:
        fh.seek(start)
        data = fh.read(size - start)
    if reset and start > 0:                     # 从文件中间起读：丢掉第一行残片
        nl = data.find(b"\n")
        data, start = data[nl + 1:], start + nl + 1
    end = data.rfind(b"\n") + 1
    parse = parse_claude if agent == "claude" else parse_codex
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
        entries = entries[-400:]
    return {"entries": entries, "offset": start + end, "reset": reset, "file": str(f)}
