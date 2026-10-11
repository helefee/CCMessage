# Entry point for the Nuitka build of AgentBridge.exe (Windows installer version).
#   AgentBridge.exe                -> desktop window (same as: python -m agent_bridge desktop)
#   AgentBridge.exe --port 8799    -> desktop window on another port
#   AgentBridge.exe <subcommand>   -> same subcommands as python -m agent_bridge (doctor, selftest, install, fx, ...)
import sys

from agent_bridge.__main__ import main

if len(sys.argv) == 1 or sys.argv[1].startswith("-"):
    sys.argv.insert(1, "desktop")
sys.exit(main() or 0)
