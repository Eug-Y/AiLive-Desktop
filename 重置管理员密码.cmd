@echo off
chcp 65001 >nul
title 重置AI数字人账号密码
setlocal

echo ============================================================
echo  重置 AI 数字人直播工作台 账号密码
echo  账号数据：E:\AiLive\LiveTalking\data\auth\users.json
echo ============================================================
echo.

if not exist "E:\AiLive\tools\reset_password.py" (
  echo 找不到 E:\AiLive\tools\reset_password.py
  pause
  exit /b 1
)

if "%~1"=="" (
  "E:\AiLive\env\python.exe" "E:\AiLive\tools\reset_password.py"
) else (
  "E:\AiLive\env\python.exe" "E:\AiLive\tools\reset_password.py" "%~1"
)

echo.
pause
endlocal