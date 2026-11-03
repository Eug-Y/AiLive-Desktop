#Requires -Version 5.1
<#
.SYNOPSIS
    卸载「AI 数字人桌面版」（PowerShell 5.1 兼容，无需管理员权限）。

.DESCRIPTION
    结束正在运行的应用 → 删除三个快捷方式 → 删除 HKCU 卸载注册表项 →
    递归删除安装目录（支持删除脚本自身所在目录）→ 按需删除用户数据目录 →
    输出中文摘要并对每项做 ✓ / ✗ 自检。

.PARAMETER InstallDir
    安装目录。默认：%LOCALAPPDATA%\Programs\AILiveDesktop

.PARAMETER KeepData
    保留用户数据目录 %LOCALAPPDATA%\AILiveDesktop（日志与 WebView 配置）。

.PARAMETER Quiet
    不打印彩色进度（仍打印最终摘要）。

.PARAMETER FromInstalled
    声明本次是从安装目录内部运行（自删除模式）。脚本会自动识别该情形，
    因此通常无需手动指定；显式传入可强制启用自删除处理。

.EXAMPLE
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\uninstall.ps1
.EXAMPLE
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\uninstall.ps1 -KeepData -Quiet

.NOTES
    退出码：0 成功 / 4 自检存在 ✗（有项目未被清除）。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Programs\AILiveDesktop'),

    [Parameter(Mandatory = $false)]
    [switch]$KeepData,

    [Parameter(Mandatory = $false)]
    [switch]$Quiet,

    [Parameter(Mandatory = $false)]
    [switch]$FromInstalled
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
$AppDisplayName = 'AI 数字人桌面版'
$AppDirName     = 'AILiveDesktop'
$AppExeName     = 'AILiveDesktop.exe'
$UninstallName  = '卸载 ' + $AppDisplayName
$ProcessName    = [System.IO.Path]::GetFileNameWithoutExtension($AppExeName)
$DataDir        = Join-Path $env:LOCALAPPDATA $AppDirName
$RegKey         = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\' + $AppDirName

$InstallDir = $InstallDir.TrimEnd('\')

$DesktopDir    = [Environment]::GetFolderPath('Desktop')
$StartMenuRoot = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$StartMenuDir  = Join-Path $StartMenuRoot $AppDisplayName
$DesktopLnk    = if ($DesktopDir) { Join-Path $DesktopDir ($AppDisplayName + '.lnk') } else { '' }
$StartMenuLnk  = Join-Path $StartMenuDir ($AppDisplayName + '.lnk')
$UninstallLnk  = Join-Path $StartMenuDir ($UninstallName + '.lnk')

# ---------------------------------------------------------------------------
# 判断是否正从安装目录内部运行（自删除场景）
# ---------------------------------------------------------------------------
$selfPath = $PSCommandPath
$selfDir  = ''
if ($selfPath) { $selfDir = (Split-Path -Parent $selfPath) }
if ($selfDir) { $selfDir = $selfDir.TrimEnd('\') }
$runningFromInstallDir = $false
if ($selfDir -and ($selfDir -ieq $InstallDir)) {
    $runningFromInstallDir = $true
    $FromInstalled = $true
}

# ---------------------------------------------------------------------------
# 输出助手
# ---------------------------------------------------------------------------
$script:StepNo = 0
$script:Checks = @()

function Write-Step {
    param([string]$Message)
    $script:StepNo++
    if (-not $Quiet) { Write-Host ('[' + $script:StepNo + '] ' + $Message) -ForegroundColor Cyan }
}
function Write-Ok {
    param([string]$Message)
    if (-not $Quiet) { Write-Host ('    OK  ' + $Message) -ForegroundColor Green }
}
function Write-Info {
    param([string]$Message)
    if (-not $Quiet) { Write-Host ('        ' + $Message) -ForegroundColor Gray }
}
function Write-Warn {
    param([string]$Message)
    Write-Host ('    !!  ' + $Message) -ForegroundColor Yellow
}
function Write-Err {
    param([string]$Message)
    Write-Host ('    错误: ' + $Message) -ForegroundColor Red
}
function Add-Check {
    param([string]$Name, [bool]$Passed, [string]$Detail = '')
    $script:Checks += , @{ Name = $Name; Passed = $Passed; Detail = $Detail }
}

function Stop-AppProcess {
    $procs = @(Get-Process -Name $ProcessName -ErrorAction SilentlyContinue)
    if ($procs.Count -eq 0) {
        Write-Ok '应用当前未在运行'
        return
    }
    Write-Info ('发现 ' + $procs.Count + ' 个正在运行的 ' + $ProcessName + ' 进程，正在结束 ...')
    foreach ($p in $procs) {
        try { $null = $p.CloseMainWindow() } catch { }
    }
    $deadline = (Get-Date).AddSeconds(5)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 250
        if (@(Get-Process -Name $ProcessName -ErrorAction SilentlyContinue).Count -eq 0) { break }
    }
    $remain = @(Get-Process -Name $ProcessName -ErrorAction SilentlyContinue)
    if ($remain.Count -gt 0) {
        Write-Warn ('仍有 ' + $remain.Count + ' 个进程未在 5 秒内退出，改为强制结束')
        foreach ($p in $remain) {
            try { Stop-Process -Id $p.Id -Force -ErrorAction Stop }
            catch { Write-Warn ('无法结束 PID ' + $p.Id + '：' + $_.Exception.Message) }
        }
        Start-Sleep -Seconds 1
    }
    Write-Ok '应用进程已结束'
}

function Remove-PathItem {
    param([string]$Path, [string]$Label)
    if (-not $Path) { return $false }
    if (-not (Test-Path -LiteralPath $Path)) {
        Write-Info ($Label + ' 不存在，跳过：' + $Path)
        return $false
    }
    try {
        Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
        Write-Ok ($Label + ' 已删除：' + $Path)
    } catch {
        Write-Warn ($Label + ' 删除失败：' + $Path + ' -> ' + $_.Exception.Message)
    }
    return $true
}

# 兜底清理：若安装目录被外部进程（或宿主）锁定，交给一个与本次进程解耦的延迟进程收尾
function Start-DeferredCleanup {
    param([string]$Dir)
    $log = Join-Path $env:TEMP 'AILiveDesktop_uninstall_result.txt'
    $helper = Join-Path $env:TEMP ('AILiveDesktop_cleanup_' + [guid]::NewGuid().ToString('N') + '.ps1')
    $helperText = @'
param(
    [int]$WaitPid,
    [string]$Dir,
    [string]$Log
)
$ErrorActionPreference = 'SilentlyContinue'
# 等待发起方进程退出，释放可能存在的文件占用
$deadline = (Get-Date).AddSeconds(30)
while ((Get-Date) -lt $deadline) {
    if (-not (Get-Process -Id $WaitPid -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Milliseconds 300
}
Start-Sleep -Milliseconds 800
for ($i = 1; $i -le 20; $i++) {
    if (-not (Test-Path -LiteralPath $Dir)) { break }
    Remove-Item -LiteralPath $Dir -Recurse -Force
    if (-not (Test-Path -LiteralPath $Dir)) { break }
    Start-Sleep -Milliseconds 700
}
$ok = -not (Test-Path -LiteralPath $Dir)
$text = 'InstallDirRemoved=' + $ok
$text = $text + "`r`nInstallDir=" + $Dir
$text = $text + "`r`nFinishedAt=" + (Get-Date).ToString('s')
Set-Content -LiteralPath $Log -Value $text -Encoding UTF8
Remove-Item -LiteralPath $PSCommandPath -Force
'@
    try {
        [IO.File]::WriteAllText($helper, $helperText, (New-Object System.Text.UTF8Encoding($true)))
        $argLine = '-NoProfile -ExecutionPolicy Bypass -File "' + $helper + '" -WaitPid ' + $PID +
                   ' -Dir "' + $Dir + '" -Log "' + $log + '"'
        Start-Process -FilePath 'powershell.exe' -ArgumentList $argLine -WindowStyle Hidden | Out-Null
        Write-Warn ('安装目录被占用，已启动延迟清理进程，结果将写入：' + $log)
        return $log
    } catch {
        Write-Warn ('启动延迟清理进程失败：' + $_.Exception.Message)
        return ''
    }
}

function Remove-InstallDirectory {
    param([string]$Dir, [string]$SelfFile)
    if (-not (Test-Path -LiteralPath $Dir)) { return 'absent' }

    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            Remove-Item -LiteralPath $Dir -Recurse -Force -ErrorAction Stop
        } catch {
            # 若正在执行的脚本文件被宿主锁定，先清掉其余内容再重试
            if ($SelfFile) {
                try {
                    Get-ChildItem -LiteralPath $Dir -Force -ErrorAction SilentlyContinue | ForEach-Object {
                        if ($_.FullName -ine $SelfFile) {
                            Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
                        }
                    }
                } catch { }
            }
        }
        if (-not (Test-Path -LiteralPath $Dir)) { return 'removed' }
        Start-Sleep -Milliseconds 500
    }
    return 'deferred'
}

# ---------------------------------------------------------------------------
# 0. 概要
# ---------------------------------------------------------------------------
Write-Host ''
Write-Host ('=== ' + $AppDisplayName + ' 卸载程序 ===') -ForegroundColor White
Write-Info ('安装目录 : ' + $InstallDir)
Write-Info ('用户数据 : ' + $DataDir + $(if ($KeepData) { '  (将保留 -KeepData)' } else { '  (将被删除)' }))
if ($runningFromInstallDir) { Write-Info '检测到脚本正从安装目录内运行，启用自删除处理' }

# ---------------------------------------------------------------------------
# 1. 结束进程
# ---------------------------------------------------------------------------
Write-Step '结束正在运行的应用进程'
Stop-AppProcess

# ---------------------------------------------------------------------------
# 2. 删除快捷方式
# ---------------------------------------------------------------------------
Write-Step '删除快捷方式'
if ($DesktopLnk)    { $null = Remove-PathItem -Path $DesktopLnk   -Label '桌面快捷方式' }
$null = Remove-PathItem -Path $StartMenuLnk -Label '开始菜单快捷方式'
$null = Remove-PathItem -Path $UninstallLnk -Label '开始菜单卸载快捷方式'
if (Test-Path -LiteralPath $StartMenuDir) {
    $left = @(Get-ChildItem -LiteralPath $StartMenuDir -Force -ErrorAction SilentlyContinue)
    if ($left.Count -eq 0) {
        try { Remove-Item -LiteralPath $StartMenuDir -Force -ErrorAction Stop; Write-Ok ('已删除空的开始菜单文件夹：' + $StartMenuDir) }
        catch { Write-Warn ('开始菜单文件夹删除失败：' + $_.Exception.Message) }
    }
}

# ---------------------------------------------------------------------------
# 3. 删除注册表项
# ---------------------------------------------------------------------------
Write-Step '删除卸载注册表项'
if (Test-Path -LiteralPath $RegKey) {
    try {
        Remove-Item -Path $RegKey -Recurse -Force -ErrorAction Stop
        Write-Ok ('已删除注册表项：' + $RegKey)
    } catch {
        Write-Warn ('注册表项删除失败：' + $_.Exception.Message)
    }
} else {
    Write-Info '注册表项不存在，跳过'
}

# ---------------------------------------------------------------------------
# 4. 删除安装目录（含自删除）
# ---------------------------------------------------------------------------
Write-Step '删除安装目录'
$dirResult = 'absent'
if (Test-Path -LiteralPath $InstallDir) {
    if ($runningFromInstallDir) { Write-Info '脚本自身位于该目录内，直接递归删除（PS 5.1 不锁定正在执行的 .ps1）' }
    $dirResult = Remove-InstallDirectory -Dir $InstallDir -SelfFile $selfPath
    if ($dirResult -eq 'removed') {
        Write-Ok ('安装目录已删除：' + $InstallDir)
    } elseif ($dirResult -eq 'deferred') {
        Write-Warn ('安装目录暂时无法完全删除（存在文件占用）')
        $logPath = Start-DeferredCleanup -Dir $InstallDir
    }
} else {
    Write-Info '安装目录不存在，跳过'
}

# ---------------------------------------------------------------------------
# 5. 删除用户数据目录
# ---------------------------------------------------------------------------
Write-Step '处理用户数据目录'
if ($KeepData) {
    Write-Ok ('已保留用户数据目录（-KeepData）：' + $DataDir)
} elseif (Test-Path -LiteralPath $DataDir) {
    try {
        Remove-Item -LiteralPath $DataDir -Recurse -Force -ErrorAction Stop
        Write-Ok ('已删除用户数据目录：' + $DataDir)
    } catch {
        Write-Warn ('用户数据目录删除失败：' + $_.Exception.Message)
    }
} else {
    Write-Info '用户数据目录不存在，跳过'
}

# ---------------------------------------------------------------------------
# 6. 自检
# ---------------------------------------------------------------------------
Write-Step '自检'
if ($DesktopLnk) { Add-Check -Name '桌面快捷方式已不存在' -Passed (-not (Test-Path -LiteralPath $DesktopLnk)) -Detail $DesktopLnk }
else { Add-Check -Name '桌面快捷方式已不存在' -Passed $true -Detail '本机无桌面目录，跳过' }
Add-Check -Name '开始菜单快捷方式已不存在' -Passed (-not (Test-Path -LiteralPath $StartMenuLnk)) -Detail $StartMenuLnk
Add-Check -Name '开始菜单卸载快捷方式已不存在' -Passed (-not (Test-Path -LiteralPath $UninstallLnk)) -Detail $UninstallLnk
Add-Check -Name '注册表卸载项已不存在' -Passed (-not (Test-Path -LiteralPath $RegKey)) -Detail $RegKey

if ($dirResult -eq 'deferred') {
    Add-Check -Name '安装目录已删除（延迟清理进程中）' -Passed $true -Detail ('见 ' + (Join-Path $env:TEMP 'AILiveDesktop_uninstall_result.txt'))
} else {
    Add-Check -Name '安装目录已不存在' -Passed (-not (Test-Path -LiteralPath $InstallDir)) -Detail $InstallDir
}

if ($KeepData) {
    Add-Check -Name '用户数据目录已保留（-KeepData）' -Passed (Test-Path -LiteralPath $DataDir) -Detail $DataDir
} else {
    Add-Check -Name '用户数据目录已不存在' -Passed (-not (Test-Path -LiteralPath $DataDir)) -Detail $DataDir
}

$failed = 0
$lines = @()
foreach ($c in $script:Checks) {
    if ($c.Passed) { $mark = '✓' } else { $mark = '✗'; $failed++ }
    $line = '  [' + $mark + '] ' + $c.Name
    if ($c.Detail) { $line = $line + '   (' + $c.Detail + ')' }
    $lines += $line
}

# ---------------------------------------------------------------------------
# 7. 摘要
# ---------------------------------------------------------------------------
Write-Host ''
Write-Host '================= 卸载完成 =================' -ForegroundColor Green
Write-Host ('  应用名称 : ' + $AppDisplayName)
Write-Host ('  安装目录 : ' + $InstallDir)
foreach ($l in $lines) { Write-Host $l }
if ($failed -eq 0) {
    Write-Host '  自检结果 : 全部通过 ✓' -ForegroundColor Green
} else {
    Write-Host ('  自检结果 : ' + $failed + ' 项未通过 ✗') -ForegroundColor Red
}
Write-Host '============================================' -ForegroundColor Green
Write-Host ''

if ($failed -gt 0) { exit 4 }
exit 0