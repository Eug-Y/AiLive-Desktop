###############################################################################
#  账号管理 API 路由
###############################################################################

import ipaddress
import json
import time
from aiohttp import web

from server.account_manager import account_manager, PLATFORM_LABELS
from utils.logger import logger


def _ok(data=None):
    body = {"code": 0, "msg": "ok"}
    if data is not None:
        body["data"] = data
    return web.Response(content_type="application/json", text=json.dumps(body, ensure_ascii=False))


def _error(message):
    return web.Response(
        content_type="application/json",
        text=json.dumps({"code": -1, "msg": str(message)}, ensure_ascii=False),
    )


def _is_local_network(remote):
    if not remote:
        return False
    try:
        address = ipaddress.ip_address(remote.split("%", 1)[0])
        return address.is_loopback or address.is_private
    except ValueError:
        return False


def _require_local_network(request):
    """写操作的地域限制：启用登录后由 auth 中间件按功能分级接管。"""
    from server.auth_manager import auth_enabled

    if auth_enabled():
        return
    if not _is_local_network(request.remote):
        raise PermissionError("账号管理只能在本机操作")


async def _json(request):
    try:
        return await request.json()
    except Exception:
        return {}


# ── 账号列表 ──────────────────────────────────────────────────────────

async def list_accounts(request):
    """获取所有账号列表。"""
    try:
        return _ok(account_manager.status())
    except Exception as exc:
        return _error(exc)


# ── 创建账号 ──────────────────────────────────────────────────────────

async def create_account(request):
    """创建新账号。"""
    try:
        _require_local_network(request)
        params = await _json(request)
        account = account_manager.create(params)
        return _ok(account.to_dict())
    except Exception as exc:
        return _error(exc)


# ── 获取账号详情 ──────────────────────────────────────────────────────

async def get_account(request):
    """获取指定账号详情。"""
    try:
        account_id = request.match_info.get("account_id", "")
        account = account_manager.get(account_id)
        if not account:
            return _error("账号不存在")
        return _ok(account.to_dict())
    except Exception as exc:
        return _error(exc)


# ── 更新账号 ──────────────────────────────────────────────────────────

async def update_account(request):
    """更新账号信息。"""
    try:
        _require_local_network(request)
        account_id = request.match_info.get("account_id", "")
        params = await _json(request)
        account = account_manager.update(account_id, params)
        return _ok(account.to_dict())
    except Exception as exc:
        return _error(exc)


# ── 删除账号 ──────────────────────────────────────────────────────────

async def delete_account(request):
    """删除账号。"""
    try:
        _require_local_network(request)
        account_id = request.match_info.get("account_id", "")
        result = account_manager.delete(account_id)
        return _ok(result)
    except Exception as exc:
        return _error(exc)


# ── 切换账号 ──────────────────────────────────────────────────────────

async def switch_account(request):
    """切换到指定账号。"""
    try:
        _require_local_network(request)
        params = await _json(request)
        account_id = params.get("account_id", "")
        account = account_manager.switch_to(account_id)
        return _ok(account.to_dict())
    except Exception as exc:
        return _error(exc)


# ── 获取当前活跃账号 ──────────────────────────────────────────────────

async def active_account(request):
    """获取当前活跃账号。"""
    try:
        account = account_manager.active_account
        return _ok(account.to_dict() if account else None)
    except Exception as exc:
        return _error(exc)


# ── 导出账号数据 ──────────────────────────────────────────────────────

async def export_account(request):
    """导出账号完整数据。"""
    try:
        _require_local_network(request)
        account_id = request.match_info.get("account_id", "")
        payload = account_manager.export_account(account_id)
        filename = f"ailive-account-{account_id}-{time.strftime('%Y%m%d-%H%M%S')}.json"
        return web.Response(
            content_type="application/json",
            text=json.dumps(payload, ensure_ascii=False, indent=2),
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as exc:
        return _error(exc)


# ── 导入账号数据 ──────────────────────────────────────────────────────

async def import_account(request):
    """导入账号数据。"""
    try:
        _require_local_network(request)
        params = await _json(request)
        data = params.get("data", params)
        overwrite = bool(params.get("overwrite", False))
        account = account_manager.import_account(data, overwrite=overwrite)
        return _ok(account.to_dict())
    except Exception as exc:
        return _error(exc)


# ── 平台列表 ──────────────────────────────────────────────────────────

async def platforms(request):
    """获取支持的平台列表。"""
    return _ok(PLATFORM_LABELS)


# ── 路由注册 ──────────────────────────────────────────────────────────

def setup_account_routes(app):
    app.router.add_get("/api/accounts", list_accounts)
    app.router.add_get("/api/accounts/active", active_account)
    app.router.add_get("/api/accounts/platforms", platforms)
    app.router.add_post("/api/accounts", create_account)
    app.router.add_get("/api/accounts/{account_id}", get_account)
    app.router.add_put("/api/accounts/{account_id}", update_account)
    app.router.add_delete("/api/accounts/{account_id}", delete_account)
    app.router.add_post("/api/accounts/switch", switch_account)
    app.router.add_get("/api/accounts/{account_id}/export", export_account)
    app.router.add_post("/api/accounts/import", import_account)
    logger.info("Account routes registered")
