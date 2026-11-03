@echo off
setlocal

rem ============================================================================
rem  AI digital human - REAL DESKTOP APP launcher.
rem  Starts AILiveDesktop.exe: an independent Windows program with its own
rem  native window, which starts/stops the LiveTalking backend itself.
rem
rem  Lookup order:  1) installed copy in %LOCALAPPDATA%\Programs\AILiveDesktop
rem                 2) dev build in <project>\app\dist\AILiveDesktop
rem
rem  KEEP THIS FILE PURE ASCII AND CRLF. Two real incidents behind this rule:
rem   * UTF-8 (multi-byte) chars + bare LF + "chcp 65001" makes cmd re-seek the
rem     batch file by byte offset, desync the parser and run fragments of the
rem     following lines ('nul' is not recognized as an internal command...).
rem   * Chinese bytes in a batch file are decoded with the console codepage, so
rem     on a GBK console a path like tools\<chinese>.ps1 does not resolve.
rem  That is also why this launcher needs no PowerShell at all.
rem ============================================================================

tasklist /FI "IMAGENAME eq AILiveDesktop.exe" 2>nul | find /I "AILiveDesktop.exe" >nul
if not errorlevel 1 (
  echo AI digital human desktop app is already running.
  echo Switch to it from the taskbar.
  exit /b 0
)

set "EXE1=%LOCALAPPDATA%\Programs\AILiveDesktop\AILiveDesktop.exe"
set "EXE2=%~dp0app\dist\AILiveDesktop\AILiveDesktop.exe"

if exist "%EXE1%" (
  start "" "%EXE1%"
  echo Started installed desktop app: %EXE1%
  exit /b 0
)

if exist "%EXE2%" (
  start "" "%EXE2%"
  echo Started desktop app dev build: %EXE2%
  exit /b 0
)

echo.
echo Desktop app is not built or not installed yet.
echo.
echo   One-shot build + install + run:
echo     powershell -ExecutionPolicy Bypass -File "%~dp0app\build_exe.ps1"
echo     powershell -ExecutionPolicy Bypass -File "%~dp0app\installer\install.ps1"
echo   Details: app\installer\README.md
echo.
echo   Browser version instead: http://127.0.0.1:8010/app.html
echo   Offline demo instead: open the .html file inside the "desktop" folder.
echo.
echo The app manages the LiveTalking service by itself - no need to prestart it.
pause
exit /b 1