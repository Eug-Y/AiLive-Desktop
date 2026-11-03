$ErrorActionPreference = 'Stop'

$projectRoot = Join-Path (Split-Path -Parent $PSScriptRoot) 'LiveTalking'
$androidRoot = Join-Path $projectRoot 'mobile\android'
$output = Join-Path $projectRoot 'mobile\AI数字人直播-debug.apk'
$localSdk = Join-Path (Split-Path -Parent $PSScriptRoot) 'android-sdk'
$localGradle = Join-Path (Split-Path -Parent $PSScriptRoot) 'gradle-8.9\bin\gradle.bat'

if (-not (Test-Path -LiteralPath (Join-Path $androidRoot 'build.gradle'))) {
    throw "Android 工程不存在：$androidRoot"
}

$gradlePath = $null
if (Test-Path -LiteralPath $localGradle) {
    $gradlePath = $localGradle
} else {
    $gradle = Get-Command gradle -ErrorAction SilentlyContinue
    if ($gradle) {
        $gradlePath = $gradle.Source
    }
}
if (-not $gradlePath) {
    throw '未找到 gradle。请安装 Android SDK、Gradle 和 JDK 17+ 后重试。'
}
if (Test-Path -LiteralPath $localSdk) {
    $env:ANDROID_SDK_ROOT = $localSdk
    $env:ANDROID_HOME = $localSdk
}

Push-Location $androidRoot
try {
    & $gradlePath assembleDebug
    if ($LASTEXITCODE -ne 0) {
        throw "APK 构建失败，退出码：$LASTEXITCODE"
    }
} finally {
    Pop-Location
}

$apk = Join-Path $androidRoot 'app\build\outputs\apk\debug\app-debug.apk'
if (-not (Test-Path -LiteralPath $apk)) {
    throw "构建完成但未找到 APK：$apk"
}

Copy-Item -LiteralPath $apk -Destination $output -Force
Write-Host "APK 已生成：$output" -ForegroundColor Green
