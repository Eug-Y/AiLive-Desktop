"""登录 / 会员体系冒烟自检。

不需要加载数字人模型，直接起一个只含鉴权中间件与同形桩路由的 aiohttp 服务，
用真实 HTTP + Cookie 跑完整链路：

    未登录拦截 → 注册（免费）→ 免费用户功能分级 → 安全操作放行
    → 管理员建码 → 用户兑换 → 升级后解锁 → 过期回落 → 改密/禁用/删除保护
    → 关闭登录后的兼容行为

用法：
    E:\\AiLive\\env\\python.exe tools\\smoke\\_smoke_auth.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "LiveTalking"
sys.path.insert(0, str(ROOT))

# Windows 控制台默认 GBK，会把中文与符号写崩；统一切到 UTF-8。
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

TMP_DIR = Path(tempfile.mkdtemp(prefix="ailive-auth-smoke-"))
os.environ["AILIVE_AUTH_DIR"] = str(TMP_DIR)
os.environ.pop("AILIVE_AUTH", None)
os.environ.pop("AILIVE_ALLOW_REGISTER", None)

from aiohttp import ClientSession, CookieJar, web  # noqa: E402

from server import auth_manager as am  # noqa: E402
from server.auth_routes import setup_auth_routes  # noqa: E402

PASSED = 0
FAILED = []


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ✅ {label}")
    else:
        FAILED.append(f"{label} :: {detail}")
        print(f"  ❌ {label} :: {detail}")


def stub(name: str):
    async def handler(request: web.Request):
        return web.json_response({
            "code": 0,
            "msg": "ok",
            "data": {
                "stub": name,
                "user": (request.get("auth_user") or {}).get("username"),
                "internal_sales": request.get("internal_sales"),
            },
        })
    return handler


def html(name: str):
    async def handler(request: web.Request):
        return web.Response(text=f"<html><body>{name}</body></html>", content_type="text/html")
    return handler


API_STUBS = [
    ("GET", "/api/live/status", "status"),
    ("GET", "/api/live/products", "products.list"),
    ("POST", "/api/live/products", "products.write"),
    ("POST", "/api/live/products/scripts", "products.scripts"),
    ("POST", "/api/workflow/start", "workflow.start"),
    ("POST", "/api/workflow/stop", "workflow.stop"),
    ("GET", "/api/workflow/preflight", "workflow.preflight"),
    ("GET", "/api/live/export", "export"),
    ("POST", "/api/live/emergency-stop", "emergency"),
    ("POST", "/api/live/simulate", "simulate"),
    ("POST", "/api/live/register", "register"),
    ("POST", "/api/live/recording", "recording"),
    ("POST", "/whep", "whep"),
    ("GET", "/api/subtitle/status", "subtitle.status"),
    ("POST", "/api/subtitle/start", "subtitle.start"),
    ("GET", "/api/llm/config", "llm.config.read"),
    ("POST", "/api/llm/credential", "llm.credential"),
    ("GET", "/api/avatar/tasks", "avatar.tasks"),
    ("POST", "/api/avatar/task", "avatar.create"),
    ("GET", "/api/admin/config", "admin.config"),
    ("GET", "/api/system/access", "system.access"),
    ("GET", "/api/accounts", "accounts.list"),
    ("POST", "/api/accounts", "accounts.create"),
]

PAGES = ["/live_console.html", "/live_output.html", "/upgrade.html", "/login.html",
         "/subtitle.html", "/llm.html", "/index.html", "/affiliate_tutorial.html"]


def build_app() -> web.Application:
    app = web.Application()
    setup_auth_routes(app)
    for method, path, name in API_STUBS:
        app.router.add_route(method, path, stub(name))
    for page in PAGES:
        app.router.add_get(page, html(page))
    return app


class Client:
    """独立 Cookie 会话，模拟一个浏览器用户。"""

    def __init__(self, base: str):
        self.base = base
        self.jar = CookieJar(unsafe=True)
        self.session = ClientSession(cookie_jar=self.jar)

    async def close(self):
        await self.session.close()

    async def raw(self, method: str, path: str, body=None, redirect=False):
        kwargs = {"allow_redirects": redirect}
        if body is not None:
            kwargs["json"] = body
        async with self.session.request(method, self.base + path, **kwargs) as response:
            text = await response.text()
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = None
            return response.status, payload, response.headers, text

    async def api(self, method: str, path: str, body=None):
        status, payload, _, _ = await self.raw(method, path, body)
        return status, (payload or {})

    async def login(self, username: str, password: str):
        return await self.api("POST", "/api/auth/login",
                              {"username": username, "password": password})

    async def register(self, username: str, password: str):
        return await self.api("POST", "/api/auth/register",
                              {"username": username, "password": password})

    async def me(self):
        _, payload = await self.api("GET", "/api/auth/me")
        return payload.get("data", {})


async def main() -> int:
    runner = web.AppRunner(build_app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"

    print(f"\n📁 临时鉴权目录：{TMP_DIR}")
    print(f"🌐 冒烟服务：{base}\n")

    guest = Client(base)
    free = Client(base)
    admin = Client(base)
    other = Client(base)

    try:
        # ── 1. 未登录拦截 ─────────────────────────────────────────────
        print("1. 未登录拦截")
        status, payload, headers, _ = await guest.raw("GET", "/live_console.html")
        location = headers.get("Location", "")
        next_value = ""
        if "?" in location:
            from urllib.parse import parse_qs, urlparse
            next_value = (parse_qs(urlparse(location).query).get("next") or [""])[0]
        check("页面访问跳转登录页", status == 302 and location.startswith("/login.html"),
              f"status={status} location={location}")
        check("跳转带上 next 参数", next_value == "/live_console.html",
              f"{location} → next={next_value!r}")
        status, payload = await guest.api("GET", "/api/live/status")
        check("接口未登录返回 401", status == 401 and payload.get("code") == 401, f"{status} {payload}")
        status, payload = await guest.api("GET", "/api/auth/plans")
        check("套餐目录公开可读", status == 200 and "pro" in payload.get("data", {}).get("plans", {}),
              f"{status} {payload}")
        status, payload = await guest.api("GET", "/api/auth/me")
        check("未登录 me 返回 authenticated=false",
              payload.get("data", {}).get("authenticated") is False, str(payload))

        # ── 2. 注册与免费用户分级 ─────────────────────────────────────
        print("\n2. 注册（免费用户）与功能分级")
        status, payload = await free.register("xiaoli", "xiaoli2026")
        check("注册成功并自动登录", status == 200 and payload.get("code") == 0, f"{status} {payload}")
        info = await free.me()
        check("免费用户等级为 free", info["user"]["plan"] == "free" and info["user"]["level"] == 0,
              str(info.get("user")))
        check("免费用户可看工作台", info["entitlements"]["console.view"] is True, "")
        check("免费用户无开播权限", info["entitlements"]["live.start"] is False, "")
        status, payload = await free.api("GET", "/api/live/status")
        check("免费用户可读取直播状态", status == 200, f"{status} {payload}")
        status, payload = await free.api("GET", "/api/live/products")
        check("免费用户可浏览商品库", status == 200, f"{status} {payload}")
        status, payload = await free.api("POST", "/api/live/simulate", {"text": "多少钱"})
        check("免费用户可做模拟评论测试", status == 200, f"{status} {payload}")
        status, payload = await free.api("POST", "/api/live/products", {"name": "测试"})
        check("免费用户写商品被拒 403", status == 403 and payload.get("code") == 403, f"{status} {payload}")
        check("403 带购买引导", "upgrade_url" in payload and payload.get("required_plan") == "pro",
              str(payload))
        status, payload = await free.api("GET", "/api/live/export")
        check("免费用户备份被拒", status == 403, f"{status} {payload}")
        status, payload = await free.api("POST", "/api/workflow/start", {})
        check("免费用户一键开播被拒", status == 403, f"{status} {payload}")
        status, payload = await free.api("POST", "/api/workflow/stop", {})
        check("安全操作：停播对免费用户放行", status == 200, f"{status} {payload}")
        status, payload = await free.api("POST", "/api/live/emergency-stop", {})
        check("安全操作：紧急停止放行", status == 200, f"{status} {payload}")
        status, payload, headers, _ = await free.raw("GET", "/live_output.html")
        check("免费用户进输出窗口跳升级页",
              status == 302 and headers.get("Location", "").startswith("/upgrade.html"),
              f"{status} {headers.get('Location')}")
        status, payload, headers, _ = await free.raw("GET", "/llm.html")
        check("非管理员进大模型页跳无权限页",
              status == 302 and headers.get("Location", "").startswith("/forbidden.html"),
              f"{status} {headers.get('Location')}")
        status, payload = await free.api("GET", "/api/admin/config")
        check("非管理员访问后台接口 403", status == 403, f"{status} {payload}")

        # ── 3. 管理员 ────────────────────────────────────────────────
        print("\n3. 管理员能力")
        status, payload = await admin.login("admin", "admin123456")
        check("默认管理员可登录", status == 200 and payload.get("code") == 0, f"{status} {payload}")
        info = await admin.me()
        check("管理员提示需修改初始密码", info.get("must_change_password") is True, str(info.get("must_change_password")))
        check("管理员全部功能放行",
              all(info["entitlements"].values()), str([k for k, v in info["entitlements"].items() if not v]))
        status, payload = await admin.api("POST", "/api/live/products", {"name": "管理员商品"})
        check("管理员可写商品", status == 200, f"{status} {payload}")
        status, payload = await admin.api("GET", "/api/subtitle/status")
        check("管理员可看字幕状态", status == 200, f"{status} {payload}")
        status, payload = await admin.api("POST", "/api/avatar/task", {})
        check("管理员可管理数字人素材", status == 200, f"{status} {payload}")
        status, payload = await admin.api("GET", "/api/auth/admin/users")
        users = payload.get("data", {}).get("users", [])
        check("管理员可列出用户", status == 200 and any(u["username"] == "xiaoli" for u in users),
              f"{status} {users}")
        check("用户列表不含密码字段", all("password" not in u for u in users), "泄露了密码字段")

        status, payload = await admin.api("POST", "/api/auth/admin/codes",
                                          {"plan": "pro", "days": 30, "count": 2, "note": "冒烟"})
        codes = payload.get("data", {}).get("codes", [])
        check("管理员生成激活码", status == 200 and len(codes) == 2, f"{status} {payload}")
        code_valid = codes[0]["code"] if codes else ""
        code_reusable = codes[1]["code"] if len(codes) > 1 else ""
        check("激活码格式规范",
              bool(__import__("re").match(r"^AILIVE(-[A-Z2-9]{4}){3}$", code_valid)), code_valid)

        # ── 4. 兑换与升级 ────────────────────────────────────────────
        print("\n4. 用户兑换激活码并解锁")
        status, payload = await free.api("POST", "/api/auth/redeem", {"code": "AILIVE-AAAA-BBBB-CCCC"})
        check("无效激活码被拒", status == 404 and payload.get("code") == -7, f"{status} {payload}")
        status, payload = await free.api("POST", "/api/auth/redeem", {"code": code_valid.lower().replace("-", " ")})
        check("激活码容错输入也能兑换", status == 200, f"{status} {payload}")
        info = await free.me()
        check("兑换后升级为 pro", info["user"]["plan"] == "pro" and info["user"]["level"] == 1,
              str(info.get("user")))
        check("兑换后开播权限打开", info["entitlements"]["live.start"] is True, "")
        check("到期时间为 30 天后",
              info["user"]["plan_days_left"] in (29, 30), str(info["user"]["plan_days_left"]))
        status, payload = await free.api("POST", "/api/live/products", {"name": "会员商品"})
        check("升级后可写商品", status == 200, f"{status} {payload}")
        status, payload, headers, _ = await free.raw("GET", "/live_output.html")
        check("升级后可进输出窗口", status == 200, f"{status} {headers.get('Location')}")
        status, payload = await free.api("POST", "/api/auth/redeem", {"code": code_valid})
        check("同一码不能重复兑换", status == 400, f"{status} {payload}")
        status, payload = await free.api("GET", "/api/auth/admin/users")
        check("会员仍然不是管理员", status == 403, f"{status} {payload}")

        # ── 5. 管理员维护 ────────────────────────────────────────────
        print("\n5. 管理员维护用户")
        status, payload = await admin.api("PUT", "/api/auth/admin/users/xiaoli",
                                          {"plan": "free"})
        check("管理员可降级为免费", status == 200, f"{status} {payload}")
        status, payload = await free.api("POST", "/api/live/products", {"name": "降级后"})
        check("降级后立刻失去权限", status == 403, f"{status} {payload}")
        status, payload = await admin.api("PUT", "/api/auth/admin/users/xiaoli",
                                          {"plan": "pro", "days": 7})
        check("管理员可再升级", status == 200, f"{status} {payload}")
        status, payload = await admin.api("PUT", "/api/auth/admin/users/xiaoli",
                                          {"plan_expires_at": "2020-01-01T00:00:00+00:00"})
        check("可写入过期时间", status == 200, f"{status} {payload}")
        info = await free.me()
        check("套餐过期后回落为免费", info["user"]["level"] == 0 and info["user"]["plan_expired"] is True,
              str(info.get("user")))
        status, payload = await free.api("POST", "/api/live/products", {"name": "过期后"})
        check("过期后写入被拒", status == 403 and payload.get("plan_expired") is True,
              f"{status} {payload}")

        status, payload = await admin.api("PUT", "/api/auth/admin/users/xiaoli", {"disabled": True})
        check("管理员可禁用用户", status == 200, f"{status} {payload}")
        status, payload = await free.api("GET", "/api/live/status")
        check("禁用后现有会话立即失效", status == 401, f"{status} {payload}")
        status, payload = await other.login("xiaoli", "xiaoli2026")
        check("禁用后无法登录", status == 403, f"{status} {payload}")
        status, payload = await admin.api("PUT", "/api/auth/admin/users/xiaoli", {"disabled": False})
        check("管理员可解禁", status == 200, f"{status} {payload}")

        status, payload = await other.login("xiaoli", "xiaoli2026")
        check("解禁后可登录", status == 200, f"{status} {payload}")
        status, payload = await other.api("POST", "/api/auth/password",
                                          {"old_password": "xiaoli2026", "new_password": "xiaoli2027"})
        check("用户可改密码", status == 200, f"{status} {payload}")
        status, payload = await other.api("GET", "/api/live/status")
        check("改密后当前会话仍可用", status == 200, f"{status} {payload}")
        status, payload = await free.api("GET", "/api/live/status")
        check("旧会话最终失效（单点登录 + 改密）", status == 401, f"{status} {payload}")
        status, payload = await other.api("POST", "/api/auth/password",
                                          {"old_password": "wrongpass1", "new_password": "abc12345"})
        check("原密码错误被拒", status == 401, f"{status} {payload}")

        status, payload = await admin.api("DELETE", "/api/auth/admin/users/admin")
        check("不能删除最后一个管理员", payload.get("code") == -6, str(payload))
        status, payload = await admin.api("PUT", "/api/auth/admin/users/admin", {"role": "user"})
        check("不能降级最后一个管理员", payload.get("code") == -6, str(payload))

        status, payload = await admin.api("POST", "/api/auth/admin/config", {"allow_register": False})
        check("管理员可关闭自助注册", status == 200 and payload["data"]["config"]["allow_register"] is False,
              f"{status} {payload}")
        status, payload = await other.register("newbie", "newbie2026")
        check("关闭后注册被拒", status == 403, f"{status} {payload}")
        await admin.api("POST", "/api/auth/admin/config", {"allow_register": True})

        status, payload = await admin.api("POST", "/api/auth/admin/users",
                                          {"username": "staff", "password": "staff2026", "plan": "admin", "role": "admin"})
        check("管理员可建管理员账号", status == 200, f"{status} {payload}")
        status, payload = await admin.api("POST", "/api/auth/admin/codes/revoke", {"code": code_reusable})
        check("管理员可作废激活码", status == 200, f"{status} {payload}")
        status, payload = await other.api("POST", "/api/auth/redeem", {"code": code_reusable})
        check("作废的码无法兑换", status == 400, f"{status} {payload}")

        status, payload = await other.api("GET", "/api/auth/admin/codes")
        check("普通用户看不到激活码列表", status == 403, f"{status} {payload}")

        status, payload = await other.api("POST", "/api/auth/logout")
        check("退出登录成功", status == 200, f"{status} {payload}")
        status, payload = await other.api("GET", "/api/live/status")
        check("退出后回到未登录", status == 401, f"{status} {payload}")

        # ── 6. 同一账号只允许一处登录 ────────────────────────────────
        print("\n6. 同一账号只允许一处登录（后来者顶下去；管理员不受限）")
        device_a = Client(base)
        device_b = Client(base)
        admin_second = Client(base)
        try:
            status, payload = await admin.api(
                "POST", "/api/auth/admin/users",
                {"username": "solo", "password": "solo2026pass", "plan": "pro"})
            check("建单点登录测试账号", status == 200, f"{status} {payload}")

            status, payload = await device_a.login("solo", "solo2026pass")
            kicked_first = payload.get("data", {}).get("kicked_sessions")
            check("第一处登录成功且没有旧会话可挤",
                  status == 200 and kicked_first == 0, f"{status} kicked={kicked_first}")

            status, payload = await device_b.login("solo", "solo2026pass")
            kicked = payload.get("data", {}).get("kicked_sessions")
            check("第二处登录成功并挤掉第一处", status == 200 and kicked == 1,
                  f"{status} kicked={kicked}")

            info = await device_a.me()
            check("被挤掉的那一处已下线", info.get("authenticated") is False, str(info)[:80])
            status, _ = await device_a.api("GET", "/api/live/status")
            check("被挤掉的会话再请求接口返回 401", status == 401, f"{status}")
            info = await device_b.me()
            check("新登录的那一处仍然在线", info.get("authenticated") is True, str(info)[:80])

            status, payload = await admin_second.login("admin", "admin123456")
            check("管理员第二处登录不受限（不挤别人）",
                  status == 200 and payload.get("data", {}).get("kicked_sessions") == 0,
                  f"{status} {payload.get('data', {}).get('kicked_sessions')}")
            info = await admin.me()
            check("管理员原有会话仍有效", info.get("authenticated") is True, str(info)[:80])

            # 关掉单点登录后再验"改密码会把其他会话踢掉"这条独立机制
            os.environ["AILIVE_SINGLE_SESSION"] = "off"
            device_c = Client(base)
            try:
                status, _ = await device_c.login("solo", "solo2026pass")
                check("关掉单点登录后可以两处同时在线", status == 200, f"{status}")
                info = await device_b.me()
                check("此时旧会话没有被挤掉", info.get("authenticated") is True, str(info)[:80])
                status, payload = await device_c.api(
                    "POST", "/api/auth/password",
                    {"old_password": "solo2026pass", "new_password": "solo2027pass"})
                check("改密码成功", status == 200, f"{status} {payload}")
                status, _ = await device_b.api("GET", "/api/live/status")
                check("改密码确实会踢掉其他会话（与单点登录无关）", status == 401, f"{status}")
            finally:
                await device_c.close()
                os.environ.pop("AILIVE_SINGLE_SESSION", None)

            status, payload = await admin.api("DELETE", "/api/auth/admin/users/solo")
            check("清理单点登录测试账号", status == 200, f"{status} {payload}")
        finally:
            await device_a.close()
            await device_b.close()
            await admin_second.close()

        # ── 7. 关闭登录后的兼容行为 ──────────────────────────────────
        print("\n7. 关闭登录（AILIVE_AUTH=off）兼容旧行为")
        os.environ["AILIVE_AUTH"] = "off"
        am.set_auth_manager(None)
        offline = Client(base)
        status, payload = await offline.api("GET", "/api/live/status")
        check("关闭后无需登录即可访问", status == 200, f"{status} {payload}")
        status, payload = await offline.api("POST", "/api/live/products", {"name": "无鉴权"})
        check("关闭后写操作放行", status == 200, f"{status} {payload}")
        info = await offline.me()
        check("关闭后 me 声明功能全开",
              info.get("auth_enabled") is False and all(info.get("entitlements", {}).values()),
              str(info)[:160])
        await offline.close()
        os.environ.pop("AILIVE_AUTH", None)
        am.set_auth_manager(None)
    finally:
        await guest.close()
        await free.close()
        await admin.close()
        await other.close()
        await runner.cleanup()

    print("\n" + "=" * 62)
    if FAILED:
        print(f"❌ 冒烟未通过：{PASSED} 项通过，{len(FAILED)} 项失败")
        for item in FAILED:
            print(f"   - {item}")
        return 1
    print(f"✅ 登录与会员体系冒烟通过：{PASSED} 项检查全部成功")
    return 0


if __name__ == "__main__":
    try:
        code = asyncio.run(main())
    finally:
        shutil.rmtree(TMP_DIR, ignore_errors=True)
    raise SystemExit(code)