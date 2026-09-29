@echo off
rem Start the agent-bridge web UI (keep this file ASCII-only: cmd reads it in the system code page)
cd /d "%~dp0"
python -m agent_bridge %*
if errorlevel 1 pause
