# 外网分享（Cloudflare 快速隧道）守护脚本
#
# 为什么需要守护：cloudflared 的快速隧道在边缘连接被掐断时会直接退出
# （日志里是 "Connection terminated" + "no more connections active and exiting"），
# 窗口一关地址也会失效。这里做三件事：
#   1) 用 TCP(HTTP/2) + 多条连接，避开网络对 QUIC/UDP 的干扰；
#   2) 进程退出后自动重连（新地址会重新打印，并写进「当前分享地址.txt」）；
#   3) 把地址复制到剪贴板，方便直接发给别人。
# 停止方式：关掉本窗口，或双击「停止外网分享.cmd」（会留下停止标记，本脚本据此退出）。

param(
    [int]$MaxRestarts = 50,
    [int]$WaitForUrlSeconds = 60
)

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot          # E:\AiLive
$cloudflared = Join-Path $PSScriptRoot 'cloudflared.exe'
$urlFile = Join-Path $root '当前分享地址.txt'
$stopFlag = Join-Path $root 'stop_share.flag'
$outFile = Join-Path $root 'tunnel.out.log'
$errFile = Join-Path $root 'tunnel.err.log'
$diagFile = Join-Path $root 'tunnel_supervisor.log'
$target = 'http://127.0.0.1:8010'

try { Start-Transcript -Path $diagFile -Force | Out-Null } catch { }

function Write-Head([string]$text) {
    Write-Host ''
    Write-Host ('=' * 62) -ForegroundColor DarkCyan
    Write-Host "  $text" -ForegroundColor Cyan
    Write-Host ('=' * 62) -ForegroundColor DarkCyan
}

if (-not (Test-Path -LiteralPath $cloudflared)) {
    Write-Host '[错误] 没有找到 tools\cloudflared.exe，无法建立外网隧道。' -ForegroundColor Red
    Write-Host '        请让 AI 助手重新下载 cloudflared。'
    exit 1
}

# 本机服务是否在跑（隧道建起来但服务没起，别人只会看到报错页）
$serviceUp = $false
try {
    $probe = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 -Uri "$target/api/auth/plans"
    $serviceUp = $probe.StatusCode -eq 200
} catch {
    $serviceUp = $false
}
if (-not $serviceUp) {
    Write-Host '[提示] 8010 上没有检测到数字人服务：隧道能建起来，但对方打开会是空白或报错。' -ForegroundColor Yellow
    Write-Host '       建议先双击「一键启动AI数字人.cmd」，等服务起来再分享。'
    if ([Environment]::UserInteractive -and $Host.Name -eq 'ConsoleHost') {
        Write-Host '       仍要继续的话，按任意键开始建立隧道……'
        try { $null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown') } catch { }
    } else {
        Write-Host '       （非交互运行，直接继续）'
    }
}

Write-Head '正在建立外网隧道（Cloudflare 快速隧道，约 10-30 秒）'
Write-Host '  下面会打印一串 https://随机词-随机词.trycloudflare.com —— 那才是真地址。'
Write-Host '  （文档里的 https://xxxx.trycloudflare.com 只是占位符，打不开是正常的）'
Write-Host '  地址同时写入「当前分享地址.txt」，并自动复制到剪贴板。'
Write-Host '  关掉本窗口 = 停止外网分享；隧道断了会自动重连。'

$attempt = 0
while ($true) {
    if (Test-Path -LiteralPath $stopFlag) {
        Remove-Item -LiteralPath $stopFlag -Force -ErrorAction SilentlyContinue
        Write-Host ''
        Write-Host '已按要求停止外网分享。' -ForegroundColor Yellow
        break
    }
    $attempt++
    if ($attempt -gt $MaxRestarts) {
        Write-Host ''
        Write-Host "已连续重建 $MaxRestarts 次仍未稳定，先停在这里。" -ForegroundColor Red
        Write-Host '请检查本机网络（能否访问外网），或改用固定地址方案（命名隧道 / ngrok 静态域名）。'
        break
    }

    Remove-Item -LiteralPath $outFile, $errFile -Force -ErrorAction SilentlyContinue

    # 数组形式传参，避免引号/转义踩坑；输出重定向到文件（由 Start-Process 自己写，
    # 不经过管道，避免子进程继承管道导致本脚本卡住）
    $cfArgs = @('tunnel', '--url', $target, '--no-autoupdate',
                '--protocol', 'http2', '--retries', '12')
    if ($attempt -gt 1) {
        Write-Host ''
        Write-Host "[重连 $($attempt - 1)] 正在重新建立隧道……" -ForegroundColor Yellow
    }
    $process = Start-Process -FilePath $cloudflared -ArgumentList $cfArgs `
        -RedirectStandardOutput $outFile -RedirectStandardError $errFile `
        -PassThru -WindowStyle Hidden

    # 等地址出现（cloudflared 把横幅和 JSON 日志分别写到 out/err）
    $url = $null
    $deadline = (Get-Date).AddSeconds($WaitForUrlSeconds)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 700
        foreach ($file in @($errFile, $outFile)) {
            if (Test-Path -LiteralPath $file) {
                $text = Get-Content -LiteralPath $file -Raw -Encoding UTF8 -ErrorAction SilentlyContinue
                if ($text) {
                    $match = [regex]::Matches($text, 'https://[a-z0-9-]+\.trycloudflare\.com')
                    if ($match.Count -gt 0) { $url = $match[$match.Count - 1].Value; break }
                }
            }
        }
        if ($url) { break }
        if ($process.HasExited) { break }
    }

    if ($url) {
        Write-Head '外网分享地址（把下面这行原样发给别人）'
        Write-Host ''
        Write-Host "   $url" -ForegroundColor Green
        Write-Host ''
        Write-Host '  对方打开是登录页；注册后是免费版，要完整功能请在「用户管理」发激活码。'
        Write-Host '  本窗口开着地址才有效；地址已复制到剪贴板，也写在「当前分享地址.txt」。'
        Write-Host ''
        try { Set-Clipboard -Value $url -ErrorAction Stop; Write-Host '  [已复制到剪贴板]' -ForegroundColor DarkGray } catch { }
        try {
            $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
            Set-Content -LiteralPath $urlFile -Encoding UTF8 -Value @(
                "外网分享地址（$stamp 起有效，只在分享窗口开着时有效）：",
                $url,
                '',
                '局域网地址（同一 Wi-Fi/网线的人用这个）：',
                "http://172.16.40.169:8010/app.html"
            )
        } catch { }

        # 刚建好的域名可能还要几十秒才能在 DNS 里查到（本机实测约 30-60 秒），
        # 所以这里先替用户验一次：能打开才说"可用"，避免发出去的地址打不开。
        $ready = $false
        $verifyDeadline = (Get-Date).AddSeconds(150)
        Write-Host '  正在验证这个地址能不能打开（刚建好的域名首次可能要等几十秒）……' -ForegroundColor DarkGray
        while ((Get-Date) -lt $verifyDeadline) {
            if ($process.HasExited) { break }
            try {
                $probe = Invoke-WebRequest -UseBasicParsing -TimeoutSec 8 -Uri "$url/login.html" -ErrorAction Stop
                if ($probe.StatusCode -eq 200) { $ready = $true; break }
            } catch { }
            Start-Sleep -Seconds 5
        }
        if ($ready) {
            Write-Host ''
            Write-Host '  ✅ 已验证：这个地址现在就能打开（登录页返回 200）。' -ForegroundColor Green
            Write-Host '     建议你自己也先打开一次，确认后再发给别人。' -ForegroundColor DarkGray
        } else {
            Write-Host ''
            Write-Host '  ⚠ 暂时还打不开：多半是刚建好、域名还在解析（通常再等 1 分钟就好）。' -ForegroundColor Yellow
            Write-Host '    建议你在浏览器里打开一次，能开了再发给别人。' -ForegroundColor Yellow
        }
    } else {
        Write-Host '没有拿到隧道地址（可能是网络到 Cloudflare 不通）。' -ForegroundColor Red
        foreach ($file in @($errFile, $outFile)) {
            if (Test-Path -LiteralPath $file) {
                Write-Host "最近几行日志（$([System.IO.Path]::GetFileName($file))）：" -ForegroundColor DarkGray
                Get-Content -LiteralPath $file -Encoding UTF8 -ErrorAction SilentlyContinue |
                    Select-Object -Last 5 | ForEach-Object { Write-Host "   $_" -ForegroundColor DarkGray }
            }
        }
    }

    # 等 cloudflared 退出（退出即代表隧道断了，需要重建）
    while (-not $process.HasExited) {
        Start-Sleep -Seconds 2
        if (Test-Path -LiteralPath $stopFlag) {
            try { $process.Kill() } catch { }
            break
        }
    }
    try { $process.WaitForExit(5000) | Out-Null } catch { }

    if (Test-Path -LiteralPath $stopFlag) {
        Remove-Item -LiteralPath $stopFlag -Force -ErrorAction SilentlyContinue
        Write-Host ''
        Write-Host '已按要求停止外网分享。' -ForegroundColor Yellow
        break
    }

    Write-Host ''
    Write-Host '隧道连接断了，3 秒后自动重连（地址会变，请看新的那一行）。' -ForegroundColor Yellow
    Start-Sleep -Seconds 3
}

if (Test-Path -LiteralPath $urlFile) {
    Write-Host ''
    Write-Host '提示：「当前分享地址.txt」里保留的是最近一次的地址，隧道停了就没用了。' -ForegroundColor DarkGray
}

try { Stop-Transcript | Out-Null } catch { }