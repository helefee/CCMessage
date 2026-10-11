# Entry point for the Nuitka build of CCMsg.exe (CC Msg, Windows installer version).
#   CCMsg.exe                -> desktop window (same as: python -m agent_bridge desktop)
#   CCMsg.exe --port 8799    -> desktop window on another port
#   CCMsg.exe <subcommand>   -> same subcommands as python -m agent_bridge (doctor, selftest, install, fx, ...)
import sys

from agent_bridge.__main__ import main

if len(sys.argv) == 1 or sys.argv[1].startswith("-"):
    sys.argv.insert(1, "desktop")
sys.exit(main() or 0)
