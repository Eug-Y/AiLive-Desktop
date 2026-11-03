#Requires -Version 5.1
<#
.SYNOPSIS
    安装「AI 数字人桌面版」（PowerShell 5.1 兼容，无需管理员权限）。

.DESCRIPTION
    把 PyInstaller onedir 产物（AILiveDesktop\）安装到 %LOCALAPPDATA%\Programs\AILiveDesktop，
    创建桌面 / 开始菜单快捷方式，并写入 HKCU 卸载注册表项。
    卸载脚本会一并复制到安装目录，删除源目录后仍可卸载。

.PARAMETER Source
    onedir 产物目录，必须包含 AILiveDesktop.exe。
    默认：E:\AiLive\app\dist\AILiveDesktop

.PARAMETER InstallDir
    安装目标目录。默认：%LOCALAPPDATA%\Programs\AILiveDesktop

.PARAMETER Quiet
    不打印彩色进度（仍打印最终摘要），适合脚本/静默安装调用。

.EXAMPLE
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
.EXAMPLE
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Source D:\build\AILiveDesktop -Quiet

.NOTES
    退出码：0 成功 / 2 源目录或前置条件错误 / 3 复制或校验失败。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$Source = 'E:\AiLive\app\dist\AILiveDesktop',

    [Parameter(Mandatory = $false)]
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Programs\AILiveDesktop'),

    [Parameter(Mandatory = $false)]
    [string]$ProjectRoot = '',

    [Parameter(Mandatory = $false)]
    [switch]$SkipProjectRoot,

    [Parameter(Mandatory = $false)]
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# 统一控制台输出为 UTF-8，保证中文与 ✓ / ✗ 在重定向和不同代码页下都不乱码
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }

# ---------------------------------------------------------------------------
# 常量（应用元数据）
# ---------------------------------------------------------------------------
$AppDisplayName = 'AI 数字人桌面版'
$AppDirName     = 'AILiveDesktop'
$AppExeName     = 'AILiveDesktop.exe'
$AppVersion     = '1.4.1'
$AppPublisher   = 'AiLive'
$AppDescription = 'AI 数字人直播工作台桌面版'
$UninstallName  = '卸载 ' + $AppDisplayName
$ProcessName    = [System.IO.Path]::GetFileNameWithoutExtension($AppExeName)
$DataDir        = Join-Path $env:LOCALAPPDATA $AppDirName
$RegKey         = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\' + $AppDirName
$UninstallFile  = Join-Path $InstallDir 'uninstall.ps1'

$Source  = $Source.TrimEnd('\')
$InstallDir = $InstallDir.TrimEnd('\')

# ---------------------------------------------------------------------------
# 输出助手
# ---------------------------------------------------------------------------
$script:StepNo = 0
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
    $left = @(Get-Process -Name $ProcessName -ErrorAction SilentlyContinue).Count
    if ($left -eq 0) { Write-Ok '应用进程已结束' } else { Write-Warn ('仍有 ' + $left + ' 个进程在运行') }
}

function New-AppShortcut {
    param(
        [string]$Path,
        [string]$TargetPath,
        [string]$Arguments = '',
        [string]$WorkingDirectory = '',
        [string]$IconLocation = '',
        [string]$Description = ''
    )
    $parent = Split-Path -Parent $Path
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        $null = New-Item -ItemType Directory -Path $parent -Force
    }
    $shell = New-Object -ComObject WScript.Shell
    try {
        $sc = $shell.CreateShortcut($Path)
        $sc.TargetPath = $TargetPath
        $sc.Arguments = $Arguments
        $sc.WorkingDirectory = $WorkingDirectory
        if ($IconLocation -ne '') { $sc.IconLocation = $IconLocation }
        if ($Description -ne '') { $sc.Description = $Description }
        $sc.Save()
    }
    finally {
        if ($shell) { [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($shell) }
    }
}

function Set-RegValue {
    param([string]$Name, $Value, [string]$Type)
    $null = New-ItemProperty -Path $RegKey -Name $Name -Value $Value -PropertyType $Type -Force
}

# ---------------------------------------------------------------------------
# 1. 校验安装源
# ---------------------------------------------------------------------------
Write-Host ''
Write-Host ('=== ' + $AppDisplayName + ' 安装程序 v' + $AppVersion + ' ===') -ForegroundColor White
Write-Step '校验安装源'
Write-Info ('源目录   : ' + $Source)
Write-Info ('安装目录 : ' + $InstallDir)

if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
    Write-Err ('安装源目录不存在：' + $Source)
    Write-Err '请先构建 onedir 产物，或用 -Source 参数指定正确的目录。'
    exit 2
}
$SourceExe = Join-Path $Source $AppExeName
if (-not (Test-Path -LiteralPath $SourceExe -PathType Leaf)) {
    Write-Err ('安装源目录中缺少主程序 ' + $AppExeName + '：' + $Source)
    Write-Err '请确认 -Source 指向 PyInstaller 生成的 onedir 目录（其中应直接包含 AILiveDesktop.exe）。'
    exit 2
}
$UninstallSrc = Join-Path $PSScriptRoot 'uninstall.ps1'
if (-not (Test-Path -LiteralPath $UninstallSrc -PathType Leaf)) {
    Write-Err ('安装程序同目录缺少 uninstall.ps1：' + $UninstallSrc)
    Write-Err '请从完整的 installer 目录运行安装脚本。'
    exit 2
}
if ($Source -ieq $InstallDir) {
    Write-Err '安装源目录与安装目录相同，无法执行复制。'
    exit 2
}
$SourceExeSize = (Get-Item -LiteralPath $SourceExe).Length
Write-Ok ('安装源有效，主程序 ' + $SourceExeSize + ' 字节')

# ---------------------------------------------------------------------------
# 2. 覆盖升级：结束正在运行的进程（保留用户数据目录）
# ---------------------------------------------------------------------------
Write-Step '检查是否已安装'
$IsUpgrade = Test-Path -LiteralPath $InstallDir -PathType Container
if ($IsUpgrade) {
    Write-Info '检测到已安装，按「覆盖升级」处理（用户数据目录保持不变）'
} else {
    Write-Info '全新安装'
}
Stop-AppProcess

# ---------------------------------------------------------------------------
# 3. 复制文件
# ---------------------------------------------------------------------------
Write-Step '复制程序文件'
if (-not (Test-Path -LiteralPath $InstallDir)) {
    $null = New-Item -ItemType Directory -Path $InstallDir -Force
}
try {
    Get-ChildItem -LiteralPath $Source -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $InstallDir -Recurse -Force -ErrorAction Stop
    }
    Copy-Item -LiteralPath $UninstallSrc -Destination $UninstallFile -Force -ErrorAction Stop
} catch {
    Write-Err ('复制文件失败：' + $_.Exception.Message)
    Write-Err '请确认应用已完全退出，并且对安装目录有写权限。'
    exit 3
}
Write-Ok '文件复制完成'

# ---------------------------------------------------------------------------
# 4. 校验复制结果
# ---------------------------------------------------------------------------
Write-Step '校验复制结果'
$TargetExe = Join-Path $InstallDir $AppExeName
if (-not (Test-Path -LiteralPath $TargetExe -PathType Leaf)) {
    Write-Err ('校验失败：安装目录中不存在 ' + $AppExeName + ' -> ' + $TargetExe)
    exit 3
}
$TargetExeSize = (Get-Item -LiteralPath $TargetExe).Length
if ($TargetExeSize -ne $SourceExeSize) {
    Write-Err ('校验失败：主程序大小不一致。源 ' + $SourceExeSize + ' 字节，目标 ' + $TargetExeSize + ' 字节')
    exit 3
}
if (-not (Test-Path -LiteralPath $UninstallFile -PathType Leaf)) {
    Write-Err ('校验失败：卸载脚本未复制到安装目录 -> ' + $UninstallFile)
    exit 3
}
Write-Ok ('主程序大小一致（' + $TargetExeSize + ' 字节）')
Write-Ok '卸载脚本已随安装复制到安装目录'

# ---------------------------------------------------------------------------
# 5. 创建快捷方式
# ---------------------------------------------------------------------------
Write-Step '创建快捷方式'
$DesktopDir     = [Environment]::GetFolderPath('Desktop')
$StartMenuRoot  = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$StartMenuDir   = Join-Path $StartMenuRoot $AppDisplayName
$DesktopLnk     = Join-Path $DesktopDir ($AppDisplayName + '.lnk')
$StartMenuLnk   = Join-Path $StartMenuDir ($AppDisplayName + '.lnk')
$UninstallLnk   = Join-Path $StartMenuDir ($UninstallName + '.lnk')
$UninstallArgs  = '-NoProfile -ExecutionPolicy Bypass -File "' + $UninstallFile + '"'
$IconSpec       = $TargetExe + ',0'

try {
    if ($DesktopDir -and (Test-Path -LiteralPath $DesktopDir)) {
        New-AppShortcut -Path $DesktopLnk -TargetPath $TargetExe -Arguments '' `
            -WorkingDirectory $InstallDir -IconLocation $IconSpec -Description $AppDescription
        Write-Ok ('桌面：' + $DesktopLnk)
    } else {
        Write-Warn '未能定位桌面目录，跳过桌面快捷方式'
        $DesktopLnk = ''
    }

    New-AppShortcut -Path $StartMenuLnk -TargetPath $TargetExe -Arguments '' `
        -WorkingDirectory $InstallDir -IconLocation $IconSpec -Description $AppDescription
    Write-Ok ('开始菜单：' + $StartMenuLnk)

    New-AppShortcut -Path $UninstallLnk -TargetPath 'powershell.exe' -Arguments $UninstallArgs `
        -WorkingDirectory $InstallDir -IconLocation $IconSpec -Description $UninstallName
    Write-Ok ('开始菜单卸载项：' + $UninstallLnk)
} catch {
    Write-Err ('创建快捷方式失败：' + $_.Exception.Message)
    exit 3
}

# ---------------------------------------------------------------------------
# 6. 写入 HKCU 卸载注册表项
# ---------------------------------------------------------------------------
Write-Step '写入卸载注册表项（HKCU，无需管理员）'
try {
    if (-not (Test-Path -LiteralPath $RegKey)) { $null = New-Item -Path $RegKey -Force }

    $totalBytes = (Get-ChildItem -LiteralPath $InstallDir -Recurse -Force -File |
                   Measure-Object -Property Length -Sum).Sum
    if (-not $totalBytes) { $totalBytes = 0 }
    $sizeKb = [int][Math]::Ceiling([double]$totalBytes / 1024.0)
    if ($sizeKb -lt 1) { $sizeKb = 1 }

    $uninstallCmd = 'powershell.exe -ExecutionPolicy Bypass -File "' + $UninstallFile + '"'

    Set-RegValue -Name 'DisplayName'          -Value $AppDisplayName                       -Type 'String'
    Set-RegValue -Name 'DisplayVersion'       -Value $AppVersion                           -Type 'String'
    Set-RegValue -Name 'Version'              -Value $AppVersion                           -Type 'String'
    Set-RegValue -Name 'Publisher'            -Value $AppPublisher                         -Type 'String'
    Set-RegValue -Name 'InstallLocation'      -Value $InstallDir                            -Type 'String'
    Set-RegValue -Name 'DisplayIcon'          -Value $IconSpec                              -Type 'String'
    Set-RegValue -Name 'UninstallString'      -Value $uninstallCmd                          -Type 'String'
    Set-RegValue -Name 'QuietUninstallString' -Value ($uninstallCmd + ' -Quiet')            -Type 'String'
    Set-RegValue -Name 'InstallDate'          -Value ((Get-Date).ToString('yyyyMMdd'))      -Type 'String'
    Set-RegValue -Name 'EstimatedSize'        -Value $sizeKb                                -Type 'DWord'
    Set-RegValue -Name 'NoModify'             -Value 1                                      -Type 'DWord'
    Set-RegValue -Name 'NoRepair'             -Value 1                                      -Type 'DWord'
} catch {
    Write-Err ('写入注册表失败：' + $_.Exception.Message)
    exit 3
}
Write-Ok ('注册表项：' + $RegKey)
Write-Ok ('EstimatedSize = ' + $sizeKb + ' KB')

# ---------------------------------------------------------------------------
# 7. 记住项目目录（首次启动就不用再手动选了）
# ---------------------------------------------------------------------------
# 为什么要做这一步：安装目录在 %LOCALAPPDATA% 下，从这里向上找是找不到数字人项目目录的；
# 应用退出时如果没记住项目目录，下次启动就会弹「请选择项目目录」。
# 安装时我们知道源路径就在项目里，顺手把检测到的项目目录写进用户配置即可。
# 判定标准与宿主 app.py 的 looks_like_project 一致：同时存在 LiveTalking\app.py 与 env\python.exe。
$SeededRoot = ''

function Test-ProjectRoot([string]$dir) {
    if ([string]::IsNullOrWhiteSpace($dir)) { return $false }
    if (-not (Test-Path -LiteralPath $dir -PathType Container)) { return $false }
    $a = Join-Path $dir 'LiveTalking\app.py'
    $b = Join-Path $dir 'env\python.exe'
    return ((Test-Path -LiteralPath $a -PathType Leaf) -and (Test-Path -LiteralPath $b -PathType Leaf))
}

if (-not $SkipProjectRoot) {
    Write-Step '记住项目目录（省掉首次启动手动选择）'
    $candidate = $ProjectRoot

    if ([string]::IsNullOrWhiteSpace($candidate)) {
        # 从安装源目录逐级向上找项目根
        $probe = $Source
        for ($i = 0; $i -lt 5 -and $probe; $i++) {
            if (Test-ProjectRoot $probe) { $candidate = $probe; break }
            $parent = Split-Path -Parent $probe
            if ($parent -eq $probe) { break }
            $probe = $parent
        }
    }

    if ($candidate -and (Test-ProjectRoot $candidate)) {
        try {
            $cfgPath = Join-Path $DataDir 'config.json'
            if (-not (Test-Path -LiteralPath $DataDir)) { $null = New-Item -Path $DataDir -ItemType Directory -Force }

            $cfg = @{}
            if (Test-Path -LiteralPath $cfgPath -PathType Leaf) {
                try {
                    $existing = Get-Content -LiteralPath $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
                    if ($existing) {
                        foreach ($p in $existing.PSObject.Properties) { $cfg[$p.Name] = $p.Value }
                    }
                } catch {
                    Write-Warn '已有 config.json 解析失败，将重建（原文件保留为 config.json.bak）'
                    Copy-Item -LiteralPath $cfgPath -Destination ($cfgPath + '.bak') -Force -ErrorAction SilentlyContinue
                    $cfg = @{}
                }
            }
            $cfg['project_root'] = (Resolve-Path -LiteralPath $candidate).Path
            if (-not $cfg.ContainsKey('close_service_on_exit')) { $cfg['close_service_on_exit'] = $true }
            $cfg['updated_at'] = (Get-Date).ToString('s')

            $json = $cfg | ConvertTo-Json -Depth 6
            [System.IO.File]::WriteAllText($cfgPath, $json, (New-Object System.Text.UTF8Encoding($false)))
            $SeededRoot = $cfg['project_root']
            Write-Ok ('项目目录已记入配置：' + $SeededRoot)
            Write-Ok ('配置文件：' + $cfgPath)
        } catch {
            Write-Warn ('写入项目配置失败（不影响安装，首次启动时可手动选择）：' + $_.Exception.Message)
        }
    } else {
        Write-Warn '未能自动识别项目目录：首次启动时应用会让你手动选择（选到含 LiveTalking\app.py 与 env\python.exe 的目录）'
    }
} else {
    Write-Step '跳过项目目录写入（-SkipProjectRoot）'
}

# ---------------------------------------------------------------------------
# 8. 摘要
# ---------------------------------------------------------------------------
$uninstallCmdText = 'powershell.exe -ExecutionPolicy Bypass -File "' + $UninstallFile + '"'

Write-Host ''
Write-Host '================= 安装完成 =================' -ForegroundColor Green
Write-Host ('  应用名称     : ' + $AppDisplayName + '  (v' + $AppVersion + ')')
Write-Host ('  安装位置     : ' + $InstallDir)
Write-Host ('  主程序       : ' + $TargetExe)
Write-Host ('  桌面快捷方式 : ' + $(if ($DesktopLnk) { $DesktopLnk } else { '(未创建)' }))
Write-Host ('  开始菜单     : ' + $StartMenuLnk)
Write-Host ('  卸载快捷方式 : ' + $UninstallLnk)
Write-Host ('  项目目录     : ' + $(if ($SeededRoot) { $SeededRoot + '   (已记住，首次启动免手选)' } else { '(未写入，首次启动时手动选择)' }))
Write-Host ('  用户数据目录 : ' + $DataDir + '   (安装/升级不会删除)')
Write-Host ('  注册表卸载项 : ' + $RegKey)
Write-Host ('  卸载方式     : 开始菜单 → 「' + $UninstallName + '」，或执行：')
Write-Host ('                 ' + $uninstallCmdText)
Write-Host '============================================' -ForegroundColor Green
Write-Host ''

exit 0