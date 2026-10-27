@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Lead Radar Server - close this window to stop the program
if not exist ".venv\Scripts\python.exe" goto FAILED
echo Lead Radar is running. Keep this window open (minimized is fine).
echo Close this window to stop the program.
echo ------------------------------------------------------------
".venv\Scripts\python.exe" "run.py"
echo ------------------------------------------------------------
echo [INFO] Lead Radar server stopped.
pause
exit /b 0
:FAILED
echo [ERROR] The Python environment cannot be found. Double-click start.bat first.
pause
exit /b 1
