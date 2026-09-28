@echo off
rem 双击启动 agent-bridge 管理界面
cd /d "%~dp0"
python -m agent_bridge %*
if errorlevel 1 pause
