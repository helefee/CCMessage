@echo off
rem Build the desktop app into dist\AgentBridge\  (options: -Zip  -NoTest)
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\build_desktop.ps1" %*
if errorlevel 1 (
  echo.
  echo Build failed.
  if "%CI%"=="" pause
  exit /b 1
)
