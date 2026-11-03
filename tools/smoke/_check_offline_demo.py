# -*- coding: utf-8 -*-
"""离线演示副本自检：用浏览器 file:// 打开 desktop\\AI数字人桌面界面（离线演示）.html。

验的是三件事：
  1. 不连服务也能正常渲染常规桌面软件界面（左导航 + 顶部工具栏 + 多标签 + 状态栏）；
  2. 自动进入「离线演示」模式并明确标注，不冒充实盘数据；
  3. 全程没有对 127.0.0.1:8010 发起任何请求（真正的离线）。

用法：E:\\AiLive\\env\\python.exe tools\\smoke\\_check_offline_demo.py
"""
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

COPY = Path(r"E:\AiLive\desktop\AI数字人桌面界面（离线演示）.html")
SHOT = Path(r"E:\AiLive\_qa\app-v1\offline-demo.png")
RESULTS = []


def launch(p):
    """和 _e2e_app_ui.py 一致：本机没装 playwright 自带的 headless shell，
    按“已存在的 chromium → Edge → 默认”挑一个能用浏览器。"""
    local = os.environ.get("LOCALAPPDATA", "")
    cand = [
        os.path.join(local, r"ms-playwright\chromium-1247\chrome-win64\chrome.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for path in cand:
        if os.path.exists(path):
            try:
                b = p.chromium.launch(headless=True, executable_path=path)
                print("using browser:", path)
                return b
            except Exception as exc:
                print("launch failed", path, exc)
    print("using bundled chromium")
    return p.chromium.launch(headless=True)


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), str(detail)[:200]))
    print(("PASS " if ok else "FAIL ") + name + ("  | " + str(detail)[:200] if detail else ""))


def main():
    if not COPY.is_file():
        check("离线演示副本存在", False, str(COPY))
        return finish()

    check("离线演示副本存在", True, "%s（%.1f KB）" % (COPY.name, COPY.stat().st_size / 1024))

    # 副本必须是主版本的逐字拷贝（改完 LiveTalking\web\app.html 要跑 desktop\同步桌面版副本.cmd）
    src = Path(r"E:\AiLive\LiveTalking\web\app.html")
    if src.is_file():
        import hashlib
        hs = hashlib.sha256(src.read_bytes()).hexdigest()
        hc = hashlib.sha256(COPY.read_bytes()).hexdigest()
        check("离线副本与主版本 app.html 逐字一致（没有过期副本）", hs == hc,
              "src=%s copy=%s" % (hs[:12], hc[:12]))

    with sync_playwright() as pw:
        browser = launch(pw)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        hits = []
        page.on("request", lambda r: hits.append(r.url) if "8010" in r.url else None)
        page.goto(COPY.as_uri(), wait_until="domcontentloaded")
        page.wait_for_selector("#navList .nav", timeout=30000)
        page.wait_for_timeout(1200)

        nav = page.eval_on_selector_all("#navList .nav", "els => els.length")
        grp = page.eval_on_selector_all("#navList .grp", "els => els.length")
        brand = page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--brand').trim()")
        mode = page.evaluate("(document.getElementById('modeText')||{}).textContent || ''")
        st = page.evaluate("(document.getElementById('stSvc')||{}).textContent || ''")
        tabs = page.eval_on_selector_all("#tabbar .tab[data-app]", "els => els.length")
        banner = page.evaluate("!!document.querySelector('.banner.warn')")
        boot = page.evaluate("!!document.getElementById('boot') && !document.getElementById('boot').classList.contains('hide')")
        fake = page.evaluate("document.querySelectorAll('#desktop,#taskbar,#startmenu').length")

        check("界面渲染出左导航栏", nav == 17, "nav=%s（期望 17：14 个原有模块 + 数字人形象/语音合成/交互面板）" % nav)
        check("导航分组正常", grp == 4, "grp=%s（期望 4）" % grp)
        check("品牌主色仍是浅蓝 #38bdf8", brand == "#38bdf8", brand)
        check("自动进入离线演示模式", "演示" in (mode + st), "modeText=%r stSvc=%r" % (mode.strip(), st.strip()))
        check("有明确的离线提示横幅（不冒充真实数据）", banner, "banner=%s" % banner)
        check("默认打开了一个标签页", tabs >= 1, "tabs=%s" % tabs)
        check("启动遮罩已消失", not boot, "bootVisible=%s" % boot)
        check("没有假桌面残留（任务栏/开始菜单）", fake == 0, "count=%s" % fake)
        check("全程没有请求 8010 服务（真离线）", len(hits) == 0, "命中 %d 次 %s" % (len(hits), hits[:2]))

        # 端区分：离线副本是 file:// 打开的网页端，桌面专属入口同样不该出现
        brandver = page.evaluate("(document.getElementById('brandVer')||{}).textContent || ''").strip()
        check("离线副本按端显示“网页端”（不自称桌面版）", brandver == "网页端", brandver)
        page.click("#menuFile")
        page.wait_for_timeout(250)
        file_items = page.locator("#popMenu .it .lbl").all_inner_texts()
        check("离线副本里没有“退出应用”等桌面专属入口", "退出应用" not in file_items, "|".join(file_items))
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)

        SHOT.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOT))
        check("已保存离线演示截图", SHOT.is_file(), str(SHOT))
        browser.close()
    return finish()


def finish():
    ok = sum(1 for _, o, _ in RESULTS if o)
    print("\n==== %d/%d 通过 ====" % (ok, len(RESULTS)))
    if ok != len(RESULTS):
        for n, o, d in RESULTS:
            if not o:
                print("  - %s | %s" % (n, d))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())