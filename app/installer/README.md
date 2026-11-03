# AI 数字人桌面版 · 安装 / 卸载脚本

面向 `E:\AiLive\app\dist\AILiveDesktop\`（PyInstaller onedir 产物）的 Windows 安装与卸载脚本。
**全部只写 HKCU 与 %LOCALAPPDATA%，不需要管理员权限。**

## 文件说明

| 文件 | 说明 |
| --- | --- |
| `install.ps1` | 安装 / 覆盖升级。默认源 `E:\AiLive\app\dist\AILiveDesktop` |
| `uninstall.ps1` | 卸载。安装时会被复制到安装目录，删掉源目录后仍可卸载 |
| `安装桌面版.cmd` | 双击安装（纯 ASCII 内容，内部调用 `install.ps1` 后 `pause`） |
| `卸载桌面版.cmd` | 双击卸载 |
| `selftest.ps1` | 自检：编码/行尾/语法，`-RoundTrip` 时跑完整安装卸载往返 |

## 使用

```powershell
# 双击
安装桌面版.cmd
卸载桌面版.cmd

# 或命令行
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Source D:\build\AILiveDesktop -Quiet
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -ProjectRoot E:\AiLive      # 指定项目目录
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -SkipProjectRoot           # 跳过项目目录写入
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\uninstall.ps1 -KeepData
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\selftest.ps1 -RoundTrip
```

## 安装时会记住项目目录（2026-10-12 新增）

安装目录在 `%LOCALAPPDATA%\Programs\AILiveDesktop`，从那里向上找**找不到**数字人项目目录
（`looks_like_project` 要求同目录下同时有 `LiveTalking\app.py` 和 `env\python.exe`），
所以"装了却让用户每次手动选目录"会很别扭。安装器因此多做一步：

1. 从 `-Source` 逐级向上（最多 5 层）识别项目根；也可以用 `-ProjectRoot` 直接指定；
2. 把识别到的路径写入 `%LOCALAPPDATA%\AILiveDesktop\config.json` 的 `project_root`
   （只改这一个键，保留已有的窗口尺寸 / 是否最大化 / 服务托管等设置；写入格式与宿主一致：UTF-8 无 BOM、2 空格缩进）；
3. 原有 `config.json` 解析失败时会先备份成 `config.json.bak` 再重建；
4. `-SkipProjectRoot` 可跳过；识别不到时只警告、不影响安装，首次启动让用户在启动页选一次也行。

卸载默认会删除用户数据目录，所以"卸载 → 重装"会自动重新写入，不会留下过期路径。
自检 `selftest.ps1 -RoundTrip` 覆盖这一行为；端到端验收见 `tools\smoke\_e2e_installer.py`（24 项，含"全新机器"场景）。

## 从 v1.3.0 起：自带界面也一并安装

安装器是**整目录复制**（`AILiveDesktop.exe` + `_internal\`），所以随应用打包的界面
`_internal\ui\`（47 个文件 / 1.34 MB）也会一起装上；装完的桌面端渲染的是这份自带副本，
不再加载 `8010` 的网页端页面，全屏也会真正铺满屏幕。

因此**升级请用新的 `app\dist\AILiveDesktop` 覆盖安装**——拿旧 dist 覆盖回去会退回
"界面其实是网页端 + 全屏不铺满"的旧行为。安装后想确认版本：主程序右键 → 属性 → 详细信息，
或看安装目录里的 `BUILD-INFO.txt`。

## 安装后产生的路径

| 项目 | 位置 |
| --- | --- |
| 安装目录 | `%LOCALAPPDATA%\Programs\AILiveDesktop` |
| 用户数据 | `%LOCALAPPDATA%\AILiveDesktop`（日志 / WebView 配置；安装与升级都不会删除） |
| 桌面快捷方式 | `<桌面>\AI 数字人桌面版.lnk` |
| 开始菜单 | `%APPDATA%\Microsoft\Windows\Start Menu\Programs\AI 数字人桌面版\` 下两个 .lnk |
| 卸载注册表项 | `HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\AILiveDesktop` |

## 退出码

| 脚本 | 码 | 含义 |
| --- | --- | --- |
| install.ps1 | 0 | 成功 |
| | 2 | 安装源缺失 / 缺少 `AILiveDesktop.exe` / 前置条件不满足 |
| | 3 | 复制失败或复制后校验不一致（主程序大小与源不符） |
| uninstall.ps1 | 0 | 成功（自检全部 ✓） |
| | 4 | 自检存在 ✗ |
| selftest.ps1 | 0 / 4 | 全部通过 / 存在失败项 |

## 已知行为与设计取舍

- **编码是硬要求**：`install.ps1` / `uninstall.ps1` / `selftest.ps1` 必须保存为 **UTF-8 with BOM + CRLF**，
  否则 Windows PowerShell 5.1 会按 GBK 解析中文导致乱码甚至语法错误。
  两个 `.cmd` 必须**纯 ASCII 内容 + CRLF + 无 BOM**。改动后请跑 `selftest.ps1` 复核。
- **自删除**：`uninstall.ps1` 可能就在安装目录里运行。实测 Windows PowerShell 5.1 用 `-File` 启动时
  **不会锁定正在执行的 .ps1**，因此可以直接递归删除自身所在目录，并在同一进程内完成 ✓/✗ 自检。
  若目录确实被其它进程占用，会退化为「先清其余内容，再把收尾交给一个延迟进程」，
  结果写入 `%TEMP%\AILiveDesktop_uninstall_result.txt`。
- **`-FromInstalled`**：声明「本次从安装目录内部运行」。脚本会**自动识别**该情形（脚本自身路径
  就在 `-InstallDir` 下），一般无需手动传。它只影响自删除的处理方式，**不缩小卸载范围** ——
  快捷方式、注册表项与（未加 `-KeepData` 时的）用户数据目录仍会一并清除，这与自检项一致。
- **结束进程只针对 `AILiveDesktop`**（按进程名匹配），不会影响项目里其它 Python 服务。
- **快捷方式定位**：桌面目录用 `[Environment]::GetFolderPath('Desktop')` 获取，
  因此在桌面被重定向到 OneDrive / 其它盘符的机器上也能正确落地。
- 卸载快捷方式的目标是 `powershell.exe -File <InstallDir>\uninstall.ps1`（而非主程序 exe），
  工作目录与图标仍指向安装目录。