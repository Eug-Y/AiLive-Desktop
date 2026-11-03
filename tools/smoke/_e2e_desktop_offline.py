# -*- coding: utf-8 -*-
"""离线演示版（file:// 打开，不需要服务）验收 + 主色调/主题变量证据。"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

FILE = Path(r"E:\AiLive\desktop\AI数字人桌面版.html").as_uri()
SHOTS = Path(r"E:\AiLive\_qa\desktop-v1")
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), str(detail)[:200]))
    print(("PASS " if ok else "FAIL ") + name + ("  | " + str(detail)[:200] if detail else ""))


def main():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, executable_path=r"C:\Users\lenovo\AppData\Local\ms-playwright\chromium-1247\chrome-win64\chrome.exe")
        ctx = b.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
        page = ctx.new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.goto(FILE, wait_until="domcontentloaded")
        page.wait_for_selector("#iconLayer .icon", timeout=20000)
        page.wait_for_function("() => !document.getElementById('boot')", timeout=20000)
        page.wait_for_timeout(800)
        check("file:// 打开也能渲染桌面（13 个图标）", page.locator("#iconLayer .icon").count() == 13,
              "icons=%d" % page.locator("#iconLayer .icon").count())
        check("file:// 自动降级为离线演示模式", "离线演示" in page.inner_text("#modeText"), page.inner_text("#modeText"))

        page.dblclick('.icon[data-app="control"]')
        page.wait_for_selector("#ctStats .stat", timeout=8000)
        check("演示模式控制中心有内容（不空白）", page.locator("#ctStats .stat").count() >= 6,
              "stat=%d" % page.locator("#ctStats .stat").count())
        page.fill("#ctSimText", "这个多少钱？有优惠券吗")
        page.click("#ctSimRun")
        page.wait_for_selector("#ctSimOut .badge", timeout=8000)
        sim = page.inner_text("#ctSimOut")
        check("演示模式本地判定可用", "T1" in sim and "199" in sim, sim.replace("\n", " ")[:140])

        # 主题变量证据（主色调浅蓝 + 深色主题覆盖）
        styles = page.evaluate("""() => {
            const cs = getComputedStyle(document.documentElement);
            return {
              brand: cs.getPropertyValue('--brand').trim(),
              brandStrong: cs.getPropertyValue('--brand-strong').trim(),
              theme: document.documentElement.dataset.theme,
              wallpaper: document.documentElement.dataset.wallpaper,
              taskbarH: cs.getPropertyValue('--taskbar-h').trim(),
              winRadius: getComputedStyle(document.querySelector('.wm-window')).borderRadius,
              winBg: getComputedStyle(document.querySelector('.wm-window')).backgroundColor
            };
        }""")
        check("主色调为浅蓝（CSS 变量）", styles["brand"].lower() == "#38bdf8" and styles["brandStrong"].lower() == "#0ea5e9",
              str(styles))
        check("窗口用圆角 + 半透明毛玻璃", styles["winRadius"].startswith("14px") and "rgba" in styles["winBg"],
              "radius=%s bg=%s" % (styles["winRadius"], styles["winBg"]))

        page.click("#showDesktop")   # 控制中心占着桌面左侧，先“显示桌面”再点设置图标
        page.wait_for_timeout(400)
        page.dblclick('.icon[data-app="settings"]')
        page.wait_for_selector('.wm-window[data-app="settings"]', timeout=6000)
        page.wait_for_timeout(500)
        page.click('#setTabs button[data-t="look"]')
        page.click('.seg[data-seg="theme"] button[data-v="dark"]')
        page.wait_for_timeout(300)
        dark = page.evaluate("""() => { const cs = getComputedStyle(document.documentElement);
            return {theme: document.documentElement.dataset.theme, winBg: getComputedStyle(document.querySelector('.wm-window')).backgroundColor,
                    text: cs.getPropertyValue('--text').trim()}; }""")
        check("深色主题变量生效", dark["theme"] == "dark" and dark["text"].lower().startswith("#e8"), str(dark))
        page.screenshot(path=str(SHOTS / "16-offline-demo-dark.png"))
        page.click('.seg[data-seg="theme"] button[data-v="light"]')
        page.wait_for_timeout(300)
        page.screenshot(path=str(SHOTS / "17-offline-demo.png"))

        check("file:// 页面无 JS 异常", not errs, " | ".join(errs)[:200])
        b.close()

    ok = sum(1 for _, o, _ in RESULTS if o)
    print("\n==== 离线演示版验收：%d/%d 通过 ====" % (ok, len(RESULTS)))
    for n, o, d in RESULTS:
        if not o:
            print("  FAILED: %s | %s" % (n, d))
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())