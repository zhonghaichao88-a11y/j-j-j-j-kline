@echo off
setlocal
cd /d "%~dp0"
title Lead Radar
set "PS_BIN=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS_BIN%" set "PS_BIN=powershell.exe"
"%PS_BIN%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
if errorlevel 1 goto FAILED
exit /b 0

:FAILED
echo ------------------------------------------------------------
echo Lead Radar did not start. Read the red message above.
echo Full log: data\start.log
pause
exit /b 1
