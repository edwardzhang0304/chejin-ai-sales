@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-hook.ps1" -CreateShortcuts -ShowErrorDialog
set "REFRESH_RESULT=%ERRORLEVEL%"
if "%REFRESH_RESULT%"=="0" echo Desktop shortcuts refreshed. No work has started.
pause
exit /b %REFRESH_RESULT%
