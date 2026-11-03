# Open the AI digital human app UI in the DEFAULT BROWSER (fallback path).
#
# The main deliverable is now a REAL desktop app (AILiveDesktop.exe) - see
# tools\启动桌面应用.ps1 / 桌面版AI数字人.cmd. This script is only the
# "no exe, just look at the UI" fallback, plus a pointer to the offline demo.
#
# NOTE: this file mentions Chinese launcher names, so it MUST be saved as
# UTF-8 with BOM. PowerShell 5.1 parses a non-ASCII .ps1 without a BOM as GBK
# and can fail with "string is missing the terminator"
# (see tools/smoke/_check_script_encoding.py). Line endings: CRLF.

$ErrorActionPreference = 'Stop'

$healthUrl  = 'http://127.0.0.1:8010/health'
$appUrl     = 'http://127.0.0.1:8010/app.html'
$starter    = Join-Path $PSScriptRoot 'start_ai_live.ps1'
$offline    = Join-Path (Split-Path -Parent $PSScriptRoot) 'desktop'

function Test-LiveHealth {
    try {
        return (Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 $healthUrl).StatusCode -eq 200
    } catch {
        return $false
    }
}

if (Test-LiveHealth) {
    Write-Host 'LiveTalking service is already running.' -ForegroundColor Green
} else {
    Write-Host 'Service is not running yet - starting it now.' -ForegroundColor Yellow
    & $starter
}

Start-Process $appUrl
Write-Host "App UI opened in browser: $appUrl" -ForegroundColor Green
Write-Host ''
Write-Host 'Reminder: the recommended way is the desktop app (independent window + self-managed service):' -ForegroundColor Cyan
Write-Host '  desktop shortcut "AI 数字人桌面版"  or  double-click 桌面版AI数字人.cmd in the project root.'
Write-Host "Offline demo (no service, demo data only): $offline"
Write-Host 'To stop the service, run 停止AI直播服务.cmd in the project root.'