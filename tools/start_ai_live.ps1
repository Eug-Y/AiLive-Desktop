$ErrorActionPreference = 'Stop'

$aiLiveRoot = Split-Path -Parent $PSScriptRoot
$projectRoot = Join-Path $aiLiveRoot 'LiveTalking'
$pythonPath = Join-Path $aiLiveRoot 'env\python.exe'
$stdoutLog = Join-Path $aiLiveRoot 'server.live.stdout.log'
$stderrLog = Join-Path $aiLiveRoot 'server.live.stderr.log'
$consoleUrl = 'http://127.0.0.1:8010/live_console.html'
$healthUrl = 'http://127.0.0.1:8010/health'
$accessUrl = 'http://127.0.0.1:8010/api/system/access'

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Python 环境不存在：$pythonPath"
}
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'app.py'))) {
    throw "LiveTalking 项目不存在：$projectRoot"
}

function Test-LiveHealth {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 $healthUrl
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Show-AccessUrls {
    Write-Host "PC 访问地址：$consoleUrl" -ForegroundColor Green
    try {
        $access = Invoke-RestMethod -UseBasicParsing -TimeoutSec 3 $accessUrl
        $lanUrls = @($access.data.lan_urls)
        if ($lanUrls.Count -eq 0) {
            Write-Host '未发现局域网 IPv4 地址，请确认电脑已连接 Wi-Fi 或网线。' -ForegroundColor Yellow
        } else {
            foreach ($url in $lanUrls) {
                Write-Host "手机访问地址：$url" -ForegroundColor Cyan
            }
            Write-Host '手机与电脑需连接同一 Wi-Fi；首次访问请允许浏览器使用摄像头和麦克风。' -ForegroundColor DarkCyan
        }
    } catch {
        Write-Host '接口需要登录，改为直接读取本机局域网地址：' -ForegroundColor Yellow
        $lanIps = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object {
                $_.IPAddress -notlike '127.*' -and
                $_.IPAddress -notlike '169.254.*' -and
                $_.AddressState -eq 'Preferred'
            } | Select-Object -ExpandProperty IPAddress
        if (-not $lanIps) {
            Write-Host '未发现局域网 IPv4 地址，请确认电脑已连接 Wi-Fi 或网线。' -ForegroundColor Yellow
        } else {
            foreach ($ip in $lanIps) {
                Write-Host "手机访问地址：http://${ip}:8010/live_console.html" -ForegroundColor Cyan
            }
            Write-Host '手机与电脑需连接同一 Wi-Fi；首次访问请允许浏览器使用摄像头和麦克风。' -ForegroundColor DarkCyan
        }
    }
}

if (Test-LiveHealth) {
    Write-Host "LiveTalking 已在运行。" -ForegroundColor Green
    Show-AccessUrls
    Start-Process $consoleUrl
    Write-Host "已打开直播工作台：$consoleUrl" -ForegroundColor Green
    exit 0
} else {
    $existingPython = Get-Process -Name python -ErrorAction SilentlyContinue |
        Where-Object { try { $_.Path -eq $pythonPath } catch { $false } } |
        Select-Object -First 1

    # Use ProcessStartInfo instead of Start-Process. Some Windows installations
    # expose both Path and PATH; PowerShell 5.1 then fails while copying the
    # inherited environment into Start-Process.
    if ($existingPython) {
        $process = $null
        Write-Host "检测到已有 LiveTalking Python 进程，正在等待服务就绪..." -ForegroundColor Cyan
    } else {
        $cmdPath = Join-Path $env:WINDIR 'System32\cmd.exe'
        $command = '/d /s /c ""' + $pythonPath +
            '" app.py --transport webrtc --model wav2lip --avatar_id wav2lip256_avatar1' +
            ' --tts edgetts --REF_FILE zh-CN-XiaoxiaoNeural > "' + $stdoutLog +
            '" 2> "' + $stderrLog + '""'
        $startInfo = New-Object System.Diagnostics.ProcessStartInfo
        $startInfo.FileName = $cmdPath
        $startInfo.Arguments = $command
        $startInfo.WorkingDirectory = $projectRoot
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $process = New-Object System.Diagnostics.Process
        $process.StartInfo = $startInfo
        [void]$process.Start()
        Write-Host "正在加载数字人模型，启动器 PID：$($process.Id)" -ForegroundColor Cyan
    }

    $ready = $false
    for ($index = 0; $index -lt 60; $index++) {
        Start-Sleep -Seconds 1
        if (Test-LiveHealth) {
            $ready = $true
            break
        }
    }
    if (-not $ready) {
        $detail = if (Test-Path -LiteralPath $stderrLog) { Get-Content -LiteralPath $stderrLog -Tail 30 | Out-String } else { '' }
        throw "服务在60秒内未就绪，请检查 server.live.stderr.log。`n$detail"
    }
    Write-Host '数字人直播服务已启动。' -ForegroundColor Green
}

Show-AccessUrls
Start-Process $consoleUrl
Write-Host "已打开直播工作台：$consoleUrl" -ForegroundColor Green
Write-Host '请勿再次重复启动；停播时双击“停止AI直播服务.cmd”。'
