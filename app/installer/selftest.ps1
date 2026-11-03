#Requires -Version 5.1
<#
.SYNOPSIS
    installer 目录自检脚本（编码 / 行尾 / 语法 / 完整安装卸载往返）。

.DESCRIPTION
    静态检查（默认执行）：
      - install.ps1 / uninstall.ps1 必须是 UTF-8 with BOM 且 CRLF 行尾（PowerShell 5.1 才不会把中文按 GBK 解析）
      - 两个 .cmd 必须是纯 ASCII 内容 + CRLF 行尾 + 无 BOM
      - 四个文件都必须能被 PowerShell 5.1 解析器成功解析
    往返测试（-RoundTrip）：
      - 生成一个假 dist（复制 cmd.exe 充当 AILiveDesktop.exe）
      - 安装 -> 校验安装目录/三个快捷方式/注册表项 -> 卸载 -> 校验全部消失 -> 清理
      - 测试安装目录放在 %TEMP% 下；若本机已存在真实安装，默认拒绝执行（除非 -Force）

.PARAMETER RoundTrip
    执行完整的「安装 -> 校验 -> 卸载 -> 校验」往返测试。

.PARAMETER Force
    即使本机检测到真实安装，也强制执行往返测试（会临时改写快捷方式与 HKCU 卸载注册表项）。

.PARAMETER KeepTemp
    保留测试用假 dist 目录（默认删除）。

.EXAMPLE
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\selftest.ps1
.EXAMPLE
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\selftest.ps1 -RoundTrip

.NOTES
    退出码：0 全部通过 / 4 存在失败项。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [switch]$RoundTrip,

    [Parameter(Mandatory = $false)]
    [switch]$Force,

    [Parameter(Mandatory = $false)]
    [switch]$KeepTemp
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }

$Here = $PSScriptRoot
$AppDisplayName = 'AI 数字人桌面版'
$RealInstallDir = Join-Path $env:LOCALAPPDATA 'Programs\AILiveDesktop'
$RegKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\AILiveDesktop'
$TestInstallDir = Join-Path $env:TEMP 'AILiveDesktop_selftest_install'
$TestDummyRoot = Join-Path $Here '_tmp_dummy'
$TestDummySrc = Join-Path $TestDummyRoot 'AILiveDesktop'

$script:Results = @()
function Add-Result {
    param([string]$Name, [bool]$Passed, [string]$Detail = '')
    $script:Results += , @{ Name = $Name; Passed = $Passed; Detail = $Detail }
    if ($Passed) { Write-Host ('  [OK]   ' + $Name) -ForegroundColor Green }
    else { Write-Host ('  [FAIL] ' + $Name + '   ' + $Detail) -ForegroundColor Red }
}

function Get-ByteReport {
    param([string]$Path)
    $enc = [Text.Encoding]::GetEncoding(28591)
    $b = [IO.File]::ReadAllBytes($Path)
    $s = $enc.GetString($b)
    $nonAscii = 0
    foreach ($x in $b) { if ($x -gt 127) { $nonAscii++ } }
    $isBom = $false
    if ($b.Length -ge 3) { $isBom = ($b[0] -eq 0xEF -and $b[1] -eq 0xBB -and $b[2] -eq 0xBF) }
    return [pscustomobject]@{
        Bytes    = $b.Length
        BOM      = $isBom
        NonASCII = $nonAscii
        CRLF     = ([regex]::Matches($s, "`r`n")).Count
        BareLF   = ([regex]::Matches($s, "(?<!`r)`n")).Count
        BareCR   = ([regex]::Matches($s, "`r(?!`n)")).Count
    }
}

function Invoke-Script {
    param([string]$Path, [string[]]$Arguments = @())
    $stdout = Join-Path $env:TEMP ('_ds_selftest_' + [guid]::NewGuid().ToString('N') + '.txt')
    $parts = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $Path + '"'))
    foreach ($a in $Arguments) { $parts += $a }
    $line = 'powershell.exe ' + ($parts -join ' ')
    cmd /c ($line + ' > "' + $stdout + '" 2>&1') | Out-Null
    $code = $LASTEXITCODE
    $text = ''
    if (Test-Path -LiteralPath $stdout) {
        $text = [IO.File]::ReadAllText($stdout, [Text.Encoding]::UTF8)
        Remove-Item -LiteralPath $stdout -Force
    }
    return [pscustomobject]@{ ExitCode = $code; Output = $text }
}

# ===========================================================================
Write-Host ''
Write-Host '=== 1. 静态检查：编码 / 行尾 / 语法 ===' -ForegroundColor White

$ps1Files = @('install.ps1', 'uninstall.ps1')
$cmdFiles = @('安装桌面版.cmd', '卸载桌面版.cmd')

foreach ($f in $ps1Files) {
    $p = Join-Path $Here $f
    if (-not (Test-Path -LiteralPath $p -PathType Leaf)) { Add-Result ($f + ' 存在') $false $p; continue }
    $r = Get-ByteReport $p
    Add-Result ($f + ' 为 UTF-8 with BOM') ($r.BOM -eq $true) ('BOM=' + $r.BOM)
    Add-Result ($f + ' 含中文（非 ASCII 字节>0）') ($r.NonASCII -gt 0) ('nonASCII=' + $r.NonASCII)
    Add-Result ($f + ' 行尾全部为 CRLF') (($r.CRLF -gt 0) -and ($r.BareLF -eq 0) -and ($r.BareCR -eq 0)) ('CRLF=' + $r.CRLF + ' bareLF=' + $r.BareLF + ' bareCR=' + $r.BareCR)
    $errs = $null
    $null = [System.Management.Automation.Language.Parser]::ParseFile($p, [ref]$null, [ref]$errs)
    Add-Result ($f + ' 可被 PS 5.1 解析') (($null -eq $errs) -or ($errs.Count -eq 0)) (($errs | ForEach-Object { $_.Message }) -join '; ')
}

foreach ($f in $cmdFiles) {
    $p = Join-Path $Here $f
    if (-not (Test-Path -LiteralPath $p -PathType Leaf)) { Add-Result ($f + ' 存在') $false $p; continue }
    $r = Get-ByteReport $p
    Add-Result ($f + ' 内容为纯 ASCII') ($r.NonASCII -eq 0) ('nonASCII=' + $r.NonASCII)
    Add-Result ($f + ' 无 BOM') ($r.BOM -eq $false) ('BOM=' + $r.BOM)
    Add-Result ($f + ' 行尾全部为 CRLF（无裸 LF）') (($r.CRLF -gt 0) -and ($r.BareLF -eq 0) -and ($r.BareCR -eq 0)) ('CRLF=' + $r.CRLF + ' bareLF=' + $r.BareLF)
}

# ===========================================================================
if ($RoundTrip) {
    Write-Host ''
    Write-Host '=== 2. 往返测试：安装 -> 校验 -> 卸载 -> 校验 ===' -ForegroundColor White

    $realPresent = (Test-Path -LiteralPath (Join-Path $RealInstallDir 'AILiveDesktop.exe')) -or (Test-Path -LiteralPath $RegKey)
    if ($realPresent -and -not $Force) {
        Add-Result '往返测试前置条件（本机无真实安装）' $false '检测到真实安装，已跳过以免破坏；确认要测试请加 -Force'
    }
    else {
        $systemExe = Join-Path $env:SystemRoot 'System32\cmd.exe'
        if (Test-Path -LiteralPath $TestDummyRoot) { Remove-Item -LiteralPath $TestDummyRoot -Recurse -Force }
        $null = New-Item -ItemType Directory -Path (Join-Path $TestDummySrc '_internal') -Force
        Copy-Item -LiteralPath $systemExe -Destination (Join-Path $TestDummySrc 'AILiveDesktop.exe') -Force
        'fake-dll' | Set-Content -LiteralPath (Join-Path $TestDummySrc '_internal\python310.dll')
        Add-Result '生成假 dist' (Test-Path -LiteralPath (Join-Path $TestDummySrc 'AILiveDesktop.exe')) $TestDummySrc

        $r1 = Invoke-Script -Path (Join-Path $Here 'install.ps1') -Arguments @('-Source', ('"' + $TestDummySrc + '"'), '-InstallDir', ('"' + $TestInstallDir + '"'), '-Quiet')
        Add-Result '安装退出码为 0' ($r1.ExitCode -eq 0) ('exit=' + $r1.ExitCode)

        $srcSize = (Get-Item -LiteralPath (Join-Path $TestDummySrc 'AILiveDesktop.exe')).Length
        $dstExe = Join-Path $TestInstallDir 'AILiveDesktop.exe'
        $dstOk = (Test-Path -LiteralPath $dstExe) -and ((Get-Item -LiteralPath $dstExe).Length -eq $srcSize)
        Add-Result '安装目录内主程序存在且大小一致' $dstOk $dstExe
        Add-Result '卸载脚本已复制到安装目录' (Test-Path -LiteralPath (Join-Path $TestInstallDir 'uninstall.ps1')) (Join-Path $TestInstallDir 'uninstall.ps1')

        $desktop = [Environment]::GetFolderPath('Desktop')
        $smDir = Join-Path $env:APPDATA ('Microsoft\Windows\Start Menu\Programs\' + $AppDisplayName)
        $lnks = @(
            (Join-Path $desktop ($AppDisplayName + '.lnk')),
            (Join-Path $smDir ($AppDisplayName + '.lnk')),
            (Join-Path $smDir ('卸载 ' + $AppDisplayName + '.lnk'))
        )
        foreach ($l in $lnks) { Add-Result ('快捷方式存在：' + (Split-Path -Leaf $l)) (Test-Path -LiteralPath $l) $l }
        Add-Result 'HKCU 卸载注册表项存在' (Test-Path -LiteralPath $RegKey) $RegKey

        $r2 = Invoke-Script -Path (Join-Path $TestInstallDir 'uninstall.ps1') -Arguments @('-InstallDir', ('"' + $TestInstallDir + '"'), '-Quiet')
        Add-Result '卸载退出码为 0（含自检全部通过）' ($r2.ExitCode -eq 0) ('exit=' + $r2.ExitCode)

        Add-Result '卸载后安装目录已消失' (-not (Test-Path -LiteralPath $TestInstallDir)) $TestInstallDir
        foreach ($l in $lnks) { Add-Result ('卸载后快捷方式已消失：' + (Split-Path -Leaf $l)) (-not (Test-Path -LiteralPath $l)) $l }
        Add-Result '卸载后注册表项已消失' (-not (Test-Path -LiteralPath $RegKey)) $RegKey

        if (-not $KeepTemp) {
            if (Test-Path -LiteralPath $TestDummyRoot) { Remove-Item -LiteralPath $TestDummyRoot -Recurse -Force }
            Add-Result '测试残留已清理（假 dist）' (-not (Test-Path -LiteralPath $TestDummyRoot)) $TestDummyRoot
        }
    }
}

# ===========================================================================
$failed = @($script:Results | Where-Object { -not $_.Passed }).Count
Write-Host ''
Write-Host '================= 自检结果 =================' -ForegroundColor White
Write-Host ('  检查项总数 : ' + $script:Results.Count)
if ($failed -eq 0) {
    Write-Host '  失败项     : 0' -ForegroundColor Green
    Write-Host '  结论       : 全部通过 ✓' -ForegroundColor Green
} else {
    Write-Host ('  失败项     : ' + $failed) -ForegroundColor Red
    Write-Host '  结论       : 存在失败项 ✗' -ForegroundColor Red
    foreach ($x in ($script:Results | Where-Object { -not $_.Passed })) {
        Write-Host ('    - ' + $x.Name + '   ' + $x.Detail) -ForegroundColor Red
    }
}
Write-Host '============================================' -ForegroundColor White
Write-Host ''

if ($failed -gt 0) { exit 4 }
exit 0