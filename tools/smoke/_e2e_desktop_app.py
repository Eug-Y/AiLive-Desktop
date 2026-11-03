# -*- coding: utf-8 -*-
"""桌面应用（AILiveDesktop）真实验收：启动宿主 → 用 CDP 连进真实 WebView2 → 断言界面与桥接。

为什么这样做：桌面应用的窗口是 WebView2，普通 Playwright 打不开。宿主用
`--debug-port` 给 WebView2 开 CDP 端口，Playwright 就能连进**真实应用窗口内部**
做断言；再配合宿主日志、进程退出码、端口存活情况，就能给出可复核的验收证据。

用法：
  E:\\AiLive\\env\\python.exe tools\\smoke\\_e2e_desktop_app.py                    # 场景一：外部服务已运行
  E:\\AiLive\\env\\python.exe tools\\smoke\\_e2e_desktop_app.py --scenario managed  # 场景二：由应用自己拉起服务
  E:\\AiLive\\env\\python.exe tools\\smoke\\_e2e_desktop_app.py --app <exe 路径>
"""
import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ENV_PY = r"E:\AiLive\env\python.exe"
APP_SRC = r"E:\AiLive\app\app.py"
LOG = Path(r"E:\AiLive\logs\desktop_host.log")
SHOTS = Path(r"E:\AiLive\_qa\app-v1")
SHOTS.mkdir(parents=True, exist_ok=True)
CDP = "http://127.0.0.1:9222"
USER, PWD = "e2e_app_qa", "DeskTop12345"
FALLBACK_USERS = ("e2e_ui_qa", "e2e_desktop_qa")
HEALTH = "http://127.0.0.1:8010/health"
USERS_API = "http://127.0.0.1:8010/api/auth"
WAIT_UI = 600          # 启动页 → 应用界面最多等 10 分钟（服务首次加载模型可能很慢）
WAIT_EXIT = 90         # exit_app 之后最多等 90 秒


def _auth_post(path, name, pwd):
    # 服务端 /api/auth/* 只吃 JSON（_body() 用 request.json()），表单编码会被当成空参数
    data = json.dumps({"username": name, "password": pwd,
                       "display_name": "桌面端自检"}).encode("utf-8")
    req = urllib.request.Request(USERS_API + path, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8", "ignore") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8", "ignore") or "{}")
        except Exception:
            return {"code": -1, "msg": "HTTP %s" % exc.code}
    except Exception as exc:
        return {"code": -1, "msg": str(exc)}


def ensure_account():
    """保证自检账号可用：先注册，已存在/被限流就退回登录。

    自检账号可能被 tools\\cleanup_e2e_accounts.py 清理掉，之前就因此让登录页卡住、
    "状态栏在线" 误判成失败——验收脚本必须自己保证前置账号存在，别依赖历史遗留。
    """
    for name in (USER,) + FALLBACK_USERS:
        body = _auth_post("/register", name, PWD)
        if body.get("code") == 0:
            return name, "注册成功"
        if _auth_post("/login", name, PWD).get("code") == 0:
            return name, "复用已有账号"
    return USER, "账号不可用（登录页会失败，界面将走离线演示）"

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), str(detail)[:240]))
    print(("PASS " if ok else "FAIL ") + name + ("  | " + str(detail)[:240] if detail else ""))


def http_ok(url, timeout=3.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 500
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def port_free(port=8010):
    import socket
    s = socket.socket(); s.settimeout(0.8)
    try:
        return s.connect_ex(("127.0.0.1", port)) != 0
    finally:
        s.close()


def run_ps(script, timeout=180):
    return subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script],
                          capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="ignore")


def native_windows(pid):
    """列出该进程的可见顶层窗口标题（证明“独立原生窗口”真实存在）。"""
    user32 = ctypes.windll.user32
    titles = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, lparam):
        wpid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value == pid and user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                titles.append(buf.value)
        return True

    user32.EnumWindows(cb, None)
    return titles


def wait_cdp(timeout=90):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(CDP + "/json/version", timeout=2) as r:
                return json.loads(r.read().decode("utf-8", "ignore"))
        except Exception:
            time.sleep(1.0)
    return None


def kill_leftover_hosts():
    """清掉上一次跑测试残留的宿主实例（否则它的 WebView2 会占着 9222，导致连到错误的窗口）。

    注意：只匹配命令行里含 `app\\app.py` 的 python（桌面宿主），绝不能误杀 LiveTalking 服务
    （服务命令行是 `env\\python.exe app.py --transport ...`，不含该片段）。
    """
    killed = []
    try:
        out = subprocess.check_output(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -like '*app\\app.py*' } | "
             "ForEach-Object { $_.ProcessId }"],
            timeout=30, text=True, encoding="utf-8", errors="ignore").split()
    except Exception:
        out = []
    for pid in out:
        pid = pid.strip()
        if pid.isdigit():
            subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True)
            killed.append(pid)
    if killed:
        print("已清理残留宿主进程：", ",".join(killed))
        time.sleep(2)
    return killed


def wait_port_free(port=9222, timeout=20):
    import socket
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = socket.socket(); s.settimeout(0.6)
        try:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return True
        finally:
            s.close()
        time.sleep(0.6)
    return False


def kill_tree(pid):
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", choices=["external", "managed"], default="external")
    ap.add_argument("--app", default="", help="exe 路径；默认用源码方式启动")
    ap.add_argument("--keep-profile", action="store_true", help="不清空 WebView 配置目录")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    kill_leftover_hosts()
    check("前置：CDP 调试端口 9222 可用（无残留宿主）", wait_port_free(9222, 25), "9222 free")

    if args.scenario == "managed":
        print("场景 managed：先停掉外部服务，交给应用自己启动")
        r = run_ps(r"E:\AiLive\tools\stop_ai_live.ps1")
        print((r.stdout or "")[-300:], (r.stderr or "")[-200:])
        time.sleep(2)
        check("前置：8010 端口已释放", port_free(), "port_free=%s" % port_free())
    else:
        check("前置：外部服务正在运行", http_ok(HEALTH), HEALTH)

    launcher = [args.app] if args.app else [ENV_PY, APP_SRC]
    cmd = launcher + ["--selftest", "--close-after", "900", "--debug-port", "9222",
                      "--wait", "420", "--allow-multiple"]
    if not args.keep_profile:
        cmd.append("--reset-profile")
    print("宿主命令：", " ".join(cmd))

    log_before = LOG.read_text(encoding="utf-8", errors="ignore") if LOG.is_file() else ""
    host_log = Path(os.environ.get("LOCALAPPDATA", "")) / "AILiveDesktop" / "logs" / "desktop_host.log"
    host_before = host_log.read_text(encoding="utf-8", errors="ignore") if host_log.is_file() else ""

    out_log = Path(r"E:\AiLive\logs\host_e2e_%s.out.log" % args.scenario)
    out_fh = out_log.open("w", encoding="utf-8", errors="ignore")
    proc = subprocess.Popen(cmd, stdout=out_fh, stderr=subprocess.STDOUT)
    exited_by_bridge = False

    try:
        ver = wait_cdp(90)
        check("宿主拉起 WebView2 并开放 CDP 调试端口", bool(ver), (ver or {}).get("Browser", "CDP 未就绪"))
        if not ver:
            return finish()

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(CDP)

            def find_page(patterns, timeout=90):
                """按 URL 片段挑窗口对应的页面（WebView2 可能同时暴露多个 target）。"""
                t0 = time.time()
                while time.time() - t0 < timeout:
                    for c in browser.contexts:
                        for pg in c.pages:
                            try:
                                u = pg.url or ""
                            except Exception:
                                continue
                            if any(x in u for x in patterns):
                                return pg
                    time.sleep(0.5)
                return None

            page = find_page(["splash.html", "desktop.html", "app.html", "login.html"], timeout=60)
            check("真实应用窗口里存在页面", page is not None, page.url if page else "无页面")
            if page is None:
                return finish()

            first_url = page.url or ""
            titles = native_windows(proc.pid)
            check("存在真实原生窗口（Win32 顶层窗口 + 标题）",
                  any("AI 数字人" in t for t in titles), " | ".join(titles)[:160])

            # 启动页证据：宿主自己起的 http 服务日志里有 GET /splash.html，
            # 宿主日志里也会有「页面加载完成：…/splash.html」。启动很快时页面可能已经跳走了，
            # 所以看宿主侧证据（窗口程序没有 stdout，所以优先读日志文件）。
            splash_ev, splash_src = False, ""
            t0 = time.time()
            while time.time() - t0 < 45:
                outs = []
                if out_log.is_file():
                    outs.append(out_log.read_text(encoding="utf-8", errors="ignore"))
                for lp in (LOG, host_log):
                    if lp.is_file():
                        outs.append(lp.read_text(encoding="utf-8", errors="ignore"))
                blob = "\n".join(outs)
                if "GET /splash.html" in blob or "splash.html" in blob.split("AI 数字人桌面版 v")[-1]:
                    splash_ev, splash_src = True, "宿主日志/http 日志出现 splash.html"
                    break
                if "splash.html" in (page.url or ""):
                    splash_ev, splash_src = True, "当前窗口 URL=%s" % page.url
                    break
                time.sleep(1)
            check("窗口先加载启动页 splash.html", splash_ev, splash_src or ("first_url=%s" % first_url))

            # 启动页 → 应用界面（未登录会先到登录页，这是服务端真实权限）
            # 桌面端自带入口是 /desktop.html；网页端入口 app.html 只应出现在"用浏览器打开"的场景
            def is_app_page(u):
                return ("desktop.html" in u) or ("app.html" in u) or ("login.html" in u)

            try:
                page.wait_for_url(is_app_page, timeout=WAIT_UI * 1000)
            except Exception as exc:
                body = ""
                try:
                    body = page.inner_text("body")[:200].replace("\n", " | ")
                except Exception:
                    pass
                check("启动页跳转到应用界面", False, "%s | now=%s | 启动页文字=%s" % (exc, page.url, body))
            landed = page.url or ""
            check("启动页已跳到应用/登录页", is_app_page(landed), landed)
            check("桌面窗口加载的是桌面端自带界面（不是网页端 app.html）",
                  "desktop.html" in landed or "login.html" in landed, landed)

            if "login.html" in landed:
                user, note = ensure_account()
                check("自检账号可用（登录前置）", "不可用" not in note, "%s：%s" % (user, note))
                page.fill("#loginUsername", user)
                page.fill("#loginPassword", PWD)
                page.click("#loginSubmit")
                try:
                    page.wait_for_url(lambda u: "desktop.html" in u, timeout=40000)
                    check("在应用窗口内用真实表单登录（登录页 → 桌面端界面）", True, page.url)
                except Exception as exc:
                    check("在应用窗口内用真实表单登录（登录页 → 桌面端界面）", False,
                          "%s | now=%s" % (exc, page.url))

            # 界面来源必须是桌面端自带的本地界面源（同源反向代理接口），不是 8010 的网页端
            ui_origin = page.evaluate("() => location.origin")
            check("界面来源不是网页端 8010", "8010" not in ui_origin, "origin=%s" % ui_origin)
            try:
                health = page.evaluate(
                    "async () => { const r = await fetch('/health', {credentials:'same-origin'});"
                    " const j = await r.json(); return {status: r.status, ok: j.status}; }")
            except Exception as exc:
                health = {"error": str(exc)}
            check("桌面端界面的接口请求由本地界面源同源代理到服务",
                  health.get("status") == 200 and health.get("ok") == "ok",
                  json.dumps(health, ensure_ascii=False))

            page.wait_for_timeout(3000)
            state = page.evaluate("""() => ({
                url: location.href,
                hasShell: !!document.getElementById('app'),
                nav: document.querySelectorAll('#navList .nav').length,
                grp: document.querySelectorAll('#navList .grp').length,
                tabs: document.querySelectorAll('#tabbar .tab[data-app]').length,
                mode: (document.getElementById('modeText')||{}).textContent || '',
                svc: (document.getElementById('stSvc')||{}).textContent || '',
                brand: getComputedStyle(document.documentElement).getPropertyValue('--brand').trim(),
                bootOverlay: !!document.getElementById('boot'),
                bridgeObj: !!(window.pywebview && window.pywebview.api),
                bridgeFn: !!(window.pywebview && window.pywebview.api &&
                             typeof window.pywebview.api.get_app_info === 'function'),
                fake: document.querySelectorAll('#iconLayer,#taskbar,#startMenu').length
            })""")
            check("应用窗口里渲染的是常规桌面软件界面",
                  state["hasShell"] and state["nav"] == 17 and state["grp"] == 4,
                  "nav=%s grp=%s" % (state["nav"], state["grp"]))
            check("启动遮罩已消失", not state["bootOverlay"], "bootOverlay=%s" % state["bootOverlay"])
            check("界面里没有假桌面残留", state["fake"] == 0, "count=%s" % state["fake"])
            check("主色仍是浅蓝 #38bdf8", state["brand"].lower() in ("#38bdf8", "rgb(56, 189, 248)"), state["brand"])
            check("状态栏服务在线", "在线" in state["svc"], state["svc"])
            check("默认已打开标签页", state["tabs"] >= 1, "tabs=%s" % state["tabs"])
            check("桌面桥接对象存在", state["bridgeObj"], "bridgeObj=%s" % state["bridgeObj"])
            check("桌面桥接方法可用（get_app_info 是函数）", state["bridgeFn"], "bridgeFn=%s" % state["bridgeFn"])

            shot_path = SHOTS / ("desktop-app-%s.png" % args.scenario)
            try:
                page.screenshot(path=str(shot_path))
                check("已保存真实应用窗口截图", shot_path.is_file(), str(shot_path))
            except Exception as exc:
                check("已保存真实应用窗口截图", False, str(exc))

            info = None
            for _ in range(15):
                try:
                    info = page.evaluate("async () => await window.pywebview.api.get_app_info()")
                    if info and info.get("version"):
                        break
                except Exception:
                    pass
                page.wait_for_timeout(1000)
            check("桥接 get_app_info() 返回宿主信息", bool(info and info.get("version")),
                  json.dumps({k: (info or {}).get(k) for k in
                              ("managed", "version", "project_root", "port", "webview")},
                             ensure_ascii=False)[:200] if info else "无返回")
            check("桥接报告项目目录正确", (info or {}).get("project_root", "").lower().endswith("ailive"),
                  str((info or {}).get("project_root")))
            expect_managed = (args.scenario == "managed")
            check("桥接的服务托管状态与场景一致", bool((info or {}).get("managed")) == expect_managed,
                  "managed=%s 期望=%s" % ((info or {}).get("managed"), expect_managed))

            # 端区分：桌面端必须保留桌面专属入口（网页端才隐藏它们）。
            chrome = page.evaluate("""() => ({
                title: document.title,
                brand: (document.getElementById('brandVer')||{}).textContent || ''
            })""")
            check("桌面端标题与品牌按端命名（桌面版）",
                  ("桌面版" in chrome["title"]) and ("桌面版" in chrome["brand"]),
                  "%s | %s" % (chrome["title"], chrome["brand"]))
            try:
                page.click("#menuService")
                page.wait_for_timeout(300)
                svc_items = page.locator("#popMenu .it .lbl").all_inner_texts()
                check("桌面端“服务”菜单保留桌面专属项（重启服务 / 打开日志文件夹）",
                      all(x in svc_items for x in ("重启数字人服务", "打开日志文件夹")), "|".join(svc_items))
                page.keyboard.press("Escape")
                page.wait_for_timeout(200)
                page.click("#menuFile")
                page.wait_for_timeout(300)
                file_items = page.locator("#popMenu .it .lbl").all_inner_texts()
                check("桌面端“文件”菜单保留“退出应用”", "退出应用" in file_items, "|".join(file_items))
                page.keyboard.press("Escape")
                page.wait_for_timeout(200)
                page.keyboard.press("F1")
                page.wait_for_timeout(1000)
                about_txt = page.inner_text("#panes .pane.on")
                check("桌面端关于面板保留桌面专属信息（项目根目录 / 服务托管 / 打开项目文件夹）",
                      all(k in about_txt for k in ("项目根目录", "日志目录", "服务托管", "界面内核"))
                      and page.locator("#abFolder").count() == 1,
                      about_txt[:160].replace("\n", " "))
            except Exception as exc:
                check("桌面端桌面专属入口检查", False, str(exc)[:200])

            # 应用窗口里再开一个标签（真实页面 iframe）
            try:
                page.click('.nav[data-app="console"]')
                page.wait_for_timeout(3500)
                tabs2 = page.evaluate("() => document.querySelectorAll('#tabbar .tab[data-app]').length")
                check("应用窗口内可多标签打开真实页面", tabs2 >= 2, "tabs=%s" % tabs2)
            except Exception as exc:
                check("应用窗口内可多标签打开真实页面", False, str(exc)[:200])

            # 用桥接接口退出应用（同时验证 exit_app 这条链路）。
            # 页面会被销毁，evaluate 报 "Target ... has been closed" 属于预期结果。
            exit_err = ""
            try:
                page.evaluate("() => window.pywebview.api.exit_app()")
            except Exception as exc:
                exit_err = str(exc)
            check("通过桥接调用 exit_app",
                  (not exit_err) or ("has been closed" in exit_err),
                  exit_err[:160] if exit_err else "调用成功")
            t0 = time.time()
            while time.time() - t0 < WAIT_EXIT and proc.poll() is None:
                time.sleep(1)
            exited_by_bridge = proc.poll() is not None
            check("应用退出（exit_app 生效）", exited_by_bridge, "returncode=%s" % proc.poll())
    except Exception as exc:
        check("用例执行过程未抛异常", False, repr(exc)[:300])
    finally:
        if proc.poll() is None:
            kill_tree(proc.pid)
            time.sleep(2)
        try:
            out_fh.close()
        except Exception:
            pass

    rc = proc.poll()
    check("宿主进程退出码为 0", rc == 0, "returncode=%s" % rc)

    out = out_log.read_text(encoding="utf-8", errors="ignore") if out_log.is_file() else ""
    log_new = ""
    for path, before in ((LOG, log_before), (host_log, host_before)):
        if path.is_file():
            now = path.read_text(encoding="utf-8", errors="ignore")
            if len(now) > len(before):
                log_new += now[len(before):]
    if not log_new:
        log_new = out
    check("宿主日志记录了启动与界面探查", ("[自检] 界面探查" in log_new) or ("[窗口] 页面加载完成" in log_new),
          " | ".join([x for x in log_new.splitlines() if "界面探查" in x or "页面加载完成" in x][:2])[:220])
    check("宿主日志记录 js_api 可用性检查通过", "可用性" in log_new and "通过" in log_new,
          " | ".join([x for x in log_new.splitlines() if "[桥接]" in x][-2:])[:220])
    check("宿主日志记录退出码 0", "退出，退出码 0" in log_new,
          " | ".join([x for x in log_new.splitlines() if "退出" in x][-3:])[:220])
    check("日志里能看到服务托管决策", ("服务由本应用启动" in log_new) or ("服务不是本应用启动的" in log_new),
          " | ".join([x for x in log_new.splitlines() if "[退出]" in x][:2])[:200])

    time.sleep(3)
    if args.scenario == "managed":
        check("退出后：应用自己启动的服务已被停掉", port_free(), "port_free=%s" % port_free())
    else:
        check("退出后：外部服务仍在运行（没有被误杀）", http_ok(HEALTH), HEALTH)

    profile = Path(os.environ.get("LOCALAPPDATA", "")) / "AILiveDesktop" / "webview"
    check("WebView2 用户数据目录已建立（登录态/设置可持久化）",
          profile.is_dir() and any(profile.iterdir()), str(profile))
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