@echo off
chcp 65001 >nul
setlocal

rem 把主版本 web\app.html 同步到本目录的离线演示副本。
rem 用浏览器直接双击打开（file://）时会自动进入离线演示模式：不调用任何接口，数据全部标注“演示”。
rem 正式用法是桌面应用（AILiveDesktop.exe），不是这个离线副本。
rem
rem 本文件必须保持 CRLF 行尾：LF 行尾 + chcp 65001 会让 cmd 按字节偏移读错行，
rem 把后面的命令切碎执行（2026-10-11 已实测复现，勿改成 LF）。
set "SRC=%~dp0..\LiveTalking\web\app.html"
set "DST=%~dp0AI数字人桌面界面（离线演示）.html"
rem 旧原型（浏览器里的假桌面版，已不推荐，仅留档）也一并同步，避免它过期误导人
set "SRC2=%~dp0..\LiveTalking\web\desktop.html"
set "DST2=%~dp0AI数字人桌面版.html"

if not exist "%SRC%" (
  echo [FAIL] 找不到主版本：%SRC%
  pause
  exit /b 1
)

copy /Y "%SRC%" "%DST%" >nul
if errorlevel 1 (
  echo [FAIL] 复制失败，请检查文件是否被占用
  pause
  exit /b 1
)

if exist "%SRC2%" (
  copy /Y "%SRC2%" "%DST2%" >nul
)

echo [ OK ] 已同步离线演示版：
echo        %DST%
echo 说明：内容与主版本完全一致；双击用浏览器打开时自动降级为离线演示，不需要启动服务。
echo       旧原型（假桌面版，不推荐）同步在：%DST2%
pause
endlocal
exit /b 0