# -*- coding: utf-8 -*-
"""桌面应用界面（web/app.html）真实浏览器验收。

app.html = 常规桌面软件界面（左侧导航 + 顶部菜单/工具栏 + 多标签内容区 + 状态栏），
业务面板与接口层复用 desktop.html 中已验证的代码。本脚本只验证「外壳」这一层：
  标签系统（打开/切换/关闭/恢复）、真实页面 iframe、真实接口调用、设置与关于面板、
  菜单与快捷键、通知中心、窄屏抽屉、以及“页面里没有假桌面残留”。

自检账号以 e2e 开头，可用 tools/smoke/_cleanup_test_data.py --apply 清理。
"""
import json
import os
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8010"
SHOTS = Path(r"E:\AiLive\_qa\app-v1")
SHOTS.mkdir(parents=True, exist_ok=True)
USER = "e2e_app_qa"        # 固定自检账号：注册接口有频率限制（429），重复跑必须复用登录
FALLBACK_USERS = ["e2e_desktop_qa", "e2e_desktop_1791710695", "e2e_desktop_1791710671", "e2e_desktop_1791710580"]
PWD = "DeskTop12345"

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), str(detail)[:220]))
    print(("PASS " if ok else "FAIL ") + name + ("  | " + str(detail)[:220] if detail else ""))


def shot(page, name):
    try:
        page.screenshot(path=str(SHOTS / (name + ".png")))
    except Exception as exc:
        print("shot fail", name, exc)


def register(ctx):
    """先尝试注册；已存在或被限流（429）则退回登录已有自检账号。"""
    global USER
    r = ctx.request.post(BASE + "/api/auth/register",
                         data={"username": USER, "password": PWD, "display_name": "应用外壳自检"})
    body = r.json()
    if body.get("code") == 0:
        return body
    for name in [USER] + FALLBACK_USERS:
        r = ctx.request.post(BASE + "/api/auth/login", data={"username": name, "password": PWD})
        body = r.json()
        if body.get("code") == 0:
            USER = name
            body["_note"] = "reused existing test account (register was rate-limited)"
            return body
    return body


def launch(p):
    """本机已有 chromium-1247 与 Edge；python playwright 需要的 1243 headless shell 未装，
    因此按“已存在的 chromium → Edge → 默认”顺序挑一个可用浏览器。"""
    local = os.environ.get("LOCALAPPDATA", "")
    cand = [
        os.path.join(local, r"ms-playwright\chromium-1247\chrome-win64\chrome.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    for path in cand:
        if os.path.exists(path):
            try:
                b = p.chromium.launch(headless=True, executable_path=path)
                print("using browser:", path)
                return b
            except Exception as exc:
                print("launch failed", path, exc)
    b = p.chromium.launch(headless=True)
    print("using bundled chromium")
    return b


def wait_boot(page):
    page.wait_for_function("() => !document.getElementById('boot')", timeout=30000)
    page.wait_for_timeout(500)


def open_module(page, app_id):
    page.click('.nav[data-app="%s"]' % app_id)
    page.wait_for_timeout(350)


def tab_count(page):
    return page.locator("#tabbar .tab[data-app]").count()


def main():
    with sync_playwright() as p:
        browser = launch(p)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
        page = ctx.new_page()
        errors = []
        api_calls = []
        page.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))

        def on_console(m):
            if m.type != "error":
                return
            loc = m.location or {}
            url = loc.get("url", "") if isinstance(loc, dict) else str(loc)
            if url == "" or url.endswith("/app.html") or "app.html?" in url:
                errors.append("console.error: %s @ %s" % (m.text, url))
        page.on("console", on_console)

        def on_response(resp):
            if "/api/" in resp.url:
                api_calls.append((resp.url.split("/api/")[-1].split("?")[0], resp.status))
        page.on("response", on_response)

        # ── 0. 未登录访问 /app.html：必须被服务端挡到登录页（真实权限，不是前端假装）──
        ctx2 = browser.new_context(viewport={"width": 1280, "height": 800}, locale="zh-CN")
        p2 = ctx2.new_page()
        p2.goto(BASE + "/app.html", wait_until="domcontentloaded")
        p2.wait_for_timeout(400)
        check("未登录访问 /app.html 被服务端跳转到登录页", "/login.html" in p2.url, p2.url)
        check("登录页带回跳地址 next=/app.html", "next=%2Fapp.html" in p2.url or "next=/app.html" in p2.url, p2.url)
        ctx2.close()

        reg = register(ctx)
        check("注册/登录自检账号", reg.get("code") == 0, json.dumps(reg, ensure_ascii=False)[:160])

        # ── 1. 打开应用：应停在 /app.html（已登录），不是登录页 ──────────────
        page.goto(BASE + "/app.html", wait_until="domcontentloaded")
        wait_boot(page)
        check("已登录并停在 /app.html", page.url.rstrip("/").endswith("/app.html"), page.url)
        check("启动引导层已消失", page.locator("#boot").count() == 0)
        check("应用外壳已显示", page.locator("#app").is_visible())

        # ── 2. 形态检查：这是常规桌面软件界面，不是“假桌面” ─────────────────
        fake = {sel: page.locator(sel).count() for sel in ("#iconLayer", "#taskbar", "#startMenu", "#desk", "#wallpaper")}
        check("页面无假桌面残留（图标层/任务栏/开始菜单都没有）", all(v == 0 for v in fake.values()), json.dumps(fake))
        check("运行模式识别为连接本地服务", "服务在线" in page.inner_text("#modeText"), page.inner_text("#modeText"))
        brand = page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--brand').trim()")
        check("主色为浅蓝 #38bdf8", brand.lower() in ("#38bdf8", "rgb(56, 189, 248)"), brand)
        brandver = page.inner_text("#brandVer").strip()
        check("网页端品牌小字显示“网页端”（不自称桌面版）", brandver == "网页端", brandver)
        check("网页端页面标题按端命名（不含“桌面版”）",
              "桌面版" not in page.title() and "网页端" in page.title(), page.title())
        tool_title = page.get_attribute("#btnExternal", "title") or ""
        check("工具栏按钮文案按端区分（新标签页打开，不是系统浏览器）", "新标签页" in tool_title, tool_title)
        menus = page.locator("#menubar .mi").all_inner_texts()
        check("顶部菜单栏 = 文件/视图/服务/帮助", menus == ["文件", "视图", "服务", "帮助"], "|".join(menus))
        navs = page.locator("#navList .nav").count()
        groups = page.locator("#navList .grp").count()
        check("左侧导航 17 个模块 / 4 个分组", navs == 17 and groups == 4, "nav=%d grp=%d" % (navs, groups))
        shot(page, "01-app-default")

        # ── 3. 默认标签（控制中心，原生面板 + 真实接口）──────────────────────
        check("默认打开 1 个标签：控制中心", tab_count(page) == 1 and page.locator('#tabbar .tab.on').inner_text().startswith("控制中心"),
              page.locator('#tabbar .tab.on').inner_text())
        page.wait_for_selector("#ctStats .stat", timeout=12000)
        stats = page.locator("#ctStats .stat").count()
        check("控制中心渲染真实状态卡片（/api/live/status）", stats >= 6, "stat=%d" % stats)
        check("状态栏显示服务在线与标签数", "在线" in page.inner_text("#stSvc") and page.inner_text("#stTabs") == "1",
              page.inner_text("#stSvc") + " / " + page.inner_text("#stTabs"))

        # ── 4. 真实页面标签（iframe 内嵌业务页）─────────────────────────────
        open_module(page, "console")
        check("打开第二个标签（直播工作台）", tab_count(page) == 2, "tabs=%d" % tab_count(page))
        frame = page.frame_locator('iframe[title="直播工作台"]')
        check("工作台标签内嵌真实页面 iframe", page.locator('iframe[title="直播工作台"]').count() == 1)
        src = page.get_attribute('iframe[title="直播工作台"]', "src")
        check("iframe 指向真实业务页面", "/live_console.html" in (src or ""), str(src))
        try:
            frame.locator("body").wait_for(timeout=12000)
            inner_has = frame.locator("text=直播").count() > 0
        except Exception:
            inner_has = False
        check("iframe 内业务页确实渲染（不是空文档）", inner_has)
        check("未出现“需要登录/专业版”被拦截告警", page.locator(".wm-alert").count() == 0,
              page.locator(".wm-alert").all_inner_texts()[:1])

        # 免费账号打开「用户与会员管理」：服务端会拦截，外壳要如实提示（不替服务端放行）
        open_module(page, "users")
        gate_ok = True
        try:
            page.wait_for_selector("#panes .pane.on .wm-alert", timeout=9000)
        except Exception:
            gate_ok = False
        gate = page.locator("#panes .pane.on .wm-alert")
        gate_txt = gate.first.inner_text() if gate.count() else ""
        gated_frames = [f.url for f in page.frames if "/forbidden.html" in f.url or "/upgrade.html" in f.url]
        check("受限模块被服务端拦截且外壳如实提示",
              (gate_ok and gate.count() >= 1 and ("管理员" in gate_txt or "专业版" in gate_txt or "登录" in gate_txt)) or bool(gated_frames),
              (gate_txt[:120].replace("\n", " ") or "无告警") + " | " + " ".join(gated_frames)[:120])
        page.click('#tabbar .tab[data-app="users"] .x')
        page.wait_for_timeout(400)

        open_module(page, "logs")
        check("打开第三个标签（日志与诊断）", tab_count(page) == 3, "tabs=%d" % tab_count(page))
        page.wait_for_selector("#dgHealth", timeout=10000)
        page.wait_for_timeout(900)
        check("诊断面板读取真实 /health", "ok" in page.inner_text("#dgHealth").lower() or "服务" in page.inner_text("#dgHealth"),
              page.inner_text("#dgHealth")[:80].replace("\n", " "))
        shot(page, "02-app-logs")

        # ── 5. 标签切换：面板保留在 DOM（直播画面不会因切标签断开）──────────
        page.click('#tabbar .tab[data-app="control"]')
        page.wait_for_timeout(300)
        active = page.locator("#panes .pane.on").get_attribute("data-app")
        check("切回控制中心标签", active == "control", str(active))
        check("其它标签仍在 DOM 中（iframe 未被销毁）", page.locator('iframe[title="直播工作台"]').count() == 1)
        open_module(page, "sop")
        page.wait_for_selector("#sopBody", timeout=10000)
        check("SOP 面板可开（第 4 个标签）", tab_count(page) == 4, "tabs=%d" % tab_count(page))

        # ── 6. 真实接口：模拟评论（外壳直连 /api/live/simulate）────────────
        page.click('#tabbar .tab[data-app="control"]')
        page.wait_for_timeout(300)
        page.fill("#ctSimText", "这个多少钱？发什么快递？")
        api_calls.clear()
        page.click("#ctSimRun")
        page.wait_for_timeout(1800)
        sim = [c for c in api_calls if c[0] == "live/simulate"]
        check("模拟评论走真实接口 /api/live/simulate", len(sim) >= 1 and sim[0][1] == 200, json.dumps(sim))
        check("模拟结果有回显（成功或服务端错误说明）", len(page.inner_text("#ctSimOut")) > 0,
              page.inner_text("#ctSimOut")[:100].replace("\n", " "))

        # ── 7. 通知中心 / 刷新提示 ─────────────────────────────────────────
        page.click("#notifyBtn")
        page.wait_for_timeout(300)
        check("通知中心可打开且有通知", page.locator("#notifyPanel.open").count() == 1 and page.locator("#npList .np-item").count() >= 1,
              "items=%d" % page.locator("#npList .np-item").count())
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        page.click("#btnRefresh")
        page.wait_for_timeout(1200)
        check("工具栏刷新有提示且状态仍在线", page.locator("#toasts .toast").count() >= 1 and "在线" in page.inner_text("#stSvc"),
              "toasts=%d" % page.locator("#toasts .toast").count())

        # ── 8. 新写的设置面板：主题/主色即时生效 ───────────────────────────
        page.click("#btnSettings")
        page.wait_for_selector("#setTabs", timeout=8000)
        check("设置面板打开且有 4 个分类", page.locator("#setTabs button").count() == 4,
              "|".join(page.locator("#setTabs button").all_inner_texts()))
        page.click('#setTabs button[data-t="look"]')
        page.wait_for_timeout(200)
        page.click('.seg[data-seg="theme"] button[data-v="dark"]')
        page.wait_for_timeout(400)
        theme = page.evaluate("document.documentElement.dataset.theme")
        check("切深色主题即时生效", theme == "dark", str(theme))
        page.click('.swatches[data-sw2="accent"] .sw[data-v="cyan"]')
        page.wait_for_timeout(300)
        brand2 = page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--brand').trim()")
        check("主色可切换（青色）", brand2.lower() in ("#22d3ee", "rgb(34, 211, 238)"), brand2)
        shot(page, "03-app-settings-dark")
        page.click('.seg[data-seg="theme"] button[data-v="light"]')
        page.click('.swatches[data-sw2="accent"] .sw[data-v="sky"]')
        page.wait_for_timeout(300)
        check("恢复浅色 + 默认浅蓝主色", page.evaluate("document.documentElement.dataset.theme") == "light" and
              page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--brand').trim()").lower() in ("#38bdf8", "rgb(56, 189, 248)"))

        # ── 9. 关于面板（版本/账号/环境）──────────────────────────────────
        page.keyboard.press("F1")
        page.wait_for_timeout(600)
        about = page.locator("#panes .pane.on").get_attribute("data-app")
        check("F1 打开关于面板", about == "about", str(about))
        body = page.inner_text("#panes .pane.on")
        check("关于面板显示版本与运行模式",
              bool(re.search(r"\d+\.\d+\.\d+", body)) and ("连接真实服务" in body or "离线演示" in body),
              (re.search(r"\d+\.\d+\.\d+", body).group(0) if re.search(r"\d+\.\d+\.\d+", body) else "无版本号"))
        check("关于面板显示账号与套餐", USER in body or "管理员" in body or "普通用户" in body)
        check("关于面板显示服务地址", "127.0.0.1:8010" in body or "服务地址" in body)
        shot(page, "04-app-about")

        # ── 9.5 端区分：网页端里不该出现任何桌面专属入口 ───────────────────
        #  桌面端（AILiveDesktop）与网页端共用同一份 app.html；桌面专属能力
        #  （打开文件夹 / 重启服务 / 服务托管 / 退出应用 / 项目根目录…）在纯浏览器里
        #  必须“不渲染”，而不是“渲染成禁用状态”。
        check("关于面板按端命名（网页端，不是“桌面版”）",
              "桌面版" not in body and "网页端" in body, body[:80].replace("\n", " "))
        check("关于面板没有桌面专属行（项目根目录/日志目录/构建类型/服务托管/界面内核）",
              all(k not in body for k in ("项目根目录", "日志目录", "构建类型", "服务托管", "界面内核")),
              body[:200].replace("\n", " "))
        check("关于面板如实说明运行端", "网页端（浏览器）" in body, "运行端")
        check("关于面板没有“打开项目文件夹”按钮（网页端没有本机文件夹入口）",
              page.locator("#abFolder").count() == 0)
        page.click("#abKeys")
        page.wait_for_selector("#modalLayer.open", timeout=5000)
        keys_txt = page.inner_text("#modalLayer")
        check("快捷键说明里没有“Ctrl + Q 退出应用”（桌面专属）",
              "Ctrl + Q" not in keys_txt and "退出应用" not in keys_txt, keys_txt[:120].replace("\n", " "))
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)

        page.click("#menuFile")
        page.wait_for_timeout(250)
        file_items = page.locator("#popMenu .it .lbl").all_inner_texts()
        check("“文件”菜单里没有“退出应用”（桌面专属）", "退出应用" not in file_items, "|".join(file_items))
        check("“文件”菜单保留网页端可用项（打开模块 / 关闭标签）",
              any("打开控制中心" in x for x in file_items) and any("关闭当前标签" in x for x in file_items),
              "|".join(file_items))
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)

        page.click("#menuService")
        page.wait_for_timeout(250)
        svc_items = page.locator("#popMenu .it .lbl").all_inner_texts()
        check("“服务”菜单里没有重启服务/打开日志文件夹（桌面专属）",
              all(x not in svc_items for x in ("重启数字人服务", "打开日志文件夹")), "|".join(svc_items))
        check("“服务”菜单保留网页端可用项（重新读取运行状态 / 日志与诊断）",
              any("重新读取" in x for x in svc_items) and any("日志与诊断" in x for x in svc_items),
              "|".join(svc_items))
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)

        page.click("#userChip")
        page.wait_for_timeout(250)
        chip_items = page.locator("#popMenu .it .lbl").all_inner_texts()
        check("账号菜单里没有“打开项目文件夹”（桌面专属）",
              "打开项目文件夹" not in chip_items, "|".join(chip_items))
        check("账号菜单“打开工作台”按端改文案（新标签页打开）",
              any("新标签页打开工作台" in x for x in chip_items), "|".join(chip_items))
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)

        page.click("#btnSettings")
        page.wait_for_selector("#setTabs", timeout=8000)
        page.click('#setTabs button[data-t="svc"]')
        page.wait_for_timeout(300)
        svc_body = page.inner_text("#setBody")
        check("设置-服务里没有“服务托管 / 界面内核”等桌面专属行",
              "服务托管" not in svc_body and "界面内核" not in svc_body, svc_body[:120].replace("\n", " "))
        check("设置-服务里没有重启服务/日志文件夹/项目文件夹按钮",
              page.locator("#svcRestart, #svcLogDir, #svcPkgDir").count() == 0,
              "btns=%d" % page.locator("#svcRestart, #svcLogDir, #svcPkgDir").count())
        check("设置-服务保留网页端可用信息（服务地址 / 端口 / 运行模式）",
              all(k in svc_body for k in ("服务地址", "端口", "运行模式")), svc_body[:150].replace("\n", " "))

        # ── 10. 菜单 / 快捷键 ─────────────────────────────────────────────
        page.click("#menuView")
        page.wait_for_timeout(250)
        check("“视图”菜单可弹出", page.locator("#popMenu.open").count() == 1 and
              page.locator("#popMenu .it").count() >= 4, "items=%d" % page.locator("#popMenu .it").count())
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        check("Esc 关闭菜单", page.locator("#popMenu.open").count() == 0)
        page.keyboard.press("Control+k")
        page.wait_for_timeout(250)
        focused = page.evaluate("document.activeElement && document.activeElement.id")
        check("Ctrl+K 聚焦左侧搜索框", focused == "navSearch", str(focused))
        page.fill("#navSearch", "商品库")
        page.wait_for_timeout(300)
        filtered = page.locator("#navList .nav").count()
        names = page.locator("#navList .nav").all_inner_texts()
        check("导航搜索可过滤（商品库 → 命中商品库且变少）",
              filtered >= 1 and filtered < 17 and any("商品库" in n for n in names),
              "nav=%d %s" % (filtered, "|".join(n.replace("\n", " ") for n in names)))
        page.fill("#navSearch", "")
        page.wait_for_timeout(250)
        check("清空搜索恢复 17 项", page.locator("#navList .nav").count() == 17,
              "nav=%d" % page.locator("#navList .nav").count())

        before = tab_count(page)
        page.keyboard.press("Control+w")
        page.wait_for_timeout(400)
        check("Ctrl+W 关闭当前标签", tab_count(page) == before - 1, "%d → %d" % (before, tab_count(page)))

        # ── 11. 会话持久化：重载后恢复上次标签 ─────────────────────────────
        keep = tab_count(page)
        page.wait_for_timeout(700)   # 等设置写入（写盘有 260ms 防抖）
        page.reload(wait_until="domcontentloaded")
        wait_boot(page)
        check("重载后恢复上次打开的标签", tab_count(page) == keep, "keep=%d now=%d" % (keep, tab_count(page)))
        check("重载后仍是已登录的真实服务模式", "服务在线" in page.inner_text("#modeText"), page.inner_text("#modeText"))

        # ── 12. 窄屏：导航变抽屉 ──────────────────────────────────────────
        page.set_viewport_size({"width": 760, "height": 900})
        page.wait_for_timeout(500)
        toggle_visible = page.locator("#navToggle").is_visible()
        side_box = page.locator("#side").bounding_box() or {"x": 0}
        check("窄屏下导航默认收起", toggle_visible and side_box["x"] < 0, "x=%s" % side_box.get("x"))
        page.click("#navToggle")
        page.wait_for_timeout(400)
        side2 = page.locator("#side").bounding_box() or {"x": -1}
        check("点击汉堡按钮抽屉滑出", page.evaluate("document.documentElement.dataset.nav") == "open" and side2["x"] >= 0,
              "nav=%s x=%s" % (page.evaluate("document.documentElement.dataset.nav"), side2.get("x")))
        shot(page, "05-app-narrow")
        page.set_viewport_size({"width": 1440, "height": 900})
        page.wait_for_timeout(300)

        # ── 12.5 入口统一（v1.3.2）：裸 / 与旧 /desktop.html 都落到统一外壳 ──
        page.goto(BASE + "/", wait_until="domcontentloaded")
        wait_boot(page)
        check("裸地址 / 落到统一外壳 app.html（不再直接进旧工作台）",
              page.url.rstrip("/").endswith("/app.html"), page.url)
        page.goto(BASE + "/desktop.html", wait_until="domcontentloaded")
        wait_boot(page)
        check("旧链接 /desktop.html 落到统一外壳 app.html（旧仿桌面页已退役）",
              page.url.rstrip("/").endswith("/app.html"), page.url)
        fake3 = {sel: page.locator(sel).count() for sel in ("#iconLayer", "#taskbar", "#startMenu", "#wallpaper")}
        check("旧链接落到的页面不是那套假桌面", all(v == 0 for v in fake3.values()), json.dumps(fake3))
        check("统一外壳在旧链接下同样按端显示“网页端”",
              page.inner_text("#brandVer").strip() == "网页端", page.inner_text("#brandVer").strip())
        ctx3 = browser.new_context(viewport={"width": 1280, "height": 800}, locale="zh-CN")
        p3 = ctx3.new_page()
        p3.goto(BASE + "/desktop.html", wait_until="domcontentloaded")
        p3.wait_for_timeout(500)
        check("未登录访问旧链接先被服务端挡到登录页", "/login.html" in p3.url, p3.url)
        ctx3.close()
        page.goto(BASE + "/app.html", wait_until="domcontentloaded")
        wait_boot(page)

        # ── 13. 外壳自身无报错 ────────────────────────────────────────────
        check("主框架无 console.error / 页面异常", len(errors) == 0, " | ".join(errors[:3]))
        page.wait_for_timeout(300)
        shot(page, "06-app-final")

        browser.close()

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