"""应用装配自检：复刻 app.py 的路由 + CORS 装配，但不加载数字人模型。

存在的意义：aiohttp_cors 无法给 "*" 通配路由补预检处理器，
一旦有人用 add_route("*", ...) 注册接口，服务会在启动时崩溃（而不是请求时）。
本脚本能在 3 秒内发现这类问题。

    E:\\AiLive\\env\\python.exe tools\\smoke\\_check_app_wiring.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "LiveTalking"
sys.path.insert(0, str(ROOT))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import aiohttp_cors  # noqa: E402
from aiohttp import web  # noqa: E402

from server.routes import setup_routes  # noqa: E402


def main() -> int:
    app = web.Application(client_max_size=1024 ** 2 * 100)
    app["opt"] = None
    app["rtc_manager"] = None

    try:
        setup_routes(app)
        app.router.add_post("/offer", lambda request: web.Response(text="ok"))
        app.router.add_get("/record/{sessionid}", lambda request: web.Response(text="ok"))

        cors = aiohttp_cors.setup(app, defaults={
            "*": aiohttp_cors.ResourceOptions(
                allow_credentials=True, expose_headers="*", allow_headers="*",
            )
        })
        for route in list(app.router.routes()):
            cors.add(route)

        whep = app.router.add_resource("/whep")
        whep.add_route("POST", lambda request: web.Response(text="ok"))
        whep.add_route("OPTIONS", lambda request: web.Response(status=200))
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] 应用装配失败：{type(exc).__name__}: {exc}")
        return 1

    paths = sorted({route.resource.canonical for route in app.router.routes()})
    auth_paths = [path for path in paths if path.startswith("/api/auth")]
    print(f"[ OK ] 应用装配成功：{len(app.router.routes())} 条路由，"
          f"{len(app.middlewares)} 个中间件，登录接口 {len(auth_paths)} 条")

    if len(app.middlewares) != 1:
        print("[FAIL] 鉴权中间件数量异常，期望 1 个")
        return 1
    if "/api/auth/login" not in paths or "/api/auth/admin/users" not in paths:
        print("[FAIL] 登录相关路由缺失")
        return 1
    print("[ OK ] 鉴权中间件与登录路由均已注册")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())