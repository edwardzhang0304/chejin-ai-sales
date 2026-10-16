@echo off
setlocal
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0diagnose-update-access.ps1"
if errorlevel 1 echo Diagnostic tool failed. Please send a screenshot of this window.
pause
