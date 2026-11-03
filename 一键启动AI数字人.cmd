@echo off
setlocal

set "START_SCRIPT=%~dp0tools\start_ai_live.ps1"

if not exist "%START_SCRIPT%" (
  echo Startup script not found:
  echo %START_SCRIPT%
  pause
  exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%START_SCRIPT%"
if errorlevel 1 (
  echo.
  echo AI digital human startup failed.
  echo Check: %~dp0server.live.stderr.log
  pause
  exit /b 1
)

endlocal
exit /b 0
