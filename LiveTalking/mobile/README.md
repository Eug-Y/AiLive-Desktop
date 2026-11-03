# AI 数字人 Android 手机端

这是一个轻量 Android WebView 安装包工程。AI 模型仍运行在电脑上，手机 APK 负责连接电脑上的 LiveTalking 工作台，支持账号、商品、话术、预检、开播和下播操作。

## 构建

需要 Android SDK、Build Tools、Gradle 和 JDK 17+：

```powershell
cd E:\AiLive\LiveTalking\mobile\android
gradle assembleDebug
```

生成文件：

`app\build\outputs\apk\debug\app-debug.apk`

## 首次使用

1. 电脑启动 LiveTalking 服务。
2. 手机和电脑连接同一 Wi-Fi。
3. 安装 APK。
4. 首次打开输入电脑地址，例如 `http://192.168.1.104:8010`。
5. 授予摄像头和麦克风权限，进入直播工作台。

当前 APK 是远程控制壳，不会把 wav2lip 模型复制到手机运行。
