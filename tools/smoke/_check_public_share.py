"""公网分享验收：从 Cloudflare 隧道地址用真实浏览器打开界面，确认外面的人能看见。

用法（先双击「外网分享给别人看.cmd」，等服务给出地址）：
    E:\\AiLive\\env\\python.exe E:\\AiLive\\tools\\smoke\\_check_public_share.py
    # 也可以直接指定地址：
    $env:AILIVE_PUBLIC_URL = "https://xxxx.trycloudflare.com"
    $env:AILIVE_ADMIN_USER = "管理员用户名"; $env:AILIVE_ADMIN_PASSWORD = "密码"

检查内容：隧道连通 → HTTPS 安全上下文（决定麦克风能否用）→ 公网登录 →
工作台/导航/权限 → 隧道上的接口鉴权没被绕过。临时账号跑完自动删除。
"""

import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

LAN = "http://127.0.0.1:8010"
TUNNEL_LOG = Path(r"E:\AiLive\tunnel.err.log")
TUNNEL_LOG_ALT = Path(r"E:\AiLive\tunnel.out.log")
URL_FILE = Path(r"E:\AiLive\当前分享地址.txt")
ADMIN_USER = os.environ.get("AILIVE_ADMIN_USER", "")
ADMIN_PASSWORD = os.environ.get("AILIVE_ADMIN_PASSWORD", "")
USER = "e2eshare" + time.strftime("%m%d%H%M%S")
PASSWORD = "ShareCheck2026"
NOISE = ("favicon", "status of 401", "status of 404")

RESULTS = []


def check(label: str, condition: bool, detail: str = "") -> None:
    RESULTS.append((label, bool(condition), detail))
    print(f"  [{'OK' if condition else 'FAIL'}] {label}"
          + (f"  ← {detail}" if detail and not condition else ""))


def public_url() -> str:
    given = os.environ.get("AILIVE_PUBLIC_URL", "").strip()
    if given:
        return given.rstrip("/")
    # 分享脚本会把当前地址写在这里（最新一次）
    if URL_FILE.exists():
        found = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com",
                           URL_FILE.read_text(encoding="utf-8", errors="replace"))
        if found:
            return found[-1]
    for path in (TUNNEL_LOG, TUNNEL_LOG_ALT):
        if path.exists():
            found = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com",
                               path.read_text(encoding="utf-8", errors="replace"))
            if found:
                return found[-1]
    return ""


def find_chromium() -> str | None:
    root = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    candidates = sorted(root.glob("chromium_headless_shell-*/chrome-headless-shell-win64/chrome-headless-shell.exe"))
    candidates += sorted(root.glob("chromium-*/chrome-win/chrome.exe"))
    return str(candidates[-1]) if candidates else None


class Admin:
    """带 Cookie 的管理端会话（建/删临时账号）。"""

    def __init__(self) -> None:
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method: str, path: str, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(LAN + path, data=data, method=method)
        if data:
            request.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(request, timeout=20) as response:
                return response.status, json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            try:
                return exc.code, json.loads(body)
            except json.JSONDecodeError:
                return exc.code, {"msg": body[:120]}


def main() -> int:
    url = public_url()
    if not url:
        print("没有找到隧道地址：先双击「外网分享给别人看.cmd」，或设置 AILIVE_PUBLIC_URL。")
        return 2
    if not ADMIN_PASSWORD:
        print("请先设置 AILIVE_ADMIN_PASSWORD（临时账号需要用它创建）。")
        return 2
    print(f"公网地址：{url}")

    admin = Admin()
    status, payload = admin.call("POST", "/api/auth/login",
                                 {"username": ADMIN_USER, "password": ADMIN_PASSWORD})
    if status != 200:
        print(f"管理端登录失败：{status} {payload}")
        return 2
    status, payload = admin.call("POST", "/api/auth/admin/users",
                                 {"username": USER, "password": PASSWORD, "plan": "pro"})
    check("建临时专业版账号（用于公网登录测试）", status == 200, f"{status} {payload}")
    errors = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=find_chromium(), headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="zh-CN")
            page = context.new_page()
            page.set_default_timeout(30000)
            page.on("pageerror", lambda exc: errors.append(f"PAGEERROR: {exc}"))
            page.on("console", lambda msg: errors.append(f"CONSOLE: {msg.text}")
                    if msg.type == "error" and not any(n in msg.text.lower() for n in NOISE) else None)

            print("\n1. 公网地址能否打开")
            response = page.goto(url + "/live_console.html", wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            check("隧道连到了本机服务（不是 502/1033）",
                  response is not None and response.status < 500,
                  f"status={response.status if response else None}")
            check("未登录被引导到登录页", "login.html" in page.url, page.url[:100])

            print("\n2. 公网环境的安全上下文（决定麦克风 / 摄像头能不能用）")
            env = page.evaluate("""() => ({
                secure: window.isSecureContext,
                media: typeof navigator.mediaDevices,
                rtc: typeof RTCPeerConnection,
                proto: location.protocol,
            })""")
            print(f"     {env}")
            check("公网地址是 HTTPS 安全上下文", env["secure"] is True, str(env))
            check("麦克风接口可用（局域网 http 访问时会被浏览器禁掉）",
                  env["media"] == "object", str(env))
            check("WebRTC 接口存在", env["rtc"] == "function", str(env))

            print("\n3. 公网登录并进入工作台")
            page.fill("#loginUsername", USER)
            page.fill("#loginPassword", PASSWORD)
            page.click("#loginSubmit")
            page.wait_for_url("**/live_console.html*", timeout=30000)
            page.wait_for_selector("#authBar", timeout=20000)
            page.wait_for_timeout(1200)
            check("公网登录成功并进入工作台", "live_console.html" in page.url, page.url[:100])
            nav = page.locator("#lt-nav-container a").count()
            check("导航正常渲染（静态资源与 common.js 都走通了）", nav >= 10, f"navLinks={nav}")
            entitlements = page.evaluate("() => (window.Auth && window.Auth.state.entitlements) || {}")
            check("权限已下发到前端", entitlements.get("live.start") is True,
                  str(list(entitlements.items())[:3]))
            check("页面标题正常", bool(page.title()), page.title())
            page.screenshot(path=r"E:\AiLive\tmp\public_share.png")

            print("\n4. 隧道上的接口鉴权没有被绕过")
            status_code = page.evaluate("""async () => {
                const r = await fetch('/api/live/products', {method: 'POST',
                    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name: 't'})});
                return r.status;
            }""")
            check("已登录用户写商品成功（走隧道同样生效）", status_code in (200, 201),
                  f"status={status_code}")

            context.close()
            browser.close()
    finally:
        status, payload = admin.call("DELETE", f"/api/auth/admin/users/{USER}")
        check("清理临时账号", status == 200, f"{status} {payload}")

    failed = [item for item in RESULTS if not item[1]]
    if errors:
        print("\n浏览器报错：")
        for item in errors[:8]:
            print("   " + item)
    print("\n" + "=" * 60)
    if failed or errors:
        print(f"公网分享验收未通过：{len(failed)} 项失败，{len(errors)} 条浏览器报错")
        return 1
    print(f"公网分享验收通过：{len(RESULTS)} 项检查全部成功，无 JS 异常")
    print("提醒：数字人画面走 WebRTC 直连（两端 STUN 打洞），能否看到要由真实远端设备确认；")
    print("     不通的话需要 TURN 或改用 HTTP-FLV 播放。")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main())