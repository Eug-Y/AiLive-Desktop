import ipaddress
import json
import asyncio
from functools import partial

from aiohttp import web

from llm import configure_llm, get_llm_public_config, llm_response, set_llm_api_key
from utils.logger import logger


def _json_ok(data=None):
    body = {"code": 0, "msg": "ok"}
    if data is not None:
        body["data"] = data
    return web.Response(content_type="application/json", text=json.dumps(body, ensure_ascii=False))


def _json_error(message, code=-1, status=200):
    return web.Response(
        status=status,
        content_type="application/json",
        text=json.dumps({"code": code, "msg": str(message)}, ensure_ascii=False),
    )


def _is_loopback(remote):
    if not remote:
        return False
    try:
        return ipaddress.ip_address(remote.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


async def get_config(request):
    return _json_ok(get_llm_public_config())


async def update_config(request):
    try:
        params = await request.json()
        return _json_ok(configure_llm(params))
    except Exception as exc:
        logger.warning("Invalid LLM configuration: %s", exc)
        return _json_error(exc)


async def update_credential(request):
    # API keys may only be changed from the same computer, or by a logged-in
    # administrator when the login system is enabled. They are never returned.
    if not _is_loopback(request.remote):
        from server.auth_manager import auth_enabled, request_can
        if not (auth_enabled() and request_can(request, "llm.config")):
            return _json_error("API Key 只能在本机页面中设置", code=403, status=403)
    try:
        params = await request.json()
        return _json_ok(set_llm_api_key(params.get("provider", ""), params.get("api_key", "")))
    except Exception as exc:
        logger.warning("Failed to update LLM credential: %s", exc)
        return _json_error(exc)


async def test_model(request):
    try:
        params = await request.json()
        message = str(params.get("message", "你好，请用一句话介绍你自己。")).strip()
        if not message:
            return _json_error("测试内容不能为空")
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            partial(llm_response, message, None, {}, True),
        )
        return _json_ok({"response": response})
    except Exception as exc:
        logger.warning("LLM test failed: %s", exc)
        return _json_error(exc)


def setup_llm_routes(app):
    app.router.add_get("/api/llm/config", get_config)
    app.router.add_post("/api/llm/config", update_config)
    app.router.add_post("/api/llm/credential", update_credential)
    app.router.add_post("/api/llm/test", test_model)
