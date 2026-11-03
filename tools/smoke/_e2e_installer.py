# -*- coding: utf-8 -*-
"""安装包端到端验收：真实 dist → 安装 → 查快捷方式/注册表 → 启动已安装的 exe → 卸载 → 查残留。

这是「安装包」这一层的实测证据，和宿主层（_e2e_desktop_app.py）互补：
  * 安装/卸载用真实的 app\\dist\\AILiveDesktop 产物（不是假 dist）；
  * 启动的是安装目录里的 AILiveDesktop.exe，验证它自己能跑起来（退出码 0 + frozen=True 日志）。

用法：E:\\AiLive\\env\\python.exe tools\\smoke\\_e2e_installer.py
      E:\\AiLive\\env\\python.exe tools\\smoke\\_e2e_installer.py --keep-installed   # 装完不卸（留给你手动看）
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(r"E:\AiLive")
DIST = PROJECT / "app" / "dist" / "AILiveDesktop"
INSTALLER = PROJECT / "app" / "installer"
HOST_LOG = PROJECT / "logs" / "desktop_host.log"
LOCALAPP = Path(os.environ.get("LOCALAPPDATA", ""))
APPDATA = Path(os.environ.get("APPDATA", ""))
INSTALL_DIR = LOCALAPP / "Programs" / "AILiveDesktop"
EXE = INSTALL_DIR / "AILiveDesktop.exe"
DATA_DIR = LOCALAPP / "AILiveDesktop"
DISPLAY = "AI 数字人桌面版"
START_MENU_DIR = APPDATA / "Microsoft" / "Windows" / "Start Menu" / "Programs" / DISPLAY
REG_KEY = r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\AILiveDesktop"

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), str(detail)[:240]))
    print(("PASS " if ok else "FAIL ") + name + ("  | " + str(detail)[:240] if detail else ""))


def ps(cmd, timeout=600):
    return subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive",
                           "-ExecutionPolicy", "Bypass", "-Command", cmd],
                          capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="ignore")


def ps_file(script, *extra, timeout=900):
    return subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                           "-File", str(script), *extra],
                          capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="ignore")


def desktop_dir() -> Path:
    r = ps("[Environment]::GetFolderPath('Desktop')")
    return Path((r.stdout or "").strip())


def reg_value(name):
    r = ps("(Get-ItemProperty -Path '%s' -Name '%s' -ErrorAction SilentlyContinue).%s" % (REG_KEY, name, name))
    return (r.stdout or "").strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-installed", action="store_true", help="装完不卸载")
    args = ap.parse_args()

    check("构建产物存在（onedir dist）", (DIST / "AILiveDesktop.exe").is_file() and DIST != INSTALL_DIR,
          "%s（%d 个文件）" % (DIST / "AILiveDesktop.exe",
                            len(list(DIST.rglob("*"))) if DIST.is_dir() else 0))
    if not (DIST / "AILiveDesktop.exe").is_file():
        return finish()

    # ── 安装 ────────────────────────────────────────────────────────────────
    # 先删掉用户数据目录，模拟"全新机器"：这样才能真正验出安装器有没有记住项目目录
    # （否则上一次运行留下的 config.json 会掩盖问题）。
    print("\n=== 前置：清空用户数据目录（模拟全新机器）===")
    if DATA_DIR.exists():
        subprocess.run(["cmd", "/c", "rmdir", "/S", "/Q", str(DATA_DIR)], capture_output=True)
    check("用户数据目录已清空（无历史配置可用）", not DATA_DIR.exists(), str(DATA_DIR))

    print("\n=== 安装（install.ps1，真实 dist）===")
    r = ps_file(INSTALLER / "install.ps1", "-Quiet")
    check("安装脚本退出码为 0", r.returncode == 0, (r.stdout or "").strip()[-160:] or (r.stderr or "")[-160:])
    check("安装目录里有主程序", EXE.is_file(), str(EXE))
    if EXE.is_file():
        src_size = (DIST / "AILiveDesktop.exe").stat().st_size
        check("安装后的主程序与源大小一致", EXE.stat().st_size == src_size,
              "src=%s dst=%s" % (src_size, EXE.stat().st_size))
    check("卸载脚本已随安装复制（源目录删了也能卸）", (INSTALL_DIR / "uninstall.ps1").is_file(),
          str(INSTALL_DIR / "uninstall.ps1"))

    desk_lnk = desktop_dir() / (DISPLAY + ".lnk")
    sm_lnk = START_MENU_DIR / (DISPLAY + ".lnk")
    sm_un_lnk = START_MENU_DIR / ("卸载 " + DISPLAY + ".lnk")
    check("桌面快捷方式已创建", desk_lnk.is_file(), str(desk_lnk))
    check("开始菜单程序项已创建", sm_lnk.is_file(), str(sm_lnk))
    check("开始菜单卸载项已创建", sm_un_lnk.is_file(), str(sm_un_lnk))
    check("注册表卸载项存在（控制面板可卸载）", bool(reg_value("DisplayName")), REG_KEY)
    check("卸载项显示名正确", reg_value("DisplayName") == DISPLAY, reg_value("DisplayName"))
    check("卸载项版本号为 1.2.0", reg_value("DisplayVersion") == "1.2.0", reg_value("DisplayVersion"))
    check("卸载项带卸载命令", "uninstall.ps1" in reg_value("UninstallString").lower(),
          reg_value("UninstallString")[:120])

    # 安装器要把项目目录写进用户配置，否则全新机器上首次启动会弹"请选择项目目录"
    cfg_path = DATA_DIR / "config.json"
    seeded = ""
    if cfg_path.is_file():
        try:
            seeded = str(json.loads(cfg_path.read_text(encoding="utf-8")).get("project_root", ""))
        except Exception as exc:
            seeded = "解析失败：%s" % exc
    check("安装器已记住项目目录（首次启动免手选）", seeded.replace("/", "\\").lower() == str(PROJECT).lower(),
          "config.json project_root=%r" % seeded)

    # ── 启动已安装的程序 ────────────────────────────────────────────────────
    print("\n=== 启动安装目录里的 exe（自检模式，10 秒自关）===")
    log_before = HOST_LOG.read_text(encoding="utf-8", errors="ignore") if HOST_LOG.is_file() else ""
    t0 = time.time()
    try:
        run = subprocess.run([str(EXE), "--selftest", "--close-after", "10", "--allow-multiple"],
                             timeout=300, capture_output=True, text=True,
                             encoding="utf-8", errors="ignore")
        rc = run.returncode
    except subprocess.TimeoutExpired:
        rc = None
        subprocess.run(["taskkill", "/IM", "AILiveDesktop.exe", "/F"], capture_output=True)
    check("已安装的 exe 正常退出（退出码 0）", rc == 0, "returncode=%s 用时 %.0f 秒" % (rc, time.time() - t0))

    log_after = HOST_LOG.read_text(encoding="utf-8", errors="ignore") if HOST_LOG.is_file() else ""
    new = log_after[len(log_before):] if len(log_after) > len(log_before) else log_after
    check("宿主日志确认这是打包后的程序（frozen=True）", "frozen=True" in new,
          " | ".join([x for x in new.splitlines() if "启动（frozen" in x][:1])[:200])
    check("安装目录的程序能自己找到项目目录",
          "已定位项目目录：E:\\AiLive" in new,
          " | ".join([x for x in new.splitlines() if "已定位项目目录" in x][:1])[:200])
    check("启动流程走通（出现界面加载完成）", "页面加载完成" in new,
          " | ".join([x for x in new.splitlines() if "页面加载完成" in x][:1])[:200])

    if args.keep_installed:
        print("\n（--keep-installed：跳过卸载）")
        return finish()

    # ── 卸载 ────────────────────────────────────────────────────────────────
    print("\n=== 卸载（用安装目录里的 uninstall.ps1）===")
    r = ps_file(INSTALL_DIR / "uninstall.ps1", "-Quiet", timeout=900)
    check("卸载脚本退出码为 0", r.returncode == 0, (r.stdout or "").strip()[-200:] or (r.stderr or "")[-200:])
    time.sleep(2)
    check("安装目录已删除", not INSTALL_DIR.exists(), str(INSTALL_DIR))
    check("桌面快捷方式已删除", not desk_lnk.exists(), str(desk_lnk))
    check("开始菜单项已删除", not sm_lnk.is_file() and not sm_un_lnk.is_file(), str(START_MENU_DIR))
    check("注册表卸载项已删除", not reg_value("DisplayName"), REG_KEY)
    check("没有残留 AILiveDesktop 进程", not ps("Get-Process AILiveDesktop -ErrorAction SilentlyContinue").stdout.strip(),
          "无残留进程")
    return finish()


def finish():
    ok = sum(1 for _, o, _ in RESULTS if o)
    total = len(RESULTS)
    print("\n==== %d/%d 通过 ====" % (ok, total))
    if ok != total:
        print("失败项：")
        for n, o, d in RESULTS:
            if not o:
                print("  - %s | %s" % (n, d))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())