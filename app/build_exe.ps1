<#
  build_exe.ps1 —— 把「AI 数字人桌面版」打包成独立的 Windows 桌面程序（PyInstaller onedir）。

  产物：E:\AiLive\app\dist\AILiveDesktop\AILiveDesktop.exe
        （onedir 目录，整个目录一起分发；安装脚本 app\installer\install.ps1 默认就取这个目录）

  用法：
    powershell -ExecutionPolicy Bypass -File E:\AiLive\app\build_exe.ps1
    powershell -ExecutionPolicy Bypass -File E:\AiLive\app\build_exe.ps1 -Console     # 带控制台的调试版
    powershell -ExecutionPolicy Bypass -File E:\AiLive\app\build_exe.ps1 -Smoke       # 打完包顺手跑一次自检启动

  注意：本文件必须以 UTF-8 BOM + CRLF 保存（Windows PowerShell 5.1 对无 BOM 的中文会乱码）。
#>
[CmdletBinding()]
param(
    [switch]$Console,          # 打包成带控制台窗口的调试版（默认 --windowed，无控制台）
    [switch]$NoClean,          # 不清理上次的构建目录，加快增量打包
    [switch]$Smoke,            # 打包后跑一次 --selftest 自检（需要本地服务可用或能自行启动）
    [string]$ProjectRoot = ''   # 默认自动识别为本脚本的上级目录
)

$ErrorActionPreference = 'Stop'

function Info($m) { Write-Host "[打包] $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[打包] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[打包] $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "[打包] 失败：$m" -ForegroundColor Red; exit 2 }

$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) { $ProjectRoot = Split-Path -Parent $AppDir }
$Python   = Join-Path $ProjectRoot 'env\python.exe'
$AppPy    = Join-Path $AppDir 'app.py'
$Splash   = Join-Path $AppDir 'splash.html'
$Icon     = Join-Path $AppDir 'app.ico'
$Vendor   = Join-Path $AppDir 'vendor_ui.py'
$UiDir    = Join-Path $AppDir 'ui'
$Name     = 'AILiveDesktop'
$DistDir  = Join-Path $AppDir 'dist'
$BuildDir = Join-Path $AppDir 'build'
$OutDir   = Join-Path $DistDir $Name
$Exe      = Join-Path $OutDir "$Name.exe"
$VerFile  = Join-Path $AppDir 'version_info.txt'

Info "项目目录：$ProjectRoot"
if (-not (Test-Path -LiteralPath $Python)) { Die "找不到项目自带 Python：$Python" }
if (-not (Test-Path -LiteralPath $AppPy))  { Die "找不到宿主程序：$AppPy" }
if (-not (Test-Path -LiteralPath $Splash)) { Die "找不到启动页：$Splash" }

# 版本号从 app.py 里读，保证 exe 的版本资源和程序一致
$version = '1.4.0'
$m = Select-String -Path $AppPy -Pattern '^VERSION\s*=\s*"([^"]+)"' -ErrorAction SilentlyContinue | Select-Object -First 1
if ($m) { $version = $m.Matches[0].Groups[1].Value }
Info "版本号：$version"

# 图标：没有就现场生成（纯 Pillow 画，不依赖外部素材）
if (-not (Test-Path -LiteralPath $Icon)) {
    Info '图标不存在，调用 make_icon.py 生成…'
    & $Python (Join-Path $AppDir 'make_icon.py')
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $Icon)) { Die '图标生成失败' }
}

# exe 的版本资源（右键属性 → 详细信息里能看到产品名/版本/公司）
$verParts = @($version.Split('.') + @('0', '0', '0'))[0..3]
$verTuple = ($verParts | ForEach-Object { [int]$_ }) -join ', '
$verFileText = @"
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=($verTuple),
    prodvers=($verTuple),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('080404B0', [
        StringStruct('CompanyName', 'AiLive'),
        StringStruct('FileDescription', 'AI 数字人桌面版'),
        StringStruct('FileVersion', '$version'),
        StringStruct('InternalName', '$Name'),
        StringStruct('LegalCopyright', 'AiLive'),
        StringStruct('OriginalFilename', '$Name.exe'),
        StringStruct('ProductName', 'AI 数字人桌面版'),
        StringStruct('ProductVersion', '$version')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"@
Set-Content -LiteralPath $VerFile -Value $verFileText -Encoding UTF8
Info "已写入版本资源文件：$VerFile"

if (-not $NoClean) {
    Info '清理上次构建目录…'
    foreach ($d in @($BuildDir, (Join-Path $AppDir "$Name.spec"))) {
        if (Test-Path -LiteralPath $d) { Remove-Item -LiteralPath $d -Recurse -Force -ErrorAction SilentlyContinue }
    }
}

# ── 同步桌面端界面副本 ───────────────────────────────────────────────────────
# 桌面端渲染的是**随应用打包**的界面副本（app\ui），由 app\vendor_ui.py 从
# LiveTalking\web 固化过来（界面本体 + 资源，含逐文件 sha256 清单）。每次打包都重新同步，
# 保证 exe 里的界面和项目里的网页界面一致；没有它 exe 会退回加载网页端页面（不是桌面端界面）。
if (-not (Test-Path -LiteralPath $Vendor)) { Die "找不到界面固化脚本：$Vendor" }
Info '同步桌面端界面副本（LiveTalking\web -> app\ui）…'
& $Python $Vendor
if ($LASTEXITCODE -ne 0) { Die "界面固化失败（vendor_ui.py 返回 $LASTEXITCODE）" }
foreach ($need in @('desktop.html', 'MANIFEST.json')) {
    if (-not (Test-Path -LiteralPath (Join-Path $UiDir $need))) {
        Die "界面副本不完整：缺少 $(Join-Path $UiDir $need)"
    }
}
$uiCount = (Get-ChildItem -LiteralPath $UiDir -Recurse -File | Measure-Object).Count
$uiSize = [math]::Round(((Get-ChildItem -LiteralPath $UiDir -Recurse -File | Measure-Object -Property Length -Sum).Sum) / 1MB, 2)
Ok "界面副本就绪：$uiCount 个文件 / $uiSize MB（打包进 exe 的 ui 目录）"

# ── 组装 PyInstaller 参数 ────────────────────────────────────────────────────
# conda 环境的 _ssl / _hashlib 依赖 Library\bin 下的 OpenSSL DLL，PyInstaller 不会自动带上，
# 缺了就会报 “ImportError: DLL load failed while importing _ssl”（实测踩到过）。
$opensslBins = @()
foreach ($dll in @('libssl-3-x64.dll', 'libcrypto-3-x64.dll', 'libssl-3.dll', 'libcrypto-3.dll')) {
    foreach ($sub in @('Library\bin', 'DLLs', '')) {
        $cand = if ($sub) { Join-Path $ProjectRoot "env\$sub\$dll" } else { Join-Path $ProjectRoot "env\$dll" }
        if (Test-Path -LiteralPath $cand) { $opensslBins += $cand; break }
    }
}
if ($opensslBins.Count -eq 0) { Warn '没有在 env 里找到 OpenSSL DLL，打包后 _ssl 可能不可用' }
foreach ($b in $opensslBins) { Info "附加二进制：$b" }

$args = @(
    '-m', 'PyInstaller',
    '--noconfirm',
    '--name', $Name,
    '--distpath', $DistDir,
    '--workpath', $BuildDir,
    '--specpath', $AppDir,
    '--icon', $Icon,
    '--version-file', $VerFile,
    '--add-data', "$Splash;.",
    '--add-data', "$Icon;.",
    '--add-data', "$(Join-Path $AppDir 'app.png');.",
    # 桌面端自带界面（含逐文件 sha256 清单）：窗口只从这份副本渲染，不再加载网页端页面
    '--add-data', "$UiDir;ui",
    # pywebview 的平台后端是动态导入的，必须显式声明；js/lib 数据文件由它自带的 hook 收集
    '--hidden-import', 'webview.platforms.edgechromium',
    '--hidden-import', 'webview.platforms.winforms',
    '--hidden-import', 'webview.http',
    '--hidden-import', 'clr_loader',
    '--hidden-import', 'pythonnet',
    '--collect-submodules', 'webview',
    '--collect-binaries', 'clr_loader',
    '--collect-binaries', 'pythonnet',
    '--exclude-module', 'tkinter',
    '--exclude-module', 'matplotlib',
    '--exclude-module', 'PyQt5',
    '--exclude-module', 'PySide6'
)
foreach ($b in $opensslBins) { $args += @('--add-binary', "$b;.") }
$args += if ($Console) { '--console' } else { '--windowed' }
if (-not $NoClean) { $args += '--clean' }
$args += $AppPy

Info "开始打包（onedir）：$Name"
# conda 环境的扩展模块依赖 env\Library\bin 下的 DLL（libffi / OpenSSL / liblzma 等）。
# PyInstaller 解析 PE 依赖时只查 PATH，所以要把 conda 的这几个目录放到 PATH 最前面，
# 它才会自动把这些 DLL 收进包里；否则运行时报
# “ImportError: DLL load failed while importing _ctypes / _ssl”。
$env:PATH = (Join-Path $ProjectRoot 'env\Library\bin') + ';' +
            (Join-Path $ProjectRoot 'env\DLLs') + ';' +
            (Join-Path $ProjectRoot 'env') + ';' + $env:PATH
Info ("PATH 前缀已加入 conda 目录：" + (Join-Path $ProjectRoot 'env\Library\bin'))
Info ("$Python " + ($args -join ' '))
& $Python @args
if ($LASTEXITCODE -ne 0) { Die "PyInstaller 返回 $LASTEXITCODE" }

if (-not (Test-Path -LiteralPath $Exe)) { Die "没有生成 $Exe" }

$exeSize = [math]::Round((Get-Item -LiteralPath $Exe).Length / 1MB, 2)
$dirSize = [math]::Round(((Get-ChildItem -LiteralPath $OutDir -Recurse -File | Measure-Object -Property Length -Sum).Sum) / 1MB, 2)
Ok "打包完成：$Exe"
Ok "可执行文件 $exeSize MB，整目录 $dirSize MB"

# 构建记录，便于以后核对“这个 exe 是哪次打包出来的”
$record = @(
    "AI 数字人桌面版 构建记录",
    "版本：$version",
    "构建时间：$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')",
    "模式：$(if ($Console) { '带控制台（调试）' } else { '窗口程序（windowed）' })",
    "产物：$Exe",
    "目录：$OutDir",
    "大小：exe $exeSize MB / 整目录 $dirSize MB",
    "自带界面：$uiCount 个文件 / $uiSize MB（app\ui，来自 LiveTalking\web 的固化副本）",
    "Python：$Python",
    "说明：整个 AILiveDesktop 目录一起分发；安装用 app\installer\安装桌面版.cmd"
)
Set-Content -LiteralPath (Join-Path $DistDir 'BUILD-INFO.txt') -Value $record -Encoding UTF8
Ok "已写入构建记录：$(Join-Path $DistDir 'BUILD-INFO.txt')"

if ($Smoke) {
    Info '自检启动（--selftest --close-after 25）…'
    & $Exe --selftest --close-after 25 --allow-multiple
    $code = $LASTEXITCODE
    if ($code -eq 0) { Ok "自检通过（退出码 0）" } else { Warn "自检退出码 $code，请查看日志 %LOCALAPPDATA%\AILiveDesktop\logs\desktop_host.log 或项目 logs\desktop_host.log" }
}

Ok '全部完成。'
exit 0