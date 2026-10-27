@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
title Lead Radar - Stop
echo Stopping Lead Radar ...
set "PS_BIN=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS_BIN%" set "PS_BIN=powershell.exe"
"%PS_BIN%" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "$root=(Resolve-Path -LiteralPath '.').Path.TrimEnd('\'); $prefix=Join-Path $root '.venv\Scripts\'; $n=0; Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'run[.]py' -and $_.ExecutablePath -and $_.ExecutablePath.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $n++ }; Write-Host ('Stopped processes: '+$n)"
if errorlevel 1 goto STOP_ERROR
echo [OK] Lead Radar stopped. Your data was not deleted.
pause
exit /b 0

:STOP_ERROR
echo [WARNING] Could not confirm the stop. Close the "Lead Radar Server" window by hand.
pause
exit /b 1
