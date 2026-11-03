<#
  启动桌面应用.ps1 —— 启动真正的桌面版「AI 数字人桌面版」（AILiveDesktop.exe）。

  和以前"用浏览器打开一个像桌面的网页"完全不同：
    * 启动的是独立 exe（原生 Windows 窗口 + 任务栏图标 + 自己的进程）；
    * 应用自己会把 LiveTalking 本地服务拉起来（并在启动页显示真实进度）；
    * 退出应用时，如果服务是它自己启动的，会一并停掉；如果是外部已经开着的，不会去动。

  查找顺序：
    1. 已安装版：%LOCALAPPDATA%\Programs\AILiveDesktop\AILiveDesktop.exe
    2. 开发构建：<项目>\app\dist\AILiveDesktop\AILiveDesktop.exe
  都没找到时：提示先打包/安装；加 -Build 可以自动"打包 + 安装 + 启动"。

  用法：
    powershell -ExecutionPolicy Bypass -File E:\AiLive\tools\启动桌面应用.ps1
    powershell -ExecutionPolicy Bypass -File E:\AiLive\tools\启动桌面应用.ps1 -Build
    powershell -ExecutionPolicy Bypass -File E:\AiLive\tools\启动桌面应用.ps1 -ProjectRoot E:\AiLive

  本文件必须保存为 UTF-8 BOM + CRLF（PowerShell 5.1 对无 BOM 的中文会按 GBK 解析而报错）。
#>
[CmdletBinding()]
param(
    [string]$ProjectRoot = '',
    [switch]$Build,          # 找不到 exe 时自动打包 + 安装
    [switch]$Install,        # 只安装（用现有 dist），不启动
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'

function Info($m) { if (-not $Quiet) { Write-Host $m -ForegroundColor Cyan } }
function Ok($m)   { Write-Host $m -ForegroundColor Green }
function Warn($m) { Write-Host $m -ForegroundColor Yellow }
function Die($m, $code = 1) { Write-Host $m -ForegroundColor Red; exit $code }

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) { $ProjectRoot = Split-Path -Parent $Here }
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path

$Installed = Join-Path $env:LOCALAPPDATA 'Programs\AILiveDesktop\AILiveDesktop.exe'
$DevBuild  = Join-Path $ProjectRoot 'app\dist\AILiveDesktop\AILiveDesktop.exe'
$AppDir    = Join-Path $ProjectRoot 'app'

# 已经在运行就不再开第二个（桌面应用是单实例的）
$running = Get-Process -Name 'AILiveDesktop' -ErrorAction SilentlyContinue
if ($running -and -not $Install) {
    Ok "「AI 数字人桌面版」已经在运行（PID $($running[0].Id)）。"
    Info '请到任务栏切换过去；如果一个窗口都没有，先结束 AILiveDesktop 进程再运行本脚本。'
    exit 0
}

$exe = $null
if (Test-Path -LiteralPath $Installed) { $exe = $Installed }
elseif (Test-Path -LiteralPath $DevBuild) { $exe = $DevBuild }

if ($Build -or $Install) {
    if (-not (Test-Path -LiteralPath (Join-Path $AppDir 'build_exe.ps1'))) {
        Die "找不到打包脚本：$(Join-Path $AppDir 'build_exe.ps1')"
    }
    if (-not (Test-Path -LiteralPath $DevBuild) -or $Build) {
        Info '正在打包桌面应用（首次约 1 分钟）…'
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $AppDir 'build_exe.ps1')
        if ($LASTEXITCODE -ne 0) { Die "打包失败（退出码 $LASTEXITCODE）" }
    }
    $installer = Join-Path $AppDir 'installer\install.ps1'
    if (Test-Path -LiteralPath $installer) {
        Info '正在安装（创建桌面与开始菜单快捷方式）…'
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installer -Quiet
        if ($LASTEXITCODE -ne 0) { Die "安装失败（退出码 $LASTEXITCODE）" }
        $exe = $Installed
        Ok "已安装到：$(Split-Path -Parent $Installed)"
    } else {
        Warn "没有找到安装脚本：$installer（跳过安装，直接启动开发构建）"
        $exe = $DevBuild
    }
    if ($Install) { exit 0 }
}

if (-not $exe -or -not (Test-Path -LiteralPath $exe)) {
    Warn '没有找到桌面应用主程序（AILiveDesktop.exe）。'
    Write-Host ''
    Write-Host '请先做下面任一步：' -ForegroundColor Yellow
    Write-Host '  方式一（推荐，一键）：双击  E:\AiLive\app\installer\安装桌面版.cmd'
    Write-Host '  方式二（命令行）：'
    Write-Host "    powershell -ExecutionPolicy Bypass -File `"$(Join-Path $AppDir 'build_exe.ps1')`""
    Write-Host "    powershell -ExecutionPolicy Bypass -File `"$(Join-Path $AppDir 'installer\install.ps1')`""
    Write-Host ''
    Write-Host '  或者直接让我一条命令搞定：'
    Write-Host "    powershell -ExecutionPolicy Bypass -File `"$(Join-Path $Here '启动桌面应用.ps1')`" -Build"
    Write-Host ''
    Write-Host '提示：没有 exe 也能先用浏览器版界面（http://127.0.0.1:8010/app.html），'
    Write-Host '      或用 desktop 目录里的离线演示副本（不需要服务）。'
    exit 1
}

Info "启动：$exe"
Start-Process -FilePath $exe -WorkingDirectory (Split-Path -Parent $exe)
Ok '已启动「AI 数字人桌面版」。'
Info '说明：应用会自己拉起数字人服务（启动页会显示真实进度）；'
Info '      退出应用时如果服务是它启动的会自动停止，外部已开着的服务不会被影响。'
exit 0