# -*- coding: utf-8 -*-
"""用量统计：自己扫 Claude Code / Claude Desktop / Codex 的会话日志，算 token、缓存和按 API 价折算的费用。

数据来源（都只读）：
- Claude：<CLAUDE_CONFIG_DIR>/projects/**/*.jsonl 里 assistant 行的 message.usage。同一次调用会被拆成好几行
  （每个内容块一行，usage 重复或逐步变大），会话分叉 / 续接还会把旧行复制进新文件。
  按 message.id + requestId 认作同一次调用，后面更完整的版本覆盖前面的，旧版本重放不会把数改小，日期用最早那次。
- Codex：<CODEX_HOME>/sessions/**/rollout-*.jsonl 和 archived_sessions/**。新版每次响应写一条
  token_usage_record（带 response_id，按它去重）；老版只有 event_msg/token_count（累计 total + 本次 last）：
  按「线程 + 段号 + 累计值」认作同一次调用，累计值变小（上下文压缩、运行时重启、子代理分页）就开新段，
  和 token_usage_record 已经算过的那次（累计值相同）不重复算。归档前后是同一个线程，键一样，不会算两遍。
- 读到哪儿记在「文件身份」上（Windows 上是卷号 + 文件号，挪进归档目录也认得出），只读新加的部分。
token 口径统一成：新输入（不含缓存）、缓存读、缓存写（5 分钟 / 1 小时）、输出（含思考）。
费用 = token × 价格（manual > models.dev > 内置表）× 供应商倍率；查不到价格就是 None（界面显示 —），不算 0。
去重规则参考 magpie（MIT），内置价格表来自 cc-switch（MIT），见 THIRD_PARTY_NOTICES.md。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import urllib.request
from pathlib import Path

from . import paths

DB_PATH = paths.HOME / "usage.sqlite"
TOK = ("inp", "cr", "cw5", "cw1h", "out")
_lock = threading.RLock()
STATE = {"scanning": False, "last": 0.0, "last_ms": 0, "files": 0, "added": 0, "error": "", "first_done": False}


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS calls(id TEXT PRIMARY KEY, app TEXT, src TEXT, ts REAL, sid TEXT, provider TEXT, sub INT,
        model TEXT, inp INT, cr INT, cw5 INT, cw1h INT, out INT, reasoning INT, tot INT, agent INT, fid TEXT);
    CREATE INDEX IF NOT EXISTS calls_ts ON calls(ts);
    CREATE TABLE IF NOT EXISTS files(fid TEXT PRIMARY KEY, path TEXT, kind TEXT, size INT, mtime REAL, off INT, state TEXT);
    CREATE TABLE IF NOT EXISTS prices(model TEXT PRIMARY KEY, inp REAL, out REAL, cr REAL, cw5 REAL, cw1h REAL, src TEXT, at REAL);
    CREATE TABLE IF NOT EXISTS mult(provider TEXT PRIMARY KEY, m REAL);
    CREATE TABLE IF NOT EXISTS hourly(h INT, app TEXT, provider TEXT, model TEXT, sub INT, n INT, inp INT, cr INT, cw5 INT, cw1h INT, out INT,
        PRIMARY KEY(h, app, provider, model, sub));
    CREATE TABLE IF NOT EXISTS daily(d TEXT, app TEXT, provider TEXT, model TEXT, sub INT, n INT, inp INT, cr INT, cw5 INT, cw1h INT, out INT,
        PRIMARY KEY(d, app, provider, model, sub));
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
    """)
    return c


# ---------------- 环境：供应商名、是不是订阅 ----------------

def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _claude_ctx() -> dict:
    s = _read_json(paths.CLAUDE_HOME / "settings.json")
    env = s.get("env") if isinstance(s.get("env"), dict) else {}
    base = env.get("ANTHROPIC_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL") or ""
    keyed = any(env.get(k) or os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")) or bool(s.get("apiKeyHelper"))
    cj = _read_json(paths.CLAUDE_HOME.parent / ".claude.json") if not os.environ.get("CLAUDE_CONFIG_DIR") else _read_json(paths.CLAUDE_HOME / ".claude.json")
    oauth = bool((cj.get("oauthAccount") or {}).get("billingType"))
    if base:
        host = re.sub(r"^https?://", "", base).split("/")[0]
        return {"provider": host, "sub": 0}
    if keyed:
        return {"provider": "Anthropic API", "sub": 0}
    return {"provider": "Claude 订阅" if oauth else "Anthropic", "sub": 1 if oauth else 0}


def _codex_ctx() -> dict:
    names = {}
    try:
        import tomllib
        cfg = tomllib.loads((paths.CODEX_HOME / "config.toml").read_text(encoding="utf-8"))
        for k, v in (cfg.get("model_providers") or {}).items():
            if isinstance(v, dict):
                names[k] = v.get("name") or k
    except Exception:
        pass
    mode = _read_json(paths.CODEX_HOME / "auth.json").get("auth_mode")
    return {"names": names, "chatgpt": mode == "chatgpt"}


def _codex_provider(pid: str | None, ctx: dict) -> tuple[str, int]:
    pid = pid or "openai"
    if pid == "openai":
        return ("ChatGPT 订阅", 1) if ctx["chatgpt"] else ("OpenAI API", 0)
    return (ctx["names"].get(pid) or pid, 0)


# ---------------- 文件身份 + 增量读 ----------------

def _fid(p: Path, st: os.stat_result) -> str:
    return f"{st.st_dev}-{st.st_ino}" if st.st_ino else "p:" + str(p).lower()


def _files() -> list[tuple[str, Path]]:
    out = []
    pj = paths.CLAUDE_HOME / "projects"
    if pj.is_dir():
        out += [("claude", p) for p in pj.rglob("*.jsonl")]
    for sub in ("sessions", "archived_sessions"):
        d = paths.CODEX_HOME / sub
        if d.is_dir():
            out += [("codex", p) for p in d.rglob("rollout-*.jsonl")]
    return out


def _n(v) -> int:
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def _ts(s) -> float:
    if not s:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


UPSERT = """INSERT INTO calls(id, app, src, ts, sid, provider, sub, model, inp, cr, cw5, cw1h, out, reasoning, tot, agent, fid)
VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(id) DO UPDATE SET inp=excluded.inp, cr=excluded.cr, cw5=excluded.cw5, cw1h=excluded.cw1h, out=excluded.out,
  reasoning=excluded.reasoning, tot=excluded.tot, model=CASE WHEN excluded.model<>'' THEN excluded.model ELSE calls.model END
WHERE excluded.tot > calls.tot"""


def _claude_lines(lines, st: dict, fid: str, ctx: dict, agent: int, hours: set, rows: list):
    rids = st.setdefault("rid", {})
    for ln in lines:
        if '"assistant"' not in ln or '"usage"' not in ln:
            continue
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if d.get("type") != "assistant":
            continue
        m = d.get("message") or {}
        u = m.get("usage") or {}
        mid, model = m.get("id"), m.get("model") or ""
        if not mid or not u or model == "<synthetic>":
            continue
        rid = d.get("requestId") or rids.get(mid) or ""
        if rid:
            rids[mid] = rid
            if len(rids) > 400:
                for k in list(rids)[:200]:
                    rids.pop(k, None)
        cc = _n(u.get("cache_creation_input_tokens"))
        c1h = min(cc, _n((u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens")))
        t = (_n(u.get("input_tokens")), _n(u.get("cache_read_input_tokens")), cc - c1h, c1h, _n(u.get("output_tokens")))
        if not sum(t):
            continue
        ts = _ts(d.get("timestamp"))
        app = "claude-desktop" if d.get("entrypoint") == "claude-desktop" else "claude-code"
        prov, sub = ctx["provider"], ctx["sub"]
        if model.startswith("ocx-"):
            prov, sub = "opencodex", 0
        th = ((u.get("output_tokens_details") or {}).get("thinking_tokens"))
        rows.append((f"cl:{mid}:{rid}", app, "log", ts, d.get("sessionId") or "", prov, sub, model, *t, _n(th), sum(t),
                     1 if (agent or d.get("isSidechain")) else 0, fid))
        hours.add(int(ts // 3600))


def _cx_tokens(u: dict) -> tuple:
    inp, ca = _n(u.get("input_tokens")), _n(u.get("cached_input_tokens"))
    return (max(0, inp - ca), ca, _n(u.get("cache_write_input_tokens")), 0, _n(u.get("output_tokens")))


def _codex_lines(lines, st: dict, fid: str, ctx: dict, hours: set, rows: list):
    for ln in lines:
        if not any(k in ln for k in ('"session_meta"', '"turn_context"', '"token_usage_record"', '"token_count"')):
            continue
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        typ, p = d.get("type"), d.get("payload") or {}
        if typ == "session_meta":
            st.setdefault("thread", p.get("id") or p.get("session_id"))
            st["prov"] = p.get("model_provider") or st.get("prov")
            src = p.get("source")
            st["agent"] = 1 if (isinstance(src, dict) or (p.get("thread_source") not in (None, "user"))) else 0
            continue
        if typ == "turn_context":
            st["model"] = p.get("model") or st.get("model")
            continue
        ts = _ts(d.get("timestamp"))
        prov, sub = _codex_provider(st.get("prov"), ctx)
        thread = st.get("thread") or fid
        if typ == "token_usage_record":
            u = p.get("usage") or p.get("turn_token_usage") or {}
            t = _cx_tokens(u)
            st["rec_tot"] = _n((p.get("thread_token_usage") or {}).get("total_tokens"))
            rid = p.get("response_id")
            if not sum(t):
                continue
            key = f"cx:{rid}" if rid else f"cx:{p.get('thread_id') or thread}:r{st['rec_tot']}"
            rows.append((key, "codex", "log", ts, p.get("thread_id") or thread, prov, sub, st.get("model") or "", *t,
                         _n(u.get("reasoning_output_tokens")), sum(t), st.get("agent", 0), fid))
            hours.add(int(ts // 3600))
            continue
        if typ == "event_msg" and p.get("type") == "token_count":
            info = p.get("info") or {}
            tot, last = info.get("total_token_usage") or {}, info.get("last_token_usage")
            T = _n(tot.get("total_tokens"))
            if not T:
                continue
            prev = st.get("tot")
            if prev is not None and T == prev:
                continue                                  # 同一份快照又写了一次
            if prev is not None and T < prev:
                st["epoch"] = st.get("epoch", 0) + 1      # 压缩 / 重启 / 子代理分页：累计值重新开始
            st["tot"] = T
            if T == st.get("rec_tot"):
                continue                                  # 这次 token_usage_record 已经算过
            if last is None:
                pt = st.get("prev_u") or {}
                last = {k: _n(tot.get(k)) - _n(pt.get(k)) for k in tot}
            st["prev_u"] = {k: _n(v) for k, v in tot.items()}
            t = _cx_tokens(last)
            if not sum(t):
                continue
            rows.append((f"cx:{thread}:{st.get('epoch', 0)}:{T}", "codex", "log", ts, thread, prov, sub, st.get("model") or "", *t,
                         _n(last.get("reasoning_output_tokens")), sum(t), st.get("agent", 0), fid))
            hours.add(int(ts // 3600))


def scan(full: bool = False) -> dict:
    """扫一遍（增量）。返回这次读了多少文件、加了多少条。"""
    with _lock:
        STATE["scanning"] = True
        t0 = time.time()
        try:
            c = _conn()
            known = {r[0]: r for r in c.execute("SELECT fid, path, kind, size, mtime, off, state FROM files")}
            cctx, xctx = _claude_ctx(), _codex_ctx()
            nfiles = added = 0
            hours: set = set()
            before = c.execute("SELECT count(*) FROM calls").fetchone()[0]
            for kind, p in _files():
                try:
                    stt = p.stat()
                except OSError:
                    continue
                fid = _fid(p, stt)
                row = known.get(fid)
                off, state = 0, {}
                if row and not full:
                    if row[3] == stt.st_size and row[4] == stt.st_mtime and row[1] == str(p):
                        continue
                    off = row[5] if stt.st_size >= row[5] else 0      # 文件变短 = 被重写：从头读，键会去重
                    state = json.loads(row[6] or "{}") if off else {}
                with open(p, "rb") as f:
                    f.seek(off)
                    data = f.read()
                end = data.rfind(b"\n") + 1                            # 只读到最后一个完整行
                lines = data[:end].decode("utf-8", "replace").splitlines()
                rows: list = []
                if kind == "claude":
                    _claude_lines(lines, state, fid, cctx, 1 if "subagents" in p.parts else 0, hours, rows)
                else:
                    _codex_lines(lines, state, fid, xctx, hours, rows)
                if rows:
                    c.executemany(UPSERT, rows)
                    if kind == "claude":
                        c.executemany("DELETE FROM calls WHERE id=?", [("cl:" + r[0].split(":")[1] + ":ccs",) for r in rows])
                c.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?)",
                          (fid, str(p), kind, stt.st_size, stt.st_mtime, off + end, json.dumps(state, separators=(",", ":"))))
                nfiles += 1
            c.commit()
            added = c.execute("SELECT count(*) FROM calls").fetchone()[0] - before
            if hours or full:
                _rollup(c, None if full else hours)
            c.close()
            STATE.update(last=time.time(), last_ms=int((time.time() - t0) * 1000), files=nfiles, added=added, error="", first_done=True)
            return {"files": nfiles, "added": added, "ms": STATE["last_ms"]}
        except Exception as e:
            STATE["error"] = f"{type(e).__name__}: {e}"
            raise
        finally:
            STATE["scanning"] = False


def _rollup(c: sqlite3.Connection, hours: set | None):
    """重算受影响小时的 hourly，再重算受影响日期的 daily（按本机时区）。"""
    cols = "count(*), sum(inp), sum(cr), sum(cw5), sum(cw1h), sum(out)"
    if hours is None:
        c.execute("DELETE FROM hourly")
        c.execute(f"INSERT INTO hourly SELECT CAST(ts/3600 AS INT), app, provider, model, sub, {cols} FROM calls GROUP BY 1,2,3,4,5")
        days = None
    else:
        hs = sorted(hours)
        for i in range(0, len(hs), 400):
            part = hs[i:i + 400]
            q = ",".join("?" * len(part))
            c.execute(f"DELETE FROM hourly WHERE h IN ({q})", part)
            c.execute(f"INSERT INTO hourly SELECT CAST(ts/3600 AS INT) h, app, provider, model, sub, {cols} FROM calls "
                      f"WHERE CAST(ts/3600 AS INT) IN ({q}) GROUP BY 1,2,3,4,5", part)
        days = {time.strftime("%Y-%m-%d", time.localtime(h * 3600)) for h in hs}
    agg = {}
    for h, app, prov, model, sub, *t in c.execute("SELECT * FROM hourly"):
        d = time.strftime("%Y-%m-%d", time.localtime(h * 3600))
        if days is not None and d not in days:
            continue
        k = (d, app, prov, model, sub)
        a = agg.setdefault(k, [0] * 6)
        for i, v in enumerate(t):
            a[i] += v or 0
    if days is None:
        c.execute("DELETE FROM daily")
    else:
        for d in days:
            c.execute("DELETE FROM daily WHERE d=?", (d,))
    c.executemany("INSERT INTO daily VALUES(?,?,?,?,?,?,?,?,?,?,?)", [(*k, *v) for k, v in agg.items()])
    c.commit()


# ---------------- 价格 ----------------
_BUILTIN: dict | None = None
EFFORT_SUFFIX = re.compile(r"-(minimal|low|medium|high|xhigh|max|ultra|thinking)$")


def builtin() -> dict:
    global _BUILTIN
    if _BUILTIN is None:
        try:
            _BUILTIN = json.loads((paths.PKG / "usage_prices.json").read_text(encoding="utf-8"))["models"]
        except Exception:
            _BUILTIN = {}
    return _BUILTIN


def _row(v: list, model: str) -> dict:
    i, o, cr, cw = (list(v) + [0, 0, 0, 0])[:4]
    claude = model.startswith("claude")
    return {"inp": i, "out": o, "cr": cr if cr else (i * 0.1 if claude else 0), "cw5": cw if cw else (i * 1.25 if claude else 0),
            "cw1h": i * 2 if claude else (cw or 0)}


def price_table(c: sqlite3.Connection | None = None) -> dict:
    """model → {inp,out,cr,cw5,cw1h,src}；manual > models.dev > 内置。"""
    own = c is None
    c = c or _conn()
    t = {m: dict(_row(v, m), src="builtin") for m, v in builtin().items()}
    rows = c.execute("SELECT model, inp, out, cr, cw5, cw1h, src FROM prices ORDER BY CASE src WHEN 'modelsdev' THEN 0 ELSE 1 END").fetchall()
    for m, i, o, cr, cw5, cw1h, src in rows:
        t[m] = {"inp": i or 0, "out": o or 0, "cr": cr or 0, "cw5": cw5 or 0, "cw1h": cw1h if cw1h is not None else (cw5 or 0), "src": src}
    if own:
        c.close()
    return t


def norm(model: str) -> str:
    m = (model or "").lower().strip()
    m = m.replace("[1m]", "").split("/")[-1]
    return m


def find_price(model: str, table: dict) -> tuple[str, dict] | tuple[None, None]:
    memo = table.setdefault("\0memo", {})
    if model not in memo:
        memo[model] = _find_price(model, table)
    return memo[model]


def _find_price(model: str, table: dict) -> tuple[str, dict] | tuple[None, None]:
    m = norm(model)
    cands = [m]
    x = re.sub(r"-\d{8}$", "", m)
    cands.append(x)
    y = EFFORT_SUFFIX.sub("", x)
    cands.append(y)
    cands.append(re.sub(r"(\d)\.(\d)", r"\1-\2", y) if y.startswith("claude") else y)
    for k in cands:
        if k in table:
            return k, table[k]
    for k in cands:                                   # 表里是带日期的全名
        for t in table:
            if t != "\0memo" and t.startswith(k + "-") and re.fullmatch(r"\d{8}", t[len(k) + 1:]):
                return t, table[t]
    return None, None


def cost(tokens: dict, p: dict | None, mult: float = 1.0) -> float | None:
    if not p:
        return None
    return mult * (tokens["inp"] * p["inp"] + tokens["out"] * p["out"] + tokens["cr"] * p["cr"]
                   + tokens["cw5"] * p["cw5"] + tokens["cw1h"] * p["cw1h"]) / 1e6


def set_price(model: str, v: dict | None):
    c = _conn()
    if v is None:
        c.execute("DELETE FROM prices WHERE model=? AND src='manual'", (model,))
    else:
        f = {k: float(v.get(k) or 0) for k in ("inp", "out", "cr", "cw5")}
        cw1h = float(v["cw1h"]) if v.get("cw1h") not in (None, "") else (f["inp"] * 2 if model.startswith("claude") else f["cw5"])
        c.execute("INSERT OR REPLACE INTO prices VALUES(?,?,?,?,?,?,?,?)", (norm(model), f["inp"], f["out"], f["cr"], f["cw5"], cw1h, "manual", time.time()))
    c.commit()
    c.close()


def set_mult(provider: str, m: float | None):
    c = _conn()
    if m is None or m == 1:
        c.execute("DELETE FROM mult WHERE provider=?", (provider,))
    else:
        c.execute("INSERT OR REPLACE INTO mult VALUES(?,?)", (provider, float(m)))
    c.commit()
    c.close()


MODELS_DEV = "https://models.dev/api.json"


def refresh_models_dev(timeout: float = 20) -> dict:
    req = urllib.request.Request(MODELS_DEV, headers={"User-Agent": "CCMsg"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    pref = ["anthropic", "openai", "google", "xai", "deepseek", "moonshotai", "zhipuai", "alibaba", "mistral"]
    found: dict = {}
    for prov in pref + [k for k in data if k not in pref]:
        for mid, m in ((data.get(prov) or {}).get("models") or {}).items():
            cst = (m or {}).get("cost") or {}
            if not cst.get("input") and not cst.get("output"):
                continue
            k = norm(mid)
            if k in found:
                continue
            i = float(cst.get("input") or 0)
            cw = float(cst.get("cache_write") or 0)
            found[k] = (k, i, float(cst.get("output") or 0), float(cst.get("cache_read") or 0), cw,
                        i * 2 if k.startswith("claude") else cw, "modelsdev", time.time())
    c = _conn()
    manual = {r[0] for r in c.execute("SELECT model FROM prices WHERE src='manual'")}
    c.execute("DELETE FROM prices WHERE src='modelsdev'")
    c.executemany("INSERT OR REPLACE INTO prices VALUES(?,?,?,?,?,?,?,?)", [v for k, v in found.items() if k not in manual])
    c.execute("INSERT OR REPLACE INTO meta VALUES('modelsdev_at', ?)", (str(time.time()),))
    c.commit()
    c.close()
    return {"models": len(found)}


# ---------------- 查询 ----------------

def _where(f: dict, tcol: str = "ts") -> tuple[str, list]:
    w, a = [], []
    if f.get("app"):
        apps = {"claude": ("claude-code", "claude-desktop")}.get(f["app"], (f["app"],))
        w.append(f"app IN ({','.join('?' * len(apps))})")
        a += list(apps)
    for k in ("provider", "model"):
        if f.get(k):
            w.append(f"{k}=?")
            a.append(f[k])
    if f.get("from"):
        w.append(f"{tcol}>=?")
        a.append(float(f["from"]) / (3600 if tcol == "h" else 1))
    if f.get("to"):
        w.append(f"{tcol}<?")
        a.append(float(f["to"]) / (3600 if tcol == "h" else 1))
    return (" WHERE " + " AND ".join(w)) if w else "", a


def _mults(c) -> dict:
    return dict(c.execute("SELECT provider, m FROM mult").fetchall())


def summary(f: dict) -> dict:
    c = _conn()
    pt, mults = price_table(c), _mults(c)
    w, a = _where(f, "h")
    rows = c.execute(f"SELECT h, app, provider, model, sub, n, inp, cr, cw5, cw1h, out FROM hourly{w}", a).fetchall()
    tot = {k: 0 for k in ("n", *TOK)}
    cost_sum, unpriced_n, sub_n, missing = 0.0, 0, 0, {}
    by_h: dict = {}
    by_model: dict = {}
    by_prov: dict = {}
    for h, app, prov, model, sub, n, *t in rows:
        tk = dict(zip(TOK, t))
        k, p = find_price(model, pt)
        cs = cost(tk, p, mults.get(prov, 1.0))
        for d, key in ((by_model, model), (by_prov, prov)):
            e = d.setdefault(key, {"n": 0, **{x: 0 for x in TOK}, "cost": 0.0, "unpriced": 0, "sub": 0, "app": set()})
            e["n"] += n
            for x in TOK:
                e[x] += tk[x]
            e["app"].add(app)
            if cs is None:
                e["unpriced"] += n
            else:
                e["cost"] += cs
            e["sub"] += n if sub else 0
        b = by_h.setdefault(h, {"n": 0, "tok": 0, "cost": 0.0})
        b["n"] += n
        b["tok"] += sum(t)
        tot["n"] += n
        for x in TOK:
            tot[x] += tk[x]
        sub_n += n if sub else 0
        if cs is None:
            unpriced_n += n
            missing.setdefault(model, 0)
            missing[model] += n
        else:
            cost_sum += cs
            b["cost"] += cs
    c.close()
    inp_all = tot["inp"] + tot["cr"] + tot["cw5"] + tot["cw1h"]
    fin = lambda d: [{"key": k, **{x: v[x] for x in ("n", *TOK, "unpriced", "sub")}, "cost": round(v["cost"], 6),
                      "apps": sorted(v["app"])} for k, v in sorted(d.items(), key=lambda kv: -kv[1]["n"])]
    return {"totals": {**tot, "real": inp_all + tot["out"],
                       "hit": (tot["cr"] / inp_all) if inp_all else None, "cost": round(cost_sum, 6),
                       "unpriced": unpriced_n, "sub": sub_n},
            "series": [{"h": h * 3600, **v} for h, v in sorted(by_h.items())],
            "models": fin(by_model), "providers": fin(by_prov),
            "missing": [{"model": m, "n": n} for m, n in sorted(missing.items(), key=lambda kv: -kv[1])],
            "options": options()}


def options() -> dict:
    c = _conn()
    r = {k: [x[0] for x in c.execute(f"SELECT DISTINCT {k} FROM hourly WHERE {k}<>'' ORDER BY 1")] for k in ("app", "provider", "model")}
    span = c.execute("SELECT min(ts), max(ts) FROM calls").fetchone()
    c.close()
    r["span"] = list(span)
    return r


def logs(f: dict, offset: int = 0, limit: int = 100) -> dict:
    c = _conn()
    pt, mults = price_table(c), _mults(c)
    w, a = _where(f)
    total = c.execute(f"SELECT count(*) FROM calls{w}", a).fetchone()[0]
    out = []
    for r in c.execute(f"SELECT id, app, src, ts, sid, provider, sub, model, inp, cr, cw5, cw1h, out, reasoning, agent FROM calls{w} "
                       f"ORDER BY ts DESC LIMIT ? OFFSET ?", a + [int(limit), int(offset)]):
        tk = dict(zip(TOK, r[8:13]))
        _, p = find_price(r[7], pt)
        out.append({"id": r[0], "app": r[1], "src": r[2], "ts": r[3], "sid": r[4], "provider": r[5], "sub": r[6], "model": r[7], **tk,
                    "reasoning": r[13], "agent": r[14], "cost": cost(tk, p, mults.get(r[5], 1.0))})
    c.close()
    return {"total": total, "rows": out}


def prices_view() -> dict:
    c = _conn()
    pt = price_table(c)
    used = [r[0] for r in c.execute("SELECT model FROM hourly GROUP BY model ORDER BY sum(n) DESC")]
    mults = _mults(c)
    provs = [r[0] for r in c.execute("SELECT DISTINCT provider FROM hourly")]
    at = c.execute("SELECT v FROM meta WHERE k='modelsdev_at'").fetchone()
    c.close()
    rows = []
    for m in used:
        k, p = find_price(m, pt)
        rows.append({"model": m, "match": k, **(p or {}), "priced": bool(p)})
    return {"used": rows, "count": sum(1 for k in pt if k != "\0memo"), "mult": [{"provider": p, "m": mults.get(p, 1.0)} for p in provs],
            "modelsdev_at": float(at[0]) if at else None}


# ---------------- 外部来源 ----------------

def _ping(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=0.8) as r:
            return r.status < 500
    except Exception as e:
        return getattr(e, "code", 0) in (401, 403, 404)


def _port_running(port):
    from .modelcfg import port_running        # 共用后台测端口 + 30 秒缓存（Windows 上同步测一个关着的端口要 ~1 秒）
    return port_running(port)


def proxies() -> list[dict]:
    """检测 opencodex / magpie 本地代理（只看端口通不通；不读它们的令牌文件）。"""
    ocx_port = (_read_json(Path.home() / ".opencodex" / "config.json").get("port")) or 10100
    out = []
    for name, port, cfg in (("opencodex", int(ocx_port), Path.home() / ".opencodex"), ("magpie", 3425, None)):
        out.append({"name": name, "port": port, "configured": bool(cfg and cfg.exists()), "running": _port_running(port)})
    return out


def ccswitch_db() -> Path:
    return Path.home() / ".cc-switch" / "cc-switch.db"


def import_ccswitch(retries: int = 6) -> dict:
    """一次性导入 CC Switch 的历史（只读打开；被它锁着就等一会儿再试）。
    CC Switch 的 Claude 行按 message.id 记（session:msg_…），和我们扫到的同一条就跳过；Codex 按线程跳过我们已经扫到的线程；
    它的代理（proxy）行和会话日志是同一批调用的两份记录，不导入。它记的费用不用，按我们的价格表重算。
    这样能补回 Claude 已经清理掉的旧会话文件里的用量。"""
    src = ccswitch_db()
    if not src.exists():
        raise FileNotFoundError("没找到 CC Switch 的数据库")
    c = _conn()
    mids = {r[0].split(":")[1] for r in c.execute("SELECT id FROM calls WHERE src='log' AND id LIKE 'cl:%'")}
    threads = {r[0] for r in c.execute("SELECT DISTINCT sid FROM calls WHERE src='log' AND app='codex'")}
    last_err = None
    for i in range(retries):
        try:
            s = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True, timeout=5)
            cols = [r[1] for r in s.execute("PRAGMA table_info(proxy_request_logs)")]
            sem = "input_token_semantics" if "input_token_semantics" in cols else "0"
            ds = "data_source" if "data_source" in cols else "'proxy'"
            prov_names = {}
            try:
                prov_names = {(r[0], r[1]): r[2] for r in s.execute("SELECT id, app_type, name FROM providers")}
            except sqlite3.Error:
                pass
            q = (f"SELECT request_id, app_type, provider_id, model, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, "
                 f"{sem}, created_at, session_id, {ds} FROM proxy_request_logs WHERE status_code BETWEEN 200 AND 299")
            rows, hours, skipped = [], set(), 0
            cctx = _claude_ctx()
            for rid, app, pid, model, inp, out, cr, cc, semv, ts, sid, dsrc in s.execute(q):
                rid = str(rid or "")
                if dsrc == "proxy":
                    skipped += 1
                    continue
                if app == "claude":
                    mid = rid.split(":", 1)[1] if rid.startswith("session:") else rid
                    if mid in mids:
                        skipped += 1
                        continue
                    key = f"cl:{mid}:ccs"
                elif app == "codex":
                    parts = rid.split(":")
                    th = parts[2] if len(parts) >= 4 else (sid or "")
                    if th in threads:
                        skipped += 1
                        continue
                    key, sid = f"ccs:{rid}", th or sid
                else:
                    key = f"ccs:{rid}"
                ts = float(ts or 0)
                if ts > 1e12:
                    ts /= 1000
                inp, cr, cc, out = _n(inp), _n(cr), _n(cc), _n(out)
                if app in ("codex", "gemini", "grokbuild"):          # 口径同 cc-switch sql_helpers::fresh_input_sql
                    if semv == 1 and inp >= cr + cc:
                        inp -= cr + cc
                    elif semv == 0 and inp >= cr:
                        inp -= cr
                t = (inp, cr, cc, 0, out)
                if not sum(t):
                    continue
                if app == "claude":
                    pname, sub = cctx["provider"], cctx["sub"]
                    if pid not in ("default", "claude-official", "", None):
                        pname, sub = prov_names.get((pid, app)) or pid, 0
                else:
                    pname, sub = prov_names.get((pid, app)) or pid or "", 0
                rows.append((key, "claude-code" if app == "claude" else app, "ccswitch", ts, sid or "",
                             pname, sub, model or "", *t, 0, sum(t), 0, "ccswitch"))
                hours.add(int(ts // 3600))
            s.close()
            c.executemany(UPSERT, rows)
            c.execute("INSERT OR REPLACE INTO meta VALUES('ccswitch_import', ?)",
                      (json.dumps({"at": time.time(), "rows": len(rows), "skipped": skipped}),))
            c.commit()
            _rollup(c, hours)
            c.close()
            return {"imported": len(rows), "skipped": skipped}
        except sqlite3.OperationalError as e:
            last_err = e
            time.sleep(1.5 * (i + 1))
    c.close()
    raise RuntimeError(f"CC Switch 数据库一直被占用：{last_err}")


def status() -> dict:
    c = _conn()
    n = c.execute("SELECT src, app, count(*), min(ts), max(ts) FROM calls GROUP BY 1,2").fetchall()
    nf = c.execute("SELECT kind, count(*) FROM files GROUP BY 1").fetchall()
    imp = c.execute("SELECT v FROM meta WHERE k='ccswitch_import'").fetchone()
    c.close()
    return {**STATE, "db": str(DB_PATH), "sources": [{"src": s, "app": a, "n": k, "from": f, "to": t} for s, a, k, f, t in n],
            "files_known": dict(nf), "claude_dir": str(paths.CLAUDE_HOME / "projects"), "codex_dir": str(paths.CODEX_HOME),
            "proxies": proxies(), "ccswitch": {"exists": ccswitch_db().exists(), "imported": json.loads(imp[0]) if imp else None}}


_started = False


def start_background(interval: float = 60):
    """第一次全量扫描放后台，之后每分钟增量。"""
    global _started
    if _started or os.environ.get("AGENT_BRIDGE_NO_USAGE"):
        return
    _started = True

    def loop():
        time.sleep(3)
        while True:
            try:
                scan()
            except Exception:
                pass
            time.sleep(interval)
    threading.Thread(target=loop, name="usage-scan", daemon=True).start()
