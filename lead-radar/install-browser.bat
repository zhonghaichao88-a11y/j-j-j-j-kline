@echo off
setlocal
cd /d "%~dp0"
title Lead Radar - Repair browser
rem Normally not needed: start.bat installs everything. This forces the browser component to reinstall.
set "PS_BIN=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS_BIN%" set "PS_BIN=powershell.exe"
"%PS_BIN%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" -RepairBrowser
if errorlevel 1 pause
