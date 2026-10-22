@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0update-hook-component.ps1" -Mode Rollback
exit /b %errorlevel%
