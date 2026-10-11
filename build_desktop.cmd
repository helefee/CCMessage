@echo off
rem Build the Windows installer version into dist\  (options: -Zip  -NoTest  -NoInstaller)
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\build_desktop.ps1" %*
if errorlevel 1 (
  echo.
  echo Build failed.
  if "%CI%"=="" pause
  exit /b 1
)
