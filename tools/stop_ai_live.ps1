$ErrorActionPreference = 'Stop'

$aiLiveRoot = Split-Path -Parent $PSScriptRoot
$expectedPython = Join-Path $aiLiveRoot 'env\python.exe'
$listener = Get-NetTCPConnection -LocalPort 8010 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1

if (-not $listener) {
    Write-Host '8010 端口没有运行中的 AI 直播服务。' -ForegroundColor Yellow
    exit 0
}

$process = Get-Process -Id $listener.OwningProcess -ErrorAction Stop
if ($process.Path -ne $expectedPython) {
    throw "拒绝停止未知程序：$($process.ProcessName) (PID $($process.Id), $($process.Path))"
}

Stop-Process -Id $process.Id
$process.WaitForExit(10000)
if (-not $process.HasExited) {
    Stop-Process -Id $process.Id -Force
}
Write-Host "AI 直播服务已停止，PID：$($process.Id)" -ForegroundColor Green
