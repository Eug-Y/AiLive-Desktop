import asyncio
import ipaddress
import json
import time

from aiohttp import web

from server.live_studio import live_studio_manager
from server.sales_catalog import product_catalog
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
    """写操作的地域限制。

    启用登录后，访问控制由 auth 中间件按功能分级完成，这里不再重复限制；
    未启用登录时沿用旧的“仅本机/内网可写”行为。
    """
    from server.auth_manager import auth_enabled

    if auth_enabled():
        return
    if not _is_local_network(request.remote):
        raise PermissionError("商品与佣金管理只能在本机直播工作台操作")


async def _json(request):
    try:
        return await request.json()
    except Exception:
        return {}


def _can_view_internal_sales(request):
    """内部佣金/进货信息：未登录模式沿用内网判断；登录模式下仅付费会员与管理员可见。"""
    from server.auth_manager import auth_enabled, request_can

    if auth_enabled():
        return request_can(request, "product.write")
    return _is_local_network(request.remote)


async def status(request):
    return _ok(live_studio_manager.status(include_internal_sales=_can_view_internal_sales(request)))


async def register(request):
    try:
        params = await _json(request)
        return _ok(live_studio_manager.register_output(params.get("sessionid", "")))
    except Exception as exc:
        return _error(exc)


async def unregister(request):
    try:
        params = await _json(request)
        return _ok(live_studio_manager.unregister_output(params.get("sessionid", "")))
    except Exception as exc:
        return _error(exc)


async def settings(request):
    try:
        return _ok(live_studio_manager.update_settings(await _json(request)))
    except Exception as exc:
        return _error(exc)


async def competition_demo_preset(request):
    """Load synthetic competition data for a repeatable local demonstration."""
    try:
        _require_local_network(request)
        from server.competition_demo import apply_demo_preset

        return _ok(apply_demo_preset())
    except Exception as exc:
        logger.warning("Competition demo preset failed: %s", exc)
        return _error(exc)


async def scripts(request):
    try:
        params = await _json(request)
        return _ok(live_studio_manager.set_scripts(params.get("scripts", []), params.get("repeat")))
    except Exception as exc:
        return _error(exc)


async def autopilot(request):
    try:
        params = await _json(request)
        return _ok(live_studio_manager.set_autopilot(params.get("enabled", False)))
    except Exception as exc:
        return _error(exc)


async def speak(request):
    try:
        params = await _json(request)
        return _ok(live_studio_manager.speak(
            params.get("text", ""),
            params.get("mode", "echo"),
            bool(params.get("interrupt", False)),
        ))
    except Exception as exc:
        logger.warning("Live speak request failed: %s", exc)
        return _error(exc)


async def simulate(request):
    """模拟一条观众评论，返回 T1/T2 判定、将播报的话术与离线可用性（不默认播报）。"""
    try:
        params = await _json(request)
        return _ok(live_studio_manager.simulate_comment(
            params.get("text", ""),
            speak=bool(params.get("speak", False)),
            use_flavor=bool(params.get("use_flavor", True)),
            allow_llm=bool(params.get("allow_llm", True)),
        ))
    except Exception as exc:
        return _error(exc)


async def interrupt(request):
    try:
        return _ok(live_studio_manager.interrupt())
    except Exception as exc:
        return _error(exc)


async def emergency_stop(request):
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, live_studio_manager.emergency_stop)
        return _ok(result)
    except Exception as exc:
        return _error(exc)


async def recording(request):
    try:
        params = await _json(request)
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, live_studio_manager.recording, params.get("action", ""))
        return _ok(result)
    except Exception as exc:
        return _error(exc)


async def get_products(request):
    try:
        _require_local_network(request)
        return _ok(product_catalog.status())
    except Exception as exc:
        return _error(exc)


async def products(request):
    try:
        _require_local_network(request)
        return _ok(live_studio_manager.upsert_product(await _json(request)))
    except Exception as exc:
        logger.warning("Product save failed: %s", exc)
        return _error(exc)


async def delete_product(request):
    try:
        _require_local_network(request)
        return _ok(live_studio_manager.delete_product(request.match_info.get("product_id", "")))
    except Exception as exc:
        return _error(exc)


async def current_product(request):
    try:
        _require_local_network(request)
        params = await _json(request)
        return _ok(live_studio_manager.set_current_product(params.get("id", "")))
    except Exception as exc:
        return _error(exc)


async def next_product(request):
    try:
        _require_local_network(request)
        return _ok(live_studio_manager.next_product())
    except Exception as exc:
        return _error(exc)


async def product_scripts(request):
    try:
        _require_local_network(request)
        params = await _json(request)
        return _ok(live_studio_manager.generate_product_scripts(
            params.get("id", ""),
            bool(params.get("apply", True)),
        ))
    except Exception as exc:
        return _error(exc)


async def readiness(request):
    try:
        _require_local_network(request)
        return _ok(live_studio_manager.readiness())
    except Exception as exc:
        return _error(exc)


async def workflow(request):
    try:
        _require_local_network(request)
        return _ok(live_studio_manager.update_workflow(await _json(request)))
    except Exception as exc:
        return _error(exc)


async def export_snapshot(request):
    try:
        _require_local_network(request)
        payload = live_studio_manager.export_snapshot()
        filename = "ailive-affiliate-backup-" + time.strftime("%Y%m%d-%H%M%S") + ".json"
        return web.Response(
            content_type="application/json",
            text=json.dumps(payload, ensure_ascii=False, indent=2),
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as exc:
        return _error(exc)


async def mark_live_started(request):
    """标记正式开播。"""
    try:
        _require_local_network(request)
        live_studio_manager.mark_live_started()
        return _ok(live_studio_manager.status())
    except Exception as exc:
        return _error(exc)


async def mark_live_ended(request):
    """标记直播结束。"""
    try:
        _require_local_network(request)
        live_studio_manager.mark_live_ended()
        return _ok(live_studio_manager.status())
    except Exception as exc:
        return _error(exc)


# ── 排品队列 ──────────────────────────────────────────────────────────

async def play_queue_init(request):
    try:
        _require_local_network(request)
        params = await _json(request)
        return _ok(live_studio_manager.init_play_queue(params.get("mode", "loop")))
    except Exception as exc:
        return _error(exc)


async def play_queue_start(request):
    try:
        _require_local_network(request)
        return _ok(live_studio_manager.start_play_queue())
    except Exception as exc:
        return _error(exc)


async def play_queue_status(request):
    try:
        return _ok(live_studio_manager.get_play_queue_status())
    except Exception as exc:
        return _error(exc)


async def play_queue_next(request):
    try:
        _require_local_network(request)
        return _ok(live_studio_manager.next_product_in_queue())
    except Exception as exc:
        return _error(exc)


# ── 氛围管理 ──────────────────────────────────────────────────────────

async def atmosphere_start(request):
    try:
        _require_local_network(request)
        params = await _json(request)
        return _ok(live_studio_manager.start_atmosphere(int(params.get("interval_seconds", 180))))
    except Exception as exc:
        return _error(exc)


async def atmosphere_stop(request):
    try:
        _require_local_network(request)
        return _ok(live_studio_manager.stop_atmosphere())
    except Exception as exc:
        return _error(exc)


# ── 数据分析 ──────────────────────────────────────────────────────────

async def analytics(request):
    try:
        _require_local_network(request)
        payload = live_studio_manager.export_analytics()
        filename = "ailive-analytics-" + time.strftime("%Y%m%d-%H%M%S") + ".json"
        return web.Response(
            content_type="application/json",
            text=json.dumps(payload, ensure_ascii=False, indent=2),
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as exc:
        return _error(exc)


async def commission_report(request):
    try:
        _require_local_network(request)
        from server.sales_catalog import product_catalog
        return _ok(product_catalog.commission_report())
    except Exception as exc:
        return _error(exc)


async def on_shutdown(app):
    live_studio_manager.shutdown()


def setup_live_routes(app):
    app.router.add_get("/api/live/status", status)
    app.router.add_post("/api/live/register", register)
    app.router.add_post("/api/live/unregister", unregister)
    app.router.add_post("/api/live/settings", settings)
    app.router.add_post("/api/live/competition-demo", competition_demo_preset)
    app.router.add_put("/api/live/scripts", scripts)
    app.router.add_post("/api/live/autopilot", autopilot)
    app.router.add_post("/api/live/speak", speak)
    app.router.add_post("/api/live/simulate", simulate)
    app.router.add_post("/api/live/interrupt", interrupt)
    app.router.add_post("/api/live/emergency-stop", emergency_stop)
    app.router.add_post("/api/live/recording", recording)
    app.router.add_get("/api/live/products", get_products)
    app.router.add_post("/api/live/products", products)
    app.router.add_delete("/api/live/products/{product_id}", delete_product)
    app.router.add_post("/api/live/products/current", current_product)
    app.router.add_post("/api/live/products/next", next_product)
    app.router.add_post("/api/live/products/scripts", product_scripts)
    app.router.add_get("/api/live/readiness", readiness)
    app.router.add_post("/api/live/workflow", workflow)
    app.router.add_get("/api/live/export", export_snapshot)
    app.router.add_post("/api/live/started", mark_live_started)
    app.router.add_post("/api/live/ended", mark_live_ended)
    # ── 百货直播 ──
    app.router.add_post("/api/live/playqueue/init", play_queue_init)
    app.router.add_post("/api/live/playqueue/start", play_queue_start)
    app.router.add_get("/api/live/playqueue/status", play_queue_status)
    app.router.add_post("/api/live/playqueue/next", play_queue_next)
    app.router.add_post("/api/live/atmosphere/start", atmosphere_start)
    app.router.add_post("/api/live/atmosphere/stop", atmosphere_stop)
    app.router.add_get("/api/live/analytics", analytics)
    app.router.add_get("/api/live/commission", commission_report)
    app.on_shutdown.append(on_shutdown)
