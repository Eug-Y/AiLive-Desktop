@echo off
chcp 65001 >nul
title 停止AI数字人直播服务
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "E:\AiLive\tools\stop_ai_live.ps1"
pause
