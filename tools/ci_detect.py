"""CI (Windows runner): start look-alike processes for each install type and check that detect.py finds them on real Windows
(Toolhelp snapshot + QueryFullProcessImageName + NtQueryInformationProcess command lines; no PowerShell)."""
import os, shutil, subprocess, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_bridge import detect
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

node = shutil.which("node")
assert node, "node not on PATH"
T = Path(tempfile.mkdtemp(prefix="ccdet-"))
NW = 0x08000000
keep = "setTimeout(()=>{}, 120000)"
procs = []

def start(exe, *args):
    procs.append(subprocess.Popen([str(exe), *args], creationflags=NW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

def clone(rel):
    p = T / rel; p.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(node, p); return p

cli = T / "npm" / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"; cli.parent.mkdir(parents=True); cli.write_text(keep)
ext = T / ".vscode" / "extensions" / "anthropic.claude-code-9.9.9" / "cli.js"; ext.parent.mkdir(parents=True); ext.write_text(keep)
cx = T / "npm" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"; cx.parent.mkdir(parents=True); cx.write_text(keep)
start(node, str(cli))                                                   # npm: node.exe …\@anthropic-ai\claude-code\cli.js
start(node, str(ext))                                                   # VS Code extension (node)
start(node, str(cx))                                                    # Codex npm
start(clone(r"User\.local\bin\CLAUDE.EXE"), "-e", keep)                 # native installer (upper case name)
start(clone(r"AppData\Local\AnthropicClaude\app-0.0.1\Claude.exe"), "-e", keep)   # desktop (Squirrel)
start(clone(r"AppData\Roaming\Claude\claude-code\2.0.0\claude.exe"), "-e", keep)  # desktop-bundled claude-code
start(clone(r"bin\codex.exe"), "-e", keep)                              # Codex native
try:
    time.sleep(3)
    t0 = time.time(); r = detect.report(); ms = int((time.time() - t0) * 1000)
    kinds = {a: set(r[a]["kinds"]) for a in detect.AGENTS}
    print("took", ms, "ms; procs", r["procs"]); print("kinds", kinds)
    print(detect.text(r))
    need = {"claude": {"npm", "vscode", "cli", "desktop", "desktop-cli"}, "codex": {"npm", "cli"}}
    miss = {a: need[a] - kinds[a] for a in need if need[a] - kinds[a]}
    assert not miss, f"missed: {miss}"
    assert r["procs"]["total"] > 10 and r["claude"]["available"] and r["claude"]["state"] == "running"
    assert ms < 5000, "detection too slow"
    print("ci_detect OK")
finally:
    for p in procs:
        p.kill()
