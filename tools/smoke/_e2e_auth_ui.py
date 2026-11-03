"""浏览器端到端验收：对真实运行中的服务用 Chromium 跑完整登录 / 分级 / 兑换链路。

前置：服务已在 http://127.0.0.1:8010 运行（一键启动AI数字人.cmd）。
运行：
    E:\\AiLive\\env\\python.exe tools\\smoke\\_e2e_auth_ui.py

覆盖：
    未登录跳转 → 管理员登录（全功能解锁 + 初始密码提醒）→ 后台生成激活码
    → 退出 → 自助注册免费用户 → 免费版功能加锁与提示条 → 免费功能可用
    → 点击锁定功能跳升级页 → 无效码被拒 → 有效码解锁 → 回工作台锁定消失
    全程收集浏览器控制台报错与未捕获异常。
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8010"
SHOTS = Path(r"E:\AiLive\_qa\auth_e2e")
# 管理员口令从环境变量取，不写死在脚本里。改过密码后这样跑：
#     $env:AILIVE_ADMIN_PASSWORD = "你的密码"
ADMIN_USER = os.environ.get("AILIVE_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("AILIVE_ADMIN_PASSWORD", "admin123456")


def find_chromium() -> str | None:
    """本机 playwright 版本与已下载浏览器版本不一致时，直接指定可执行文件。"""
    root = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    candidates = sorted(root.glob("chromium_headless_shell-*/chrome-headless-shell-win64/chrome-headless-shell.exe"))
    candidates += sorted(root.glob("chromium-*/chrome-win/chrome.exe"))
    return str(candidates[-1]) if candidates else None
FREE_USER = "e2e" + time.strftime("%m%d%H%M%S")
FREE_PASSWORD = "e2ePass2026"
NOISE = ("failed to load resource", "net::err", "favicon", "cdn.jsdelivr.net",
         "bootstrap-icons", "status of 404", "status of 401", "pay_qr")

PASSED = 0
FAILED: list[str] = []
CONSOLE_ERRORS: list[str] = []
PAGE_ERRORS: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> bool:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  [OK]   {label}")
    else:
        FAILED.append(f"{label} :: {detail}")
        print(f"  [FAIL] {label} :: {detail}")
    return bool(condition)


def text_of(page, selector: str) -> str:
    try:
        return page.locator(selector).first.inner_text().strip()
    except Exception:
        return ""


def classes(page, selector: str) -> str:
    try:
        return page.locator(selector).first.get_attribute("class") or ""
    except Exception:
        return ""


def dom_text(page, selector: str) -> str:
    """读 textContent（不看可见性）——inner_text 对隐藏元素返回空串，会误导判断。"""
    try:
        return page.evaluate(
            "(sel) => { const el = document.querySelector(sel); return el ? el.textContent.trim() : ''; }",
            selector,
        )
    except Exception:
        return ""


def computed_display(page, selector: str) -> str:
    """读元素自身的 computed display。

    比 is_visible() 更精准：元素若躺在未激活的标签页里，is_visible() 对谁都返回 False，
    分不清是「被权限藏了」还是「只是页签没切过去」。
    """
    try:
        return page.evaluate(
            "(sel) => { const el = document.querySelector(sel); "
            "return el ? getComputedStyle(el).display : 'missing'; }",
            selector,
        )
    except Exception:
        return "error"


def wait_for_url(page, needle: str, timeout: int = 15000) -> str:
    """服务端 302 经过 Service Worker 转发时，page.url 会晚一拍才更新，这里轮询等待。"""
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        if needle in page.url:
            return page.url
        page.wait_for_timeout(200)
    return page.url


def wait_for_text(page, selector: str, needle: str, timeout: int = 20000) -> str:
    """等到元素文本包含关键字才返回，避免用固定 sleep 赌异步渲染。"""
    deadline = time.time() + timeout / 1000
    text = ""
    while time.time() < deadline:
        text = dom_text(page, selector)
        if needle in text:
            return text
        page.wait_for_timeout(250)
    return text


def layout_check(page, label: str, selectors: list) -> None:
    """粗粒度布局自检：横向溢出 / 关键控件缺失 / 控件尺寸为 0（看不见）。"""
    info = page.evaluate("""(sels) => {
        const out = {overflow: document.documentElement.scrollWidth > window.innerWidth + 2,
                     overflowBy: document.documentElement.scrollWidth - window.innerWidth,
                     missing: [], tiny: []};
        for (const s of sels) {
            const el = document.querySelector(s);
            if (!el) { out.missing.push(s); continue; }
            const r = el.getBoundingClientRect();
            if (r.width < 2 || r.height < 2) out.tiny.push(s);
        }
        return out;
    }""", selectors)
    check(f"{label}：无横向溢出", not info["overflow"], f"超出 {info['overflowBy']}px")
    check(f"{label}：关键控件都在", not info["missing"], "、".join(info["missing"]))
    check(f"{label}：关键控件可见", not info["tiny"], "、".join(info["tiny"]))


def dismiss_password_modal(page) -> None:
    """初始密码提醒弹窗若出现则关闭，避免挡住后台操作。"""
    try:
        modal = page.locator("#authPasswordModal")
        if modal.count():
            page.locator("#authPasswordModal [data-modal-close]").first.click(timeout=3000)
            page.wait_for_timeout(300)
    except Exception:
        pass


def collect(page, tag: str) -> None:
    def on_console(message):
        if message.type != "error":
            return
        text = f"[{tag}] {message.text}"
        if any(noise in text.lower() for noise in NOISE):
            return
        CONSOLE_ERRORS.append(text)

    def on_page_error(error):
        PAGE_ERRORS.append(f"[{tag}] {error}")

    page.on("console", on_console)
    page.on("pageerror", on_page_error)


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    print(f"\n目标服务：{BASE}\n截图目录：{SHOTS}\n免费用户：{FREE_USER}\n")

    with sync_playwright() as playwright:
        executable = find_chromium()
        if executable:
            print(f"浏览器：{executable}")
            browser = playwright.chromium.launch(headless=True, executable_path=executable)
        else:
            browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000},
                                      locale="zh-CN")
        page = context.new_page()
        page.set_default_timeout(25000)
        collect(page, "guest")

        # ── 1. 未登录拦截 ─────────────────────────────────────
        print("1. 未登录访问工作台")
        page.goto(f"{BASE}/live_console.html", wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")
        check("未登录被跳转到登录页", "/login.html" in page.url, page.url)
        check("跳转带回跳参数 next", "next=" in page.url, page.url)
        check("登录页渲染了套餐对比", "专业版" in text_of(page, "#planList"),
              text_of(page, "#planList")[:60])
        guest_competition = computed_display(page, 'a[data-hide-without="competition.view"]')
        check("未登录访客看不到「参赛项目」入口", guest_competition == 'none', guest_competition)
        layout_check(page, "登录页",
                     ["#loginUsername", "#loginPassword", "#loginSubmit", "#tabRegister", "#planList"])
        page.screenshot(path=str(SHOTS / "01_login.png"), full_page=True)

        # ── 2. 管理员登录 ─────────────────────────────────────
        print("\n2. 管理员登录与全功能")
        page.fill("#loginUsername", ADMIN_USER)
        page.fill("#loginPassword", ADMIN_PASSWORD)
        page.click("#loginSubmit")
        page.wait_for_url("**/live_console.html*", timeout=25000)
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1500)
        bar = wait_for_text(page, "#authBar", "管理员")
        check("进入工作台", "live_console.html" in page.url, page.url)
        check("顶部显示管理员身份", "管理员" in bar, bar[:80])
        # 初始密码是否已改由服务端说了算：没改就该提醒，改过就不该再提醒
        must_change = page.evaluate(
            "() => !!(window.Auth && Auth.state.must_change_password)")
        banner = dom_text(page, "#authBanner")
        modal = page.locator("#authPasswordModal").count()
        if must_change:
            check("未改初始密码：页面给出强提醒", modal == 1 or "初始管理员密码" in banner,
                  banner[:80])
        else:
            check("已改过密码：不再提示初始密码", modal == 0 and "初始管理员密码" not in banner,
                  f"modal={modal} banner={banner[:60]}")
        if modal:
            check("修改密码弹窗可关闭", True)
            page.locator("#authPasswordModal [data-modal-close]").first.click()
            page.wait_for_timeout(400)
        check("管理员一键开播未被加锁",
              "feature-locked" not in classes(page, "#startLive"), classes(page, "#startLive"))
        check("管理员自动话术未加锁",
              "feature-locked" not in classes(page, "#startAuto"), classes(page, "#startAuto"))
        check("管理员商品库无遮罩",
              page.locator('[data-feature-lock="product.write"] .feature-lock-overlay').count() == 0)
        check("管理员看到完整导出入口",
              "feature-locked" not in classes(page, 'a[data-feature="export"]'))
        competition_nav = page.locator('a[data-hide-without="competition.view"]')
        check("管理员可见「参赛项目」导航入口", competition_nav.first.is_visible(),
              f"count={competition_nav.count()}")
        page.screenshot(path=str(SHOTS / "02_admin_console.png"), full_page=True)
        # 参赛材料卡片在「接入与帮助」页签里，先切过去再判断（否则看不见不代表被权限藏了）
        page.click('[data-lc-tab="access"]')
        page.wait_for_timeout(400)
        admin_card_display = computed_display(page, 'section[data-hide-without="competition.view"]')
        check("管理员切到「接入与帮助」能看到参赛材料卡片",
              admin_card_display == 'block', admin_card_display)
        page.click('[data-lc-tab="check"]')
        page.wait_for_timeout(200)

        # ── 3. 管理员后台生成激活码 ────────────────────────────
        print("\n3. 用户管理后台")
        page.unroute_all(behavior="ignoreErrors")
        page.remove_listener("console", lambda message: None) if False else None
        page.goto(f"{BASE}/admin_users.html", wait_until="domcontentloaded")
        page.wait_for_selector("#statCards .stat-card", timeout=25000)
        page.wait_for_timeout(600)
        dismiss_password_modal(page)
        check("概览卡片渲染", page.locator("#statCards .stat-card").count() == 8,
              str(page.locator("#statCards .stat-card").count()))
        check("账号列表包含 admin", "admin" in text_of(page, "#userRows"),
              text_of(page, "#userRows")[:60])
        check("功能对照表渲染", page.locator("#featureRows tr").count() > 10,
              str(page.locator("#featureRows tr").count()))
        layout_check(page, "用户管理页",
                     ["#statCards", "#userRows", "#createCodes", "#codeDays", "#cfgEnabled",
                      "#cfgContact", "#cfgSingleSession"])
        check("登录设置里有「同一账号只允许一处登录」开关且默认开启",
              page.locator("#cfgSingleSession").is_checked(),
              f"checked={page.locator('#cfgSingleSession').is_checked()}")

        page.fill("#codeDays", "30")
        page.fill("#codeCount", "1")
        page.fill("#codeNote", "E2E 自动生成")
        page.click("#createCodes")
        page.wait_for_selector("#newCodes .code-text", timeout=25000)
        page.wait_for_timeout(600)
        code = text_of(page, "#newCodes .code-text")
        check("生成激活码成功", bool(code), code)
        check("激活码格式规范", bool(re.match(r"^AILIVE(-[A-Z2-9]{4}){3}$", code)), code)
        check("激活码进入记录表", code in text_of(page, "#codeRows"),
              text_of(page, "#codeRows")[:80])
        page.screenshot(path=str(SHOTS / "03_admin_users.png"), full_page=True)

        # ── 4. 退出并注册免费用户 ─────────────────────────────
        print("\n4. 退出登录并自助注册免费用户")
        page.click('[data-auth-action="logout"]')
        page.wait_for_url("**/login.html*", timeout=25000)
        page.wait_for_load_state("networkidle")
        check("退出后回到登录页", "/login.html" in page.url, page.url)

        page.click("#tabRegister")
        page.wait_for_timeout(300)
        page.fill("#regUsername", FREE_USER)
        page.fill("#regPassword", FREE_PASSWORD)
        page.fill("#regPasswordConfirm", FREE_PASSWORD)
        page.click("#registerSubmit")
        registered = True
        try:
            page.wait_for_url("**/live_console.html*", timeout=12000)
        except Exception:
            registered = False
        if registered:
            check("自助注册成功并自动登录", True)
        else:
            # 自助注册有每 IP 每小时 5 次的限流，自检反复跑会触发；
            # 此时改用管理员开号接口继续验证会员分级链路。
            note = dom_text(page, "#registerMessage")
            check("自助注册被限流时给出提示", "频繁" in note, note[:80])
            api = context.request
            api.post(f"{BASE}/api/auth/login",
                     data={"username": ADMIN_USER, "password": ADMIN_PASSWORD})
            created = api.post(f"{BASE}/api/auth/admin/users",
                               data={"username": FREE_USER, "password": FREE_PASSWORD,
                                     "display_name": "自检免费用户", "plan": "free", "role": "user"})
            check("管理员开号不受自助注册限流",
                  created.status == 200, f"{created.status} {created.text()[:80]}")
            api.post(f"{BASE}/api/auth/login",
                     data={"username": FREE_USER, "password": FREE_PASSWORD})
            page.goto(f"{BASE}/live_console.html", wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1200)
        bar = wait_for_text(page, "#authBar", "免费版")
        check("进入免费账号工作台", "live_console.html" in page.url, page.url)
        check("顶部显示免费版", "免费版" in bar, bar[:80])
        check("显示免费版提示条", "免费版" in text_of(page, "#authBanner"),
              text_of(page, "#authBanner")[:80])

        # ── 5. 免费版功能分级 ────────────────────────────────
        print("\n5. 免费版功能分级")
        check("一键开播被加锁", "feature-locked" in classes(page, "#startLive"),
              classes(page, "#startLive"))
        check("自动话术被加锁", "feature-locked" in classes(page, "#startAuto"),
              classes(page, "#startAuto"))
        check("录制被加锁", "feature-locked" in classes(page, "#startRecord"),
              classes(page, "#startRecord"))
        check("字幕自动回应入口被加锁", "feature-locked" in classes(page, 'a[data-feature="subtitle"]'),
              classes(page, 'a[data-feature="subtitle"]'))
        check("大模型入口被加锁（管理员专属）",
              "feature-locked" in classes(page, 'a[data-feature="llm.config"]'))
        check("备份导出被加锁", "feature-locked" in classes(page, 'a[data-feature="export"]'),
              classes(page, 'a[data-feature="export"]'))
        check("商品库写入区被遮罩",
              page.locator('[data-feature-lock="product.write"] .feature-lock-overlay').count() == 1)
        check("安全下播未被加锁（安全操作）",
              "feature-locked" not in classes(page, "#stopLive"))
        check("紧急停止未被加锁（安全操作）",
              "feature-locked" not in classes(page, "#emergency"))
        check("模拟评论测试未被加锁（免费功能）",
              "feature-locked" not in classes(page, "#runSim"))
        competition_nav_free = page.locator('a[data-hide-without="competition.view"]')
        check("免费用户看不到「参赛项目」导航入口（元素存在但被隐藏）",
              competition_nav_free.count() >= 1 and not competition_nav_free.first.is_visible(),
              f"count={competition_nav_free.count()} visible={competition_nav_free.first.is_visible()}")
        page.click('[data-lc-tab="access"]')
        page.wait_for_timeout(400)
        free_card_display = computed_display(page, 'section[data-hide-without="competition.view"]')
        check("免费用户即使切到该页签也看不到参赛材料卡片",
              free_card_display == 'none', free_card_display)
        page.click('[data-lc-tab="check"]')
        page.wait_for_timeout(200)

        # 步骤 1-6 在「操作步骤」标签页里，先切过去才能操作
        page.click('[data-lc-tab="steps"]')
        page.wait_for_timeout(500)
        page.fill("#simText", "多少钱一斤？")
        page.click("#runSim")
        page.wait_for_timeout(2000)
        sim = text_of(page, "#simResult")
        check("免费版可以跑模拟评论测试", "判定链路" in sim, sim[:100])
        layout_check(page, "免费版工作台",
                     ["#authBar", "#startLive", "#stopLive", "#emergency"])
        page.screenshot(path=str(SHOTS / "04_free_console.png"), full_page=True)

        # ── 6. 锁定功能跳升级页 + 兑换 ────────────────────────
        print("\n6. 点击锁定功能 / 兑换激活码")
        # 锁定按钮点击后应弹出升级页（用事件监听，避免 expect_page 在失败时污染栈）
        opened: list = []
        context.on("page", lambda new_page: opened.append(new_page))
        # 锁定元素带 aria-disabled="true"（无障提示），Playwright 会按“不可点击”处理，
        # 但真实浏览器仍会派发 click —— 这里用 force=True 还原真实用户点击。
        page.click("#startAuto", force=True)
        deadline = time.time() + 15
        while not opened and time.time() < deadline:
            page.wait_for_timeout(200)
        check("点击锁定功能打开升级页", bool(opened), "未打开新页面")
        if not opened:
            context.close()
            browser.close()
            raise SystemExit(1)
        upgrade = opened[0]
        upgrade.set_default_timeout(25000)
        collect(upgrade, "upgrade")
        upgrade.wait_for_load_state("networkidle")
        upgrade.wait_for_timeout(1200)
        check("升级页地址正确", "upgrade.html" in upgrade.url, upgrade.url)
        status = wait_for_text(upgrade, "#statusCardBody", "免费版")
        check("升级页显示当前为免费版", "免费版" in status, status[:80])
        check("升级页渲染套餐对比", upgrade.locator("#planGrid").count() == 1)
        layout_check(upgrade, "升级页",
                     ["#statusCardBody", "#codeInput", "#redeemButton", "#planGrid", "#qrSlot"])

        upgrade.fill("#codeInput", "AILIVE-AAAA-BBBB-CCCC")
        upgrade.click("#redeemButton")
        upgrade.wait_for_timeout(1500)
        check("无效激活码被拒并提示", len(dom_text(upgrade, "#redeemMessage")) > 0,
              dom_text(upgrade, "#redeemMessage")[:80])

        upgrade.fill("#codeInput", code)
        upgrade.click("#redeemButton")
        status = wait_for_text(upgrade, "#statusCardBody", "专业版")
        check("有效激活码兑换成功", "专业版" in status, status[:100])
        upgrade.screenshot(path=str(SHOTS / "05_upgrade_paid.png"), full_page=True)

        # ── 7. 解锁后回到工作台 ───────────────────────────────
        print("\n7. 解锁后的工作台")
        page.goto(f"{BASE}/live_console.html", wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1800)
        bar = wait_for_text(page, "#authBar", "专业版")
        check("顶部显示专业版", "专业版" in bar, bar[:80])
        check("免费版提示条已消失", page.locator("#authBanner").count() == 0
              or "免费版" not in text_of(page, "#authBanner"),
              text_of(page, "#authBanner")[:80])
        check("一键开播锁已解除", "feature-locked" not in classes(page, "#startLive"),
              classes(page, "#startLive"))
        check("自动话术锁已解除", "feature-locked" not in classes(page, "#startAuto"),
              classes(page, "#startAuto"))
        check("录制锁已解除", "feature-locked" not in classes(page, "#startRecord"),
              classes(page, "#startRecord"))
        check("商品库遮罩已解除",
              page.locator('[data-feature-lock="product.write"] .feature-lock-overlay').count() == 0)
        page.screenshot(path=str(SHOTS / "06_paid_console.png"), full_page=True)

        # ── 8. 会员仍进不去管理员页面 ─────────────────────────
        print("\n8. 会员权限边界")
        page.goto(f"{BASE}/admin_users.html", wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")
        url = wait_for_url(page, "forbidden.html")
        body = dom_text(page, "body")
        check("专业版进用户管理页被挡到无权限页", "forbidden.html" in url, url)
        check("无权限页说明需要管理员", "权限" in body, body[:60])
        if "forbidden.html" in url:
            page.screenshot(path=str(SHOTS / "07_forbidden.png"), full_page=True)

        page.goto(f"{BASE}/llm.html", wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")
        url = wait_for_url(page, "forbidden.html")
        check("专业版进大模型页被挡（管理员专属）", "forbidden.html" in url, url)

        page.goto(f"{BASE}/competition.html", wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")
        url = wait_for_url(page, "forbidden.html")
        check("专业版进参赛项目页被挡（管理员专属）", "forbidden.html" in url, url)

        # 导航在所有页面共享，隐藏逻辑在 common.js 里 —— 换个页面再验一次
        page.goto(f"{BASE}/accounts.html", wait_until="domcontentloaded")
        page.wait_for_timeout(900)
        other_page_display = computed_display(page, 'a[data-hide-without="competition.view"]')
        check("换到账号管理页，普通会员依然看不到参赛项目入口",
              other_page_display == 'none', other_page_display)

        # ── 9. 修改密码：纯数字口令必须当场被拒（用户实际踩过的坑：以为改了其实没生效）──
        print("\n9. 修改密码的规则校验")
        # 修改密码弹窗由 auth.js 提供，而 auth.js 只在少数页面引入 —— 先回工作台
        page.goto(f"{BASE}/live_console.html", wait_until="domcontentloaded")
        page.wait_for_selector("#authBar", timeout=20000)
        page.wait_for_timeout(600)
        page.evaluate("() => window.Auth.openPasswordModal()")
        page.wait_for_selector("#authPasswordModal", timeout=10000)
        page.fill("#authOldPw", FREE_PASSWORD)
        page.fill("#authNewPw", "12345678")
        page.fill("#authNewPw2", "12345678")
        page.click("#authPwSubmit")
        page.wait_for_timeout(500)
        message = dom_text(page, "#authPwMessage")
        classes_of_message = page.locator("#authPwMessage").get_attribute("class") or ""
        check("纯数字新密码被当场拦下", "字母和数字" in message, message[:80])
        check("提示标红醒目（不是灰色小字）", "is-error" in classes_of_message, classes_of_message)
        check("弹窗没有关闭（没被误当成修改成功）",
              page.locator("#authPasswordModal").count() == 1)
        page.locator("#authPasswordModal [data-modal-close]").first.click()
        page.wait_for_timeout(300)

        # ── 10. 同一账号只允许一处登录（换一台"设备"再登录，原来那台应当掉线）──
        print("\n10. 同一账号只允许一处登录")
        second_context = browser.new_context(viewport={"width": 1440, "height": 1000},
                                             locale="zh-CN")
        second_page = second_context.new_page()
        second_page.set_default_timeout(25000)
        collect(second_page, "second-device")
        second_page.goto(f"{BASE}/login.html", wait_until="domcontentloaded")
        second_page.wait_for_selector("#loginUsername", timeout=20000)
        second_page.fill("#loginUsername", FREE_USER)
        second_page.fill("#loginPassword", FREE_PASSWORD)
        second_page.click("#loginSubmit")
        second_page.wait_for_url("**/live_console.html*", timeout=25000)
        check("第二台设备登录成功", "live_console.html" in second_page.url, second_page.url)

        page.goto(f"{BASE}/live_console.html", wait_until="domcontentloaded")
        kicked_url = wait_for_url(page, "login.html")
        check("原来那台设备被挤下线（回到登录页）", "login.html" in kicked_url, kicked_url)
        check("登录页说明了只能一处登录",
              "只能在一处登录" in dom_text(page, "#singleSessionNotice"),
              dom_text(page, "#singleSessionNotice")[:60])
        second_context.close()

        # ── 11. 清理自检数据 ─────────────────────────────────
        print("\n11. 清理自检数据")
        api = context.request
        api.post(f"{BASE}/api/auth/login",
                 data={"username": ADMIN_USER, "password": ADMIN_PASSWORD})
        removed = api.delete(f"{BASE}/api/auth/admin/users/{FREE_USER}")
        check("删除自检账号", removed.status == 200, f"{removed.status} {removed.text()[:80]}")
        revoked = api.post(f"{BASE}/api/auth/admin/codes/revoke", data={"code": code})
        check("作废自检激活码", revoked.status == 200, f"{revoked.status} {revoked.text()[:80]}")

        context.close()
        browser.close()

    # ── 汇总 ─────────────────────────────────────────────────
    print("\n" + "=" * 64)
    if CONSOLE_ERRORS:
        print(f"浏览器控制台报错 {len(CONSOLE_ERRORS)} 条：")
        for item in CONSOLE_ERRORS[:10]:
            print("   -", item[:160])
    else:
        print("浏览器控制台无异常报错")
    if PAGE_ERRORS:
        print(f"未捕获 JS 异常 {len(PAGE_ERRORS)} 条：")
        for item in PAGE_ERRORS[:10]:
            print("   -", item[:160])

    if FAILED or PAGE_ERRORS:
        print(f"\n端到端验收未通过：{PASSED} 项成功，{len(FAILED)} 项失败，"
              f"{len(PAGE_ERRORS)} 个 JS 异常")
        for item in FAILED:
            print("   -", item)
        return 1
    print(f"\n端到端验收通过：{PASSED} 项检查全部成功，无 JS 异常")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main())