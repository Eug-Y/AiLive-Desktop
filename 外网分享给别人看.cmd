@echo off
chcp 65001 >nul
title AiLive 外网分享
cd /d "%~dp0"

rem 真正干活的是 tools\share_public.ps1：建立隧道 + 断线自动重连 + 打印并复制地址
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\share_public.ps1"

echo.
echo 外网分享已结束（隧道已关闭，外网地址立即失效）。
pause