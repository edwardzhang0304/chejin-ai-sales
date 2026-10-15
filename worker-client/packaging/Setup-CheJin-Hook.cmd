@echo off
setlocal EnableExtensions
set "SETUP_DIR=%~dp0"
if not exist "%SETUP_DIR%start-hook.ps1" (
  echo Setup files are incomplete. Contact operations.
  pause
  exit /b 3
)
if not exist "%SETUP_DIR%profile.json" (
  echo This PC has no deployment profile. Contact operations for profile.json.
  pause
  exit /b 2
)
echo Before continuing, turn OFF WeChat voice auto-transcription.
set "SETUP_CONFIRM="
set /p "SETUP_CONFIRM=Confirmed off? Type Y to continue: "
if /I not "%SETUP_CONFIRM%"=="Y" exit /b 2
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SETUP_DIR%start-hook.ps1" -Configuration "%SETUP_DIR%profile.json" -ConfirmAutoTextDisabled -CreateShortcuts
set "SETUP_RESULT=%ERRORLEVEL%"
if not "%SETUP_RESULT%"=="0" (
  echo Setup failed. No work has started. Contact operations with the error shown above.
  pause
  exit /b %SETUP_RESULT%
)
echo Setup completed. Use the two new desktop shortcuts.
pause
exit /b 0
