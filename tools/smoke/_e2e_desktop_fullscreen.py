# -*- coding: utf-8 -*-
"""桌面端全屏真机验收：窗口是否**真正铺满屏幕**、任务栏还能不能盖上来。

背景（这条验收就是为它写的）
----------------------------
用户报「界面UI全屏的时候没有完全覆盖」。实测本机：
    Shell_TrayWnd exstyle=0x00000088 WS_EX_TOPMOST=True
即任务栏是置顶窗口，永远压在所有非置顶窗口之上；只要鼠标碰到屏幕底边，自动隐藏的任务栏
就会滑出来盖在无边框全屏窗口上。所以验收必须同时验两件事：
    1) 全屏窗口矩形 == 显示器矩形（物理像素）；
    2) 全屏期间窗口处于置顶状态，且**把鼠标推到屏幕底边后**，那一点上最上层的窗口仍是
       应用窗口，不是 Shell_TrayWnd。
另外验「界面来自桌面端自带界面源」：页面源不是 8010。

用法：
    E:\\AiLive\\env\\python.exe tools\\smoke\\_e2e_desktop_fullscreen.py
    E:\\AiLive\\env\\python.exe tools\\smoke\\_e2e_desktop_fullscreen.py --app E:\\AiLive\\app\\dist\\AILiveDesktop\\AILiveDesktop.exe
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PROJECT = Path(r"E:\AiLive")
ENV_PY = str(PROJECT / "env" / "python.exe")
APP_SRC = str(PROJECT / "app" / "app.py")
PROFILE_ROOT = str(PROJECT / "tmp" / "fs_probe_profile")
CDP = "http://127.0.0.1:9223"
TITLE = "AI 数字人桌面版"
SW_RESTORE = 9
USER, PWD = "e2e_app_qa", "DeskTop12345"
FALLBACK_USERS = ("e2e_ui_qa", "e2e_desktop_qa")
USERS_API = "http://127.0.0.1:8010/api/auth"

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)


def _auth_post(path, name, pwd):
    import urllib.request
    # 服务端 /api/auth/* 只吃 JSON
    data = json.dumps({"username": name, "password": pwd,
                       "display_name": "桌面端自检"}).encode("utf-8")
    req = urllib.request.Request(USERS_API + path, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8", "ignore") or "{}")
    except Exception as exc:
        return {"code": -1, "msg": str(exc)}


def ensure_account():
    """桌面端未登录时会先落到本地登录页，验收必须自己保证账号可用。"""
    for name in (USER,) + FALLBACK_USERS:
        if _auth_post("/register", name, PWD).get("code") == 0:
            return name
        if _auth_post("/login", name, PWD).get("code") == 0:
            return name
    return USER


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", wt.DWORD)]


user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(RECT)]
user32.GetWindow.argtypes = [wt.HWND, wt.UINT]
user32.GetWindow.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.GetAncestor.restype = wt.HWND
user32.GetForegroundWindow.restype = wt.HWND
user32.FindWindowW.restype = wt.HWND
user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
user32.MonitorFromWindow.restype = wt.HANDLE
user32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
user32.GetMonitorInfoW.argtypes = [wt.HANDLE, ctypes.POINTER(MONITORINFO)]
user32.WindowFromPoint.restype = wt.HWND
user32.GetWindowLongPtrW.restype = ctypes.c_longlong
user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]

RESULTS = []
SKIPPED = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), str(detail)[:240]))
    print(("PASS " if ok else "FAIL ") + name + ("  | " + str(detail)[:240] if detail else ""))


def skip(name, detail=""):
    """因环境原因没验的项：单独列出，不算失败也不算通过。

    这台机器是**人在用的**工作机：验"全屏时置顶 / 鼠标触底不被任务栏盖住"必须先抢占前台，
    而 Windows 不允许后台进程从正在使用的程序手里抢走前台。把它硬当失败就成了
    "有人在用电脑就报红"，所以明确标记为跳过并写清原因。
    """
    SKIPPED.append((name, str(detail)[:240]))
    print("SKIP " + name + ("  | " + str(detail)[:240] if detail else ""))


def finish():
    ok = sum(1 for _, o, _ in RESULTS if o)
    tail = ("（另 %d 项因环境跳过）" % len(SKIPPED)) if SKIPPED else ""
    print("\n==== %d/%d 通过%s ====" % (ok, len(RESULTS), tail))
    if ok != len(RESULTS):
        print("失败项：")
        for n, o, d in RESULTS:
            if not o:
                print("  - %s | %s" % (n, d))
    for n, d in SKIPPED:
        print("  ~ 跳过：%s | %s" % (n, d))
    return 1 if ok != len(RESULTS) else 0


def cls_of(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(wt.HWND(hwnd), buf, 256)
    return buf.value


def title_of(hwnd) -> str:
    n = user32.GetWindowTextLengthW(wt.HWND(hwnd))
    buf = ctypes.create_unicode_buffer(n + 2)
    user32.GetWindowTextW(wt.HWND(hwnd), buf, n + 2)
    return buf.value


def find_all_hwnds(title=TITLE):
    """同一个标题可能有多个顶层窗口（启动页残留 / 最小化占位），验收要挑真正在用的那个。"""
    hits = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        if title_of(hwnd) == title:
            hits.append(int(hwnd))
        return True

    user32.EnumWindows(cb, 0)
    return hits


def find_hwnd(title=TITLE):
    """优先选"可见且不是最小化占位"的那个窗口。"""
    best, best_score = 0, -1
    for hwnd in find_all_hwnds(title):
        r = rect_of(hwnd)
        visible = bool(user32.IsWindowVisible(wt.HWND(hwnd)))
        minimized = r[0] <= -32000 or r[1] <= -32000
        score = (2 if visible else 0) + (1 if not minimized else 0)
        if score > best_score:
            best, best_score = hwnd, score
    return best


def rect_of(hwnd):
    r = RECT()
    user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def monitor_of(hwnd):
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    user32.GetMonitorInfoW(user32.MonitorFromWindow(wt.HWND(hwnd), 2), ctypes.byref(mi))
    m = mi.rcMonitor
    return (m.left, m.top, m.right, m.bottom)


def topmost_at(x, y):
    """屏幕物理坐标点上最上层的顶层窗口。"""
    h = int(user32.WindowFromPoint(wt.POINT(x, y)) or 0)
    if not h:
        return 0, ""
    root = int(user32.GetAncestor(wt.HWND(h), 2) or 0) or h
    return root, cls_of(root)


def force_foreground(hwnd) -> bool:
    """把窗口抢到前台（跨进程 SetForegroundWindow 需要 AttachThreadInput 才被允许）。"""
    user32.ShowWindow(wt.HWND(hwnd), SW_RESTORE)
    fg = int(user32.GetForegroundWindow() or 0)
    tid_fg = user32.GetWindowThreadProcessId(wt.HWND(fg), None) if fg else 0
    tid_me = k32.GetCurrentThreadId()
    attached = False
    try:
        if tid_fg and tid_fg != tid_me:
            attached = bool(user32.AttachThreadInput(tid_me, tid_fg, True))
        user32.BringWindowToTop(wt.HWND(hwnd))
        user32.SetForegroundWindow(wt.HWND(hwnd))
        user32.SetActiveWindow(wt.HWND(hwnd))
    except Exception as exc:
        print("   force_foreground 异常：%r" % exc)
    finally:
        if attached:
            user32.AttachThreadInput(tid_me, tid_fg, False)
    time.sleep(0.8)
    cur = int(user32.GetForegroundWindow() or 0)
    return cur == hwnd or int(user32.GetAncestor(wt.HWND(cur), 2) or 0) == hwnd


def wait_cdp(timeout=90):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(CDP + "/json/version", timeout=2) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            time.sleep(1)
    return None


def cdp_busy() -> bool:
    try:
        with urllib.request.urlopen(CDP + "/json/version", timeout=2):
            return True
    except Exception:
        return False


def kill_leftover_hosts() -> int:
    """清掉残留的宿主进程。

    否则残留实例会继续占着 CDP 9223，本脚本"等 CDP 就绪"会立刻成功、然后**驱动错的那个实例**
    （实测踩到：一条超时被杀的命令留下了宿主，之后的验收连上了它，断言全乱）。
    """
    killed = 0
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq AILiveDesktop.exe", "/NH"],
                             capture_output=True, text=True, timeout=20).stdout
        for line in out.splitlines():
            parts = [p.strip() for p in line.split()]
            if parts and parts[0].lower().startswith("ailivedesktop"):
                subprocess.run(["taskkill", "/PID", parts[1], "/T", "/F"], capture_output=True)
                killed += 1
    except Exception:
        pass
    return killed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", default="", help="exe 路径；默认用源码启动")
    ap.add_argument("--keep", action="store_true", help="跑完不关掉被测实例")
    args = ap.parse_args()

    # 让本进程按物理像素工作，鼠标坐标与窗口矩形才和被测窗口同一套坐标系
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass

    from playwright.sync_api import sync_playwright

    killed = kill_leftover_hosts()
    if killed:
        print("已清掉 %d 个残留宿主进程" % killed)
        time.sleep(2)
    if cdp_busy():
        check("前置：CDP 调试端口 9223 空闲（没有残留实例）", False,
              "9223 已被占用——先手工结束 AILiveDesktop.exe 再跑")
        return finish()
    check("前置：CDP 调试端口 9223 空闲（没有残留实例）", True, "9223 free")

    os.makedirs(os.path.join(PROFILE_ROOT, "AILiveDesktop"), exist_ok=True)
    with open(os.path.join(PROFILE_ROOT, "AILiveDesktop", "config.json"), "w", encoding="utf-8") as f:
        json.dump({"project_root": str(PROJECT), "close_service_on_exit": False,
                   "window": {"width": 1360, "height": 860, "maximized": False}}, f,
                  ensure_ascii=False, indent=2)

    env = os.environ.copy()
    env["LOCALAPPDATA"] = PROFILE_ROOT
    launcher = [args.app] if args.app else [ENV_PY, APP_SRC]
    out = open(PROJECT / "logs" / "e2e_fullscreen.out.log", "w", encoding="utf-8", errors="ignore")
    proc = subprocess.Popen(launcher + ["--allow-multiple", "--debug-port", "9223"],
                            env=env, stdout=out, stderr=subprocess.STDOUT)
    saved_fg = int(user32.GetForegroundWindow() or 0)
    try:
        if not wait_cdp(90):
            check("宿主拉起 WebView2 并开放 CDP 调试端口", False, "CDP 未就绪")
            return finish()
        check("宿主拉起 WebView2 并开放 CDP 调试端口", True, "127.0.0.1:9223")

        hwnd = 0
        for _ in range(80):
            hwnd = find_hwnd()
            if hwnd:
                break
            time.sleep(0.5)
        check("找到桌面应用的原生窗口", bool(hwnd), "hwnd=%s title=%r 候选=%s"
                  % (hwnd, TITLE, find_all_hwnds()))
        if not hwnd:
            return finish()

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(CDP)
            page = next((pg for c in browser.contexts for pg in c.pages), None)
            if page is None:
                check("拿到应用页面 target", False, "无页面")
                return finish()
            page.set_default_timeout(20000)
            for _ in range(60):
                try:
                    if "/desktop.html" in (page.url or "") or "login.html" in (page.url or ""):
                        break
                except Exception:
                    pass
                time.sleep(0.5)
            time.sleep(1.0)

            # 桌面端未登录时会先落在**本地登录页**（与网页端 8010 的行为一致）。
            # 这里补一次真实表单登录，否则界面停在登录页，后面所有外壳断言都没有意义。
            if "login.html" in (page.url or ""):
                user = ensure_account()
                page.fill("#loginUsername", user)
                page.fill("#loginPassword", PWD)
                page.click("#loginSubmit")
                try:
                    page.wait_for_url(lambda u: "/desktop.html" in u and "login.html" not in u,
                                      timeout=40000)
                except Exception as exc:
                    check("在应用窗口内登录后进入桌面端界面", False, "%s | now=%s" % (exc, page.url))
            # 外壳渲染完成
            for _ in range(40):
                try:
                    if page.evaluate("() => document.querySelectorAll('#navList .nav').length") >= 13:
                        break
                except Exception:
                    pass
                time.sleep(0.5)

            # ── 1. 界面来源：必须是桌面端自带的本地界面源，不是 8010 的网页端 ──
            url = page.url or ""
            path = page.evaluate("() => location.pathname")
            origin = page.evaluate("() => location.origin")
            check("窗口加载的是桌面端自带界面（路径 /desktop.html）", path == "/desktop.html",
                  "%s（url=%s）" % (path, url))
            check("界面源不是网页端 8010", "8010" not in origin, "origin=%s" % origin)

            shell = page.evaluate(
                "() => ({nav: document.querySelectorAll('#navList .nav').length,"
                " grp: document.querySelectorAll('#navList .grp').length,"
                " brand: getComputedStyle(document.documentElement).getPropertyValue('--brand').trim(),"
                " bridge: !!(window.pywebview && window.pywebview.api)})")
            check("本地界面渲染出完整外壳（导航/分组/主色/桥接）",
                  shell["nav"] == 13 and shell["grp"] == 4 and shell["brand"] == "#38bdf8"
                  and shell["bridge"], json.dumps(shell, ensure_ascii=False))

            # 本地界面源同时要能代理接口（同源，登录态才带得上）
            health = page.evaluate(
                "async () => { const r = await fetch('/health', {credentials:'same-origin'});"
                " const j = await r.json(); return {status: r.status, ok: j.status}; }")
            check("同源反向代理打通本地服务（/health）",
                  health.get("status") == 200 and health.get("ok") == "ok", json.dumps(health))

            # ── 2. 全屏：前台状态下进入，必须铺满显示器 + 置顶 ──
            # 先切前台（复现用户点菜单的真实状态）再量基线，否则量到的可能是"最小化占位"矩形
            hold = force_foreground(hwnd)
            fg_title = title_of(user32.GetForegroundWindow()) or cls_of(user32.GetForegroundWindow())
            if hold:
                check("前置：把应用窗口切到前台（复现用户点菜单的真实状态）", True,
                      "foreground=%s" % fg_title)
            else:
                skip("前置：把应用窗口切到前台",
                     "前台被『%s』占着——Windows 不允许后台进程抢走正在使用的前台，"
                     "置顶/鼠标触底相关断言本轮跳过（几何与还原仍照验）" % fg_title)

            tray = int(user32.FindWindowW("Shell_TrayWnd", None) or 0)
            tray_topmost = bool(int(user32.GetWindowLongPtrW(wt.HWND(tray), -20) or 0) & 0x8) if tray else False
            check("前置：任务栏是置顶窗口（否则这条 bug 复现不了）", tray and tray_topmost,
                  "tray_hwnd=%s topmost=%s" % (tray, tray_topmost))

            before = rect_of(hwnd)
            mon = monitor_of(hwnd)
            check("前置：全屏前窗口并没有铺满屏幕（说明这条验收有意义）",
                  not (before[0] <= mon[0] and before[1] <= mon[1]
                       and before[2] >= mon[2] and before[3] >= mon[3]),
                  "window=%s monitor=%s" % (before, mon))

            # 进入全屏并等它收敛（窗口矩形 / 置顶 / 任务栏让位由看门狗在几百毫秒内补齐）。
            # 机器上有人正在用别的程序时会抢走前台，此时"置顶"按设计会降级——那就重新抢回前台再试。
            st = {}
            held = hold
            for attempt in range(4):
                held = force_foreground(hwnd) or held
                page.evaluate("() => { window.pywebview.api.toggle_fullscreen(); return true; }")
                deadline = time.time() + 8
                while time.time() < deadline:
                    st = page.evaluate("() => window.pywebview.api.get_fullscreen_state()")
                    if (st.get("covers") and st.get("taskbar_visible") == 0
                            and st.get("focus") == "self" and (not held or st.get("topmost"))):
                        break
                    time.sleep(0.5)
                if (st.get("covers") and st.get("taskbar_visible") == 0
                        and st.get("focus") == "self" and (not held or st.get("topmost"))):
                    break
                page.evaluate("() => { window.pywebview.api.toggle_fullscreen(); return true; }")
                time.sleep(1.0)
            covers = (st.get("window_rect") and st.get("monitor")
                      and st["window_rect"][0] <= st["monitor"][0]
                      and st["window_rect"][1] <= st["monitor"][1]
                      and st["window_rect"][2] >= st["monitor"][0] + st["monitor"][2]
                      and st["window_rect"][3] >= st["monitor"][1] + st["monitor"][3])
            check("全屏后窗口矩形精确铺满显示器", bool(st.get("covers")) and covers,
                  json.dumps(st, ensure_ascii=False))
            if held:
                check("全屏后窗口处于置顶", bool(st.get("topmost")),
                      "topmost=%s focus=%s" % (st.get("topmost"), st.get("focus")))
            else:
                skip("全屏后窗口处于置顶", "前台抢不到，按设计不置顶；st=%s" % json.dumps(st, ensure_ascii=False))
            tray = int(user32.FindWindowW("Shell_TrayWnd", None) or 0)
            tray_topmost = bool(int(user32.GetWindowLongPtrW(wt.HWND(tray), -20) or 0) & 0x8) if tray else False
            check("全屏后任务栏已让位（可见任务栏数 0）",
                  st.get("taskbar_hidden_by_app", 0) >= 1 and st.get("taskbar_visible", 1) == 0,
                  "hidden_by_app=%s visible=%s（任务栏本身 topmost=%s）"
                  % (st.get("taskbar_hidden_by_app"), st.get("taskbar_visible"), tray_topmost))

            inner = page.evaluate("() => ({iw: innerWidth, ih: innerHeight, dpr: devicePixelRatio,"
                                  " sw: screen.width, sh: screen.height})")
            check("页面视口也铺满（CSS 像素 = 屏幕 CSS 尺寸）",
                  inner["iw"] == inner["sw"] and inner["ih"] == inner["sh"],
                  json.dumps(inner))

            # ── 3. 把鼠标推到屏幕底边：任务栏不能盖上来（这条就是用户报的现象）──
            cursor = wt.POINT()
            user32.GetCursorPos(ctypes.byref(cursor))
            cx = (mon[0] + mon[2]) // 2
            moved = False
            for _ in range(3):
                if not held:
                    break
                force_foreground(hwnd)
                page.evaluate("() => { const a = window.pywebview.api;"
                              " if (a && a.get_fullscreen_state) return a.get_fullscreen_state();"
                              " return null; }")
                user32.SetCursorPos(cx, mon[3] - 2)
                time.sleep(1.0)
                user32.SetCursorPos(cx, mon[3] - 1)
                time.sleep(1.5)
                root, klass = topmost_at(cx, mon[3] - 3)
                mid, _ = topmost_at(cx, (mon[1] + mon[3]) // 2)
                if root == hwnd and mid == hwnd:
                    moved = True
                    break
            if held:
                check("鼠标触底时，屏幕底部最上层仍是应用窗口（不是任务栏）",
                      moved and topmost_at(cx, mon[3] - 3)[0] == hwnd,
                      "命中 hwnd=%s class=%r（应用 hwnd=%s）"
                      % (topmost_at(cx, mon[3] - 3)[0], topmost_at(cx, mon[3] - 3)[1], hwnd))
                _, klass_mid = topmost_at(cx, (mon[1] + mon[3]) // 2)
                check("屏幕中部同样是应用窗口", klass_mid.startswith("WindowsForms"), klass_mid)
            else:
                skip("鼠标触底时屏幕底部最上层仍是应用窗口（不是任务栏）",
                     "需要前台才能验（人工使用中，前台被『%s』占着）" % fg_title)
                skip("屏幕中部同样是应用窗口", "同上")
            user32.SetCursorPos(cursor.x, cursor.y)

            # ── 4. 退出全屏：恢复原样、不再置顶、任务栏交还 ──
            page.evaluate("() => { window.pywebview.api.toggle_fullscreen(); return true; }")
            # 桥接调用是异步的，还原本身也是"摆完核对"（WinForms 布局是延迟的）——这里等它收敛
            st2, deadline = {}, time.time() + 10
            while time.time() < deadline:
                st2 = page.evaluate("() => window.pywebview.api.get_fullscreen_state()")
                if not st2.get("fullscreen") and st2.get("taskbar_hidden_by_app", 0) == 0:
                    break
                time.sleep(0.4)
            time.sleep(0.4)
            after = rect_of(hwnd)
            check("退出全屏后恢复原来的窗口矩形", after == before and not st2.get("fullscreen"),
                  "before=%s after=%s st=%s" % (before, after, json.dumps(st2, ensure_ascii=False)))
            check("退出全屏后不再置顶", not st2.get("topmost"), json.dumps(st2, ensure_ascii=False))
            check("退出全屏后任务栏已还原（可见且仍是置顶窗口）",
                  st2.get("taskbar_visible", 0) >= 1 and st2.get("taskbar_hidden_by_app", 1) == 0
                  and bool(int(user32.GetWindowLongPtrW(wt.HWND(tray), -20) or 0) & 0x8),
                  "visible=%s hidden_by_app=%s topmost=%s"
                  % (st2.get("taskbar_visible"), st2.get("taskbar_hidden_by_app"),
                     bool(int(user32.GetWindowLongPtrW(wt.HWND(tray), -20) or 0) & 0x8)))

            # 任务栏功能没被弄坏：退出全屏后鼠标触底仍能把它弹出来
            # （自动隐藏任务栏只在鼠标"进入"底边时弹出，所以先挪开再回底边，最多试三次）
            popped, klass2 = False, ""
            for _ in range(3):
                user32.SetCursorPos(cx, (mon[1] + mon[3]) // 2)
                time.sleep(0.6)
                user32.SetCursorPos(cx, mon[3] - 1)
                time.sleep(1.6)
                _, klass2 = topmost_at(cx, mon[3] - 3)
                if klass2 in ("Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
                    popped = True
                    break
            check("退出全屏后任务栏功能正常（鼠标触底仍能弹出）", popped, "命中 class=%r" % klass2)
            user32.SetCursorPos(cursor.x, cursor.y)
            browser.close()
    finally:
        try:
            if saved_fg:
                user32.SetForegroundWindow(wt.HWND(saved_fg))
        except Exception:
            pass
        if not args.keep:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                pass
            # 兜底：确认真的没了，否则残留实例会占着 CDP 端口坑下一次验收
            for _ in range(6):
                if proc.poll() is not None:
                    break
                time.sleep(1)
            if proc.poll() is None:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        out.close()
    kill_leftover_hosts()
    return finish()


if __name__ == "__main__":
    sys.exit(main())