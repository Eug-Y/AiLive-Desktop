import asyncio
import json

from aiohttp import web

from server.subtitle_ocr import subtitle_ocr_manager
from utils.logger import logger


def _json_ok(data=None):
    body = {"code": 0, "msg": "ok"}
    if data is not None:
        body["data"] = data
    return web.Response(content_type="application/json", text=json.dumps(body, ensure_ascii=False))


def _json_error(message, code=-1):
    return web.Response(
        content_type="application/json",
        text=json.dumps({"code": code, "msg": str(message)}, ensure_ascii=False),
    )


async def get_status(request):
    return _json_ok(subtitle_ocr_manager.status())


async def get_screenshot(request):
    try:
        loop = asyncio.get_event_loop()
        image, bounds = await loop.run_in_executor(None, subtitle_ocr_manager.capture_screen)
        return web.Response(
            body=image,
            content_type="image/png",
            headers={
                "X-Screen-Left": str(bounds["left"]),
                "X-Screen-Top": str(bounds["top"]),
                "X-Screen-Width": str(bounds["width"]),
                "X-Screen-Height": str(bounds["height"]),
                "Cache-Control": "no-store",
            },
        )
    except Exception as exc:
        logger.exception("Screen capture failed")
        return _json_error(exc)


async def start_monitor(request):
    try:
        params = await request.json()
        return _json_ok(subtitle_ocr_manager.start(params))
    except Exception as exc:
        logger.warning("Cannot start subtitle OCR: %s", exc)
        return _json_error(exc)


async def stop_monitor(request):
    try:
        loop = asyncio.get_event_loop()
        status = await loop.run_in_executor(None, subtitle_ocr_manager.stop)
        return _json_ok(status)
    except Exception as exc:
        logger.exception("Cannot stop subtitle OCR")
        return _json_error(exc)


async def stop_on_shutdown(app):
    subtitle_ocr_manager.stop()


def setup_subtitle_routes(app):
    app.router.add_get("/api/subtitle/status", get_status)
    app.router.add_get("/api/subtitle/screenshot", get_screenshot)
    app.router.add_post("/api/subtitle/start", start_monitor)
    app.router.add_post("/api/subtitle/stop", stop_monitor)
    app.on_shutdown.append(stop_on_shutdown)
