"""登录 / 会员体系的 HTTP 层：中间件鉴权、功能分级、账号与激活码 API。

中间件规则：
  * 登录功能关闭（``AILIVE_AUTH=off`` 或 config.enabled=false）时完全放行，保持旧行为；
  * 公开路径（登录页、登录注册接口、静态资源）放行；
  * 其余请求必须先登录，否则页面 302 到 /login.html，接口返回 401；
  * 命中 ROUTE_RULES / PAGE_RULES 的请求再做功能分级：
      - 需要专业版但用户是免费/已过期 → 页面 302 到 /upgrade.html，接口 403 并带购买提示；
      - 需要管理员但用户不是管理员 → 页面 302 到 /forbidden.html，接口 403。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional
from urllib.parse import quote, urlencode

from aiohttp import web

from server.auth_manager import (
    FEATURES,
    PLANS,
    SESSION_COOKIE,
    SESSION_TTL_SECONDS,
    AuthError,
    auth_enabled,
    get_auth_manager,
    is_public_path,
    match_page_rule,
    match_route_rule,
    request_token,
    request_user,
)
from utils.logger import logger


# ────────────────────────────── 响应工具 ──────────────────────────────


def _ok(data: Any = None, message: str = "ok") -> web.Response:
    body: Dict[str, Any] = {"code": 0, "msg": message}
    if data is not None:
        body["data"] = data
    return web.json_response(body)


def _fail(message: str, code: int = -1, status: int = 200, **extra: Any) -> web.Response:
    body: Dict[str, Any] = {"code": code, "msg": str(message)}
    body.update(extra)
    return web.json_response(body, status=status)


def _set_session_cookie(response: web.StreamResponse, token: str) -> None:
    # 局域网多为 http 访问，因此不能加 secure=True，否则手机端登录态会失效。
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="Lax",
        path="/",
    )


def _clear_session_cookie(response: web.StreamResponse) -> None:
    response.del_cookie(SESSION_COOKIE, path="/")


async def _body(request: web.Request) -> Dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _client_ip(request: web.Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote or ""


# ────────────────────────────── 中间件 ──────────────────────────────


def _wants_html(request: web.Request) -> bool:
    path = request.path
    if path.endswith((".html", ".htm")):
        return True
    if path == "/":
        return True
    return "text/html" in request.headers.get("Accept", "")


def _login_redirect(request: web.Request) -> web.HTTPFound:
    target = request.path
    if request.query_string:
        target = f"{target}?{request.query_string}"
    url = "/login.html" + ("?" + urlencode({"next": target}) if target and target != "/" else "")
    return web.HTTPFound(url)


@web.middleware
async def auth_middleware(request: web.Request, handler):
    manager = get_auth_manager()
    if not manager.enabled or request.method == "OPTIONS":
        return await handler(request)

    user = manager.session_user(request_token(request))
    request["auth_user"] = user

    if is_public_path(request.path, request.method):
        return await handler(request)

    if user is None:
        if _wants_html(request):
            raise _login_redirect(request)
        return _fail("请先登录", code=401, status=401, login_url="/login.html")

    if request.path.endswith((".html", ".htm")) or request.path == "/":
        _, feature = match_page_rule(request.path)
    else:
        _, feature = match_route_rule(request.method, request.path)

    if not feature or manager.has_feature(user, feature):
        return await handler(request)

    hint = manager.upgrade_hint(feature)
    view = manager.public_user(user) or {}
    upgrade_needed = hint["required_plan"] == "pro" and not view.get("is_admin")
    message = (
        f"“{hint['label']}”属于{hint['required_plan_label']}功能，"
        + ("请购买/续费后使用" if upgrade_needed else "仅管理员可用")
    )
    logger.info(
        "[AUTH] 拒绝 %s %s（用户=%s 功能=%s）",
        request.method, request.path, view.get("username"), feature,
    )
    if _wants_html(request):
        if upgrade_needed:
            params = {"feature": feature}
            if request.path.endswith((".html", ".htm")):
                params["next"] = request.path
            raise web.HTTPFound("/upgrade.html?" + urlencode(params))
        raise web.HTTPFound("/forbidden.html?" + urlencode({"feature": feature}))
    return _fail(
        message,
        code=403,
        status=403,
        feature=feature,
        required_plan=hint["required_plan"],
        required_plan_label=hint["required_plan_label"],
        plan_expired=bool(view.get("plan_expired")),
        upgrade_url=f"/upgrade.html?feature={quote(feature)}",
    )


# ────────────────────────────── 公开接口 ──────────────────────────────


def _catalog(manager) -> Dict[str, Any]:
    return {
        "plans": PLANS,
        "features": FEATURES,
        "config": {
            "contact": manager.config.get("contact", ""),
            "purchase_note": manager.config.get("purchase_note", ""),
            "pro_days_default": manager.config.get("pro_days_default", 30),
        },
        "allow_register": manager.allow_register,
        "single_session": manager.single_session,
        "auth_enabled": manager.enabled,
    }


async def plans(request: web.Request) -> web.Response:
    return _ok(_catalog(get_auth_manager()))


async def me(request: web.Request) -> web.Response:
    manager = get_auth_manager()
    if not manager.enabled:
        return _ok({
            "auth_enabled": False,
            "authenticated": True,
            "user": None,
            "entitlements": {key: True for key in FEATURES},
            **_catalog(manager),
        })
    user = request_user(request)
    view = manager.public_user(user)
    return _ok({
        "auth_enabled": True,
        "authenticated": view is not None,
        "user": view,
        "entitlements": manager.entitlements(user),
        "must_change_password": bool(view and view.get("must_change_password")),
        **_catalog(manager),
    })


async def register(request: web.Request) -> web.Response:
    manager = get_auth_manager()
    if not manager.enabled:
        return _fail("登录功能当前已关闭", code=403, status=403)
    payload = await _body(request)
    try:
        view = manager.register(
            payload.get("username", ""),
            payload.get("password", ""),
            payload.get("display_name", ""),
            remote=_client_ip(request),
        )
        logger.info("[AUTH] 新用户注册：%s", view["username"])
    except AuthError as exc:
        return _fail(exc.message, code=exc.code, status=exc.status)
    # 注册即登录，省一次输入
    _, token, _kicked = manager.login(
        payload.get("username", ""),
        payload.get("password", ""),
        remote=_client_ip(request),
        user_agent=request.headers.get("User-Agent", ""),
    )
    response = _ok({"user": view, "entitlements": manager.entitlements(manager.get_user(view["username"]))},
                   message="注册成功，已自动登录")
    _set_session_cookie(response, token)
    return response


async def login(request: web.Request) -> web.Response:
    manager = get_auth_manager()
    if not manager.enabled:
        return _fail("登录功能当前已关闭", code=403, status=403)
    payload = await _body(request)
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    try:
        view, token, kicked = manager.login(
            username, password,
            remote=_client_ip(request),
            user_agent=request.headers.get("User-Agent", ""),
        )
    except AuthError as exc:
        logger.info("[AUTH] 登录失败：%s（%s）", username, exc.message)
        return _fail(exc.message, code=exc.code, status=exc.status)
    if kicked:
        logger.info("[AUTH] %s 在其他设备上的 %d 个会话已作废（同一账号只允许一处在线）",
                    view["username"], kicked)
    logger.info("[AUTH] 登录成功：%s（%s）", view["username"], view["plan_label"])
    message = "登录成功"
    if kicked:
        message = f"登录成功；该账号在其他设备上的 {kicked} 处登录已退出"
    response = _ok({"user": view,
                    "entitlements": manager.entitlements(manager.get_user(username)),
                    "kicked_sessions": kicked},
                   message=message)
    _set_session_cookie(response, token)
    return response


async def logout(request: web.Request) -> web.Response:
    manager = get_auth_manager()
    manager.logout(request_token(request))
    response = _ok({"authenticated": False}, message="已退出登录")
    _clear_session_cookie(response)
    return response


async def change_password(request: web.Request) -> web.Response:
    manager = get_auth_manager()
    user = request_user(request)
    if not user:
        return _fail("请先登录", code=401, status=401)
    payload = await _body(request)
    try:
        manager.change_password(
            user["username"],
            str(payload.get("old_password", "")),
            str(payload.get("new_password", "")),
            token=request_token(request),
        )
    except AuthError as exc:
        return _fail(exc.message, code=exc.code, status=exc.status)
    logger.info("[AUTH] 用户 %s 修改了密码", user["username"])
    return _ok({"user": manager.public_user(manager.get_user(user["username"]))}, message="密码已更新")


async def redeem(request: web.Request) -> web.Response:
    manager = get_auth_manager()
    user = request_user(request)
    if not user:
        return _fail("请先登录", code=401, status=401)
    payload = await _body(request)
    try:
        result = manager.redeem(user["username"], payload.get("code", ""))
    except AuthError as exc:
        return _fail(exc.message, code=exc.code, status=exc.status)
    logger.info("[AUTH] 用户 %s 兑换成功：%s（%s 天）",
                user["username"], result["code"], result["days"])
    return _ok({
        "user": result["user"],
        "code": result["code"],
        "days": result["days"],
        "plan_expires_at": result["plan_expires_at"],
        "entitlements": manager.entitlements(manager.get_user(user["username"])),
    }, message=f"兑换成功，已解锁 {result['days']} 天专业版")


# ────────────────────────────── 管理员接口 ──────────────────────────────


def _require_admin(request: web.Request) -> Optional[web.Response]:
    manager = get_auth_manager()
    user = request_user(request)
    if not user:
        return _fail("请先登录", code=401, status=401)
    if not manager.has_feature(user, "admin.users"):
        return _fail("仅管理员可管理用户", code=403, status=403)
    return None


async def admin_stats(request: web.Request) -> web.Response:
    denied = _require_admin(request)
    if denied:
        return denied
    return _ok(get_auth_manager().stats())


async def admin_users(request: web.Request) -> web.Response:
    denied = _require_admin(request)
    if denied:
        return denied
    manager = get_auth_manager()
    if request.method == "GET":
        return _ok({"users": manager.list_users(), "stats": manager.stats()})
    payload = await _body(request)
    try:
        view = manager.register(
            payload.get("username", ""),
            payload.get("password", ""),
            payload.get("display_name", ""),
            remote=_client_ip(request),
            invite="admin",
        )
        plan = str(payload.get("plan", "free"))
        role = str(payload.get("role", "user"))
        if plan != "free" or role != "user":
            view = manager.update_user(view["username"], {
                "plan": plan,
                "role": role,
                "days": payload.get("days"),
                "note": payload.get("note", ""),
            }, by=request_user(request)["username"])
    except AuthError as exc:
        return _fail(exc.message, code=exc.code, status=exc.status)
    logger.info("[AUTH] 管理员创建用户 %s（%s / %s）", view["username"], view["role"], view["plan"])
    return _ok({"user": view}, message=f"已创建用户 {view['username']}")


async def admin_user_detail(request: web.Request) -> web.Response:
    denied = _require_admin(request)
    if denied:
        return denied
    manager = get_auth_manager()
    username = request.match_info.get("username", "")
    operator = request_user(request)["username"]
    try:
        if request.method == "DELETE":
            target = manager.public_user(manager.get_user(username))
            if target and target["username"].lower() == operator.lower():
                return _fail("不能删除当前登录的管理员账号", code=-6, status=400)
            manager.delete_user(username)
            logger.info("[AUTH] 管理员 %s 删除了用户 %s", operator, username)
            return _ok({"deleted": username}, message=f"已删除 {username}")
        payload = await _body(request)
        patch = {key: payload[key] for key in
                 ("display_name", "note", "disabled", "role", "plan", "days",
                  "plan_expires_at", "password")
                 if key in payload}
        view = manager.update_user(username, patch, by=operator)
    except AuthError as exc:
        return _fail(exc.message, code=exc.code, status=exc.status)
    logger.info("[AUTH] 管理员 %s 更新用户 %s：%s", operator, username, list(patch.keys()))
    return _ok({"user": view}, message=f"已更新 {view['username']}")


async def admin_codes(request: web.Request) -> web.Response:
    denied = _require_admin(request)
    if denied:
        return denied
    manager = get_auth_manager()
    if request.method == "GET":
        return _ok({"codes": manager.list_codes()})
    payload = await _body(request)
    try:
        created = manager.create_codes(
            plan=str(payload.get("plan", "pro")),
            days=int(payload.get("days") or manager.config.get("pro_days_default", 30)),
            count=int(payload.get("count") or 1),
            max_uses=int(payload.get("max_uses") or 1),
            valid_days=int(payload.get("valid_days") or 0),
            note=str(payload.get("note", "")),
            created_by=request_user(request)["username"],
        )
    except (AuthError, ValueError) as exc:
        message = exc.message if isinstance(exc, AuthError) else str(exc)
        return _fail(message, code=-2, status=400)
    logger.info("[AUTH] 生成 %d 个激活码（%s / %d 天）", len(created), created[0]["plan"], created[0]["days"])
    return _ok({"codes": created}, message=f"已生成 {len(created)} 个激活码")


async def admin_code_revoke(request: web.Request) -> web.Response:
    denied = _require_admin(request)
    if denied:
        return denied
    payload = await _body(request)
    try:
        record = get_auth_manager().revoke_code(payload.get("code", ""))
    except AuthError as exc:
        return _fail(exc.message, code=exc.code, status=exc.status)
    return _ok({"code": record}, message=f"已作废 {record['code']}")


async def admin_config(request: web.Request) -> web.Response:
    denied = _require_admin(request)
    if denied:
        return denied
    manager = get_auth_manager()
    if request.method == "GET":
        return _ok({"config": manager.config, "stats": manager.stats()})
    payload = await _body(request)
    config = manager.save_config(payload if isinstance(payload, dict) else {})
    logger.info("[AUTH] 管理员更新登录配置：%s",
                {k: config.get(k) for k in ("enabled", "allow_register", "single_session")})
    return _ok({"config": config}, message="配置已保存")


async def admin_sessions(request: web.Request) -> web.Response:
    denied = _require_admin(request)
    if denied:
        return denied
    manager = get_auth_manager()
    payload = await _body(request)
    username = str(payload.get("username", "")).strip()
    if not username:
        return _fail("请指定用户名", code=-2, status=400)
    count = manager.logout_user_everywhere(username)
    return _ok({"revoked": count}, message=f"已强制 {username} 下线（{count} 个会话）")


# ────────────────────────────── 注册 ──────────────────────────────


def is_html_page_path(path: str) -> bool:
    return path == "/" or path.lower().endswith((".html", ".htm"))


async def no_store_html_pages(request: web.Request, response: web.StreamResponse) -> None:
    """HTML 页面一律不缓存。

    静态文件默认只带 ETag/Last-Modified，浏览器会"启发式缓存"HTML：
    管理员访问过 /admin_users.html 后，同一浏览器里的普通会员再打开该地址，
    浏览器可能直接拿缓存里的页面外壳，**完全不请求服务器**，页面级鉴权就被绕过了。
    接口鉴权仍然有效（拿不到数据），但页面壳不该漏。这里在响应发出前统一加 no-store。
    """
    if is_html_page_path(request.path):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"


def setup_auth_routes(app: web.Application) -> None:
    """注册鉴权中间件与登录相关路由。必须在应用启动前调用。"""
    manager = get_auth_manager()
    bootstrap = manager.ensure_bootstrap()

    if manager.enabled:
        app.middlewares.append(auth_middleware)
        logger.info("[AUTH] 登录与会员体系已启用（存储目录：%s）", manager.data_dir)
        if bootstrap.get("created"):
            logger.warning(
                "[AUTH] 已创建初始管理员：%s，密码：%s%s",
                bootstrap.get("username"),
                "admin123456（默认）" if bootstrap.get("default_password") else "（来自 AILIVE_ADMIN_PASSWORD）",
                "，请登录后立即修改！" if bootstrap.get("default_password") else "",
            )
    else:
        logger.warning("[AUTH] 登录功能已关闭（AILIVE_AUTH=off 或 config.enabled=false），所有接口不校验登录")

    app["auth_manager"] = manager
    app.on_response_prepare.append(no_store_html_pages)

    # 公开
    app.router.add_get("/api/auth/plans", plans)
    app.router.add_get("/api/auth/me", me)
    app.router.add_post("/api/auth/login", login)
    app.router.add_post("/api/auth/register", register)
    app.router.add_post("/api/auth/logout", logout)
    # 登录用户
    app.router.add_post("/api/auth/password", change_password)
    app.router.add_post("/api/auth/redeem", redeem)
    # 管理员（逐方法注册：aiohttp_cors 无法给 "*" 通配路由补预检处理器，会直接启动失败）
    app.router.add_get("/api/auth/admin/stats", admin_stats)
    app.router.add_get("/api/auth/admin/users", admin_users)
    app.router.add_post("/api/auth/admin/users", admin_users)
    app.router.add_put("/api/auth/admin/users/{username}", admin_user_detail)
    app.router.add_delete("/api/auth/admin/users/{username}", admin_user_detail)
    app.router.add_get("/api/auth/admin/codes", admin_codes)
    app.router.add_post("/api/auth/admin/codes", admin_codes)
    app.router.add_post("/api/auth/admin/codes/revoke", admin_code_revoke)
    app.router.add_get("/api/auth/admin/config", admin_config)
    app.router.add_post("/api/auth/admin/config", admin_config)
    app.router.add_post("/api/auth/admin/sessions/revoke", admin_sessions)