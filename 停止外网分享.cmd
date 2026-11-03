@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 光杀 cloudflared 不够：守护脚本会自动重连，所以先留下停止标记，
rem 再把 cloudflared 和「AiLive 外网分享」窗口一起结束。
echo stop > "%~dp0stop_share.flag"

tasklist /FI "IMAGENAME eq cloudflared.exe" 2>nul | find /I "cloudflared.exe" >nul
if errorlevel 1 (
  echo 当前没有cloudflared 在运行（可能本来就没在分享）。
) else (
  taskkill /IM cloudflared.exe /F >nul 2>&1
  echo 已结束 cloudflared 隧道进程。
)

taskkill /FI "WINDOWTITLE eq AiLive 外网分享*" /T /F >nul 2>&1
if not errorlevel 1 echo 已关闭外网分享窗口。

:waitflag
if exist "%~dp0stop_share.flag" (
  rem 守护脚本退出时会自己删掉标记；等最多 4 秒
  ping -n 2 127.0.0.1 >nul
  if exist "%~dp0stop_share.flag" del "%~dp0stop_share.flag" >nul 2>&1
)

echo.
echo 外网分享已停止：外网地址立即失效，局域网地址不受影响（仍然可以继续用）。
echo.
pause