###############################################################################
#  服务器路由 — 统一异常处理的 API 路由
###############################################################################

import asyncio
import ipaddress
import json
import socket
from aiohttp import web

from utils.logger import logger


# ─── 路由工具函数 ──────────────────────────────────────────────────────────

def json_ok(data=None):
    """返回成功 JSON 响应"""
    body = {"code": 0, "msg": "ok"}
    if data is not None:
        body["data"] = data
    return web.Response(
        content_type="application/json",
        text=json.dumps(body),
    )


def json_error(msg: str, code: int = -1):
    """返回错误 JSON 响应"""
    return web.Response(
        content_type="application/json",
        text=json.dumps({"code": code, "msg": str(msg)}, ensure_ascii=False),
    )


async def favicon(request):
    """Avoid a noisy 404 when browsers request the conventional favicon."""
    return web.Response(status=204)


def _lan_ipv4_addresses():
    """Return usable private IPv4 addresses for same-network device access."""
    addresses = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            addresses.add(info[4][0])
    except OSError:
        pass

    # This resolves the interface actually used for outbound LAN traffic on
    # machines whose hostname resolves only to 127.0.0.1.
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 80))
        addresses.add(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()

    usable = []
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
            if address.version == 4 and address.is_private and not address.is_loopback:
                usable.append(str(address))
        except ValueError:
            continue
    return sorted(set(usable))


async def system_access(request):
    """Return browser URLs for desktop and same-LAN mobile devices."""
    opt = request.app.get("opt")
    port = int(getattr(opt, "listenport", 8010))
    lan_urls = [
        f"http://{address}:{port}/live_console.html"
        for address in _lan_ipv4_addresses()
    ]
    return json_ok(data={
        "desktop_url": f"http://127.0.0.1:{port}/live_console.html",
        "lan_urls": lan_urls,
        "port": port,
        "same_network_only": True,
    })


from server.session_manager import session_manager
from server.avatar_routes import setup_avatar_routes
from server.llm_routes import setup_llm_routes
from server.subtitle_routes import setup_subtitle_routes
from server.live_routes import setup_live_routes
from server.workflow import workflow


def get_session(request, sessionid: str):
    """从 session_manager 获取 session 实例"""
    return session_manager.get_session(sessionid)


# ─── 路由处理函数 ──────────────────────────────────────────────────────────

async def human(request):
    """文本输入（echo/chat 模式），支持 voice/emotion 参数"""
    try:
        params: dict = await request.json()
        if not isinstance(params, dict):
            return json_error("request body must be a JSON object", -2)

        sessionid: str = params.get('sessionid', '')
        text = str(params.get('text', '') or '').strip()
        message_type = params.get('type', 'echo')
        if not sessionid:
            return json_error("sessionid is required", -2)
        if not text:
            return json_error("text is required", -2)
        if len(text) > 2000:
            return json_error("text is too long (maximum 2000 characters)", -2)
        if message_type not in {'echo', 'chat'}:
            return json_error("type must be echo or chat", -2)

        avatar_session = get_session(request, sessionid)
        if avatar_session is None:
            return json_error("session not found")

        if params.get('interrupt'):
            avatar_session.flush_talk()

        datainfo = {}
        if params.get('tts'):  # tts 参数透传（voice, emotion 等）
            datainfo['tts'] = params.get('tts')

        if message_type == 'echo':
            avatar_session.put_msg_txt(text, datainfo)
        elif message_type == 'chat':
            llm_response = request.app.get("llm_response")
            if llm_response:
                # 使用 LLM 专用线程池，避免阻塞默认 executor
                from llm import get_llm_executor
                executor = get_llm_executor()
                asyncio.get_event_loop().run_in_executor(
                    executor, llm_response, text, avatar_session, datainfo
                )
            else:
                return json_error("LLM is not configured")

        return json_ok()
    except Exception as e:
        logger.exception('human route exception:')
        return json_error(str(e))


async def interrupt_talk(request):
    """打断当前说话"""
    try:
        params = await request.json()
        sessionid = params.get('sessionid', '')
        avatar_session = get_session(request, sessionid)
        if avatar_session is None:
            return json_error("session not found")
        avatar_session.flush_talk()
        return json_ok()
    except Exception as e:
        logger.exception('interrupt_talk exception:')
        return json_error(str(e))


async def humanaudio(request):
    """上传音频文件"""
    try:
        form = await request.post()
        sessionid = str(form.get('sessionid', ''))
        fileobj = form.get("file")
        if not sessionid:
            return json_error("sessionid is required", -2)
        if fileobj is None or not getattr(fileobj, "file", None):
            return json_error("audio file is required", -2)
        filebytes = fileobj.file.read()
        if not filebytes:
            return json_error("audio file is empty", -2)

        datainfo = {}

        avatar_session = get_session(request, sessionid)
        if avatar_session is None:
            return json_error("session not found")
        avatar_session.put_audio_file(filebytes, datainfo)
        return json_ok()
    except Exception as e:
        logger.exception('humanaudio exception:')
        return json_error(str(e))


async def set_audiotype(request):
    """设置自定义状态（动作编排）"""
    try:
        params = await request.json()
        sessionid = params.get('sessionid', '')
        avatar_session = get_session(request, sessionid)
        if avatar_session is None:
            return json_error("session not found")
        avatar_session.set_custom_state(params['audiotype'])
        return json_ok()
    except Exception as e:
        logger.exception('set_audiotype exception:')
        return json_error(str(e))


async def record(request):
    """录制控制"""
    try:
        params = await request.json()
        sessionid = params.get('sessionid', '')
        avatar_session = get_session(request, sessionid)
        if avatar_session is None:
            return json_error("session not found")
        if params['type'] == 'start_record':
            avatar_session.start_recording()
        elif params['type'] == 'end_record':
            avatar_session.stop_recording()
        return json_ok()
    except Exception as e:
        logger.exception('record exception:')
        return json_error(str(e))


async def is_speaking(request):
    """查询是否正在说话"""
    try:
        params = await request.json()
        sessionid = params.get('sessionid', '') if isinstance(params, dict) else ''
        if not sessionid:
            return json_error("sessionid is required", -2)
        avatar_session = get_session(request, sessionid)
        if avatar_session is None:
            return json_error("session not found")
        return json_ok(data=avatar_session.is_speaking())
    except Exception as e:
        logger.exception('is_speaking exception:')
        return json_error(str(e))


async def sse_handler(request):
    """SSE 事件流，推送服务器状态更新到客户端。

    优化：使用 asyncio.Queue 替代 queue.Queue + 忙等轮询。
    """
    sessionid = request.query.get('sessionid', '')
    avatar_session = session_manager.get_session(sessionid)
    if avatar_session is None:
        return json_error("session not found")

    response = web.StreamResponse(
        status=200,
        reason='OK',
        headers={
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'Access-Control-Allow-Origin': '*',
        }
    )
    await response.prepare(request)

    # 使用 asyncio.Queue 避免忙等轮询
    msgqueue: asyncio.Queue = asyncio.Queue(maxsize=256)
    avatar_session.add_msgqueue(msgqueue)

    try:
        while True:
            try:
                msg = await asyncio.wait_for(msgqueue.get(), timeout=30.0)
                await response.write(f"data: {msg}\n\n".encode('utf-8'))
            except asyncio.TimeoutError:
                # 发送心跳保持连接
                await response.write(b": heartbeat\n\n")
    except (asyncio.CancelledError, ConnectionResetError):
        logger.info('SSE connection closed for session: %s', sessionid)
    finally:
        if msgqueue in avatar_session.msgqueues:
            avatar_session.msgqueues.remove(msgqueue)

    return response


async def health_handler(request):
    """健康检查端点 — 返回服务状态、GPU、会话、LLM 等信息。"""
    try:
        from server.context import get_context
        ctx = get_context()
        if ctx:
            report = ctx.health_report()
        else:
            report = {"status": "ok", "note": "AppContext not yet initialized"}
        return web.Response(
            content_type="application/json",
            text=json.dumps(report),
        )
    except Exception as e:
        logger.exception('health check exception:')
        return web.Response(
            content_type="application/json",
            text=json.dumps({"status": "error", "detail": str(e)}),
            status=500,
        )


async def admin_config(request):
    """Admin: 获取全局配置参数"""
    try:
        opt = request.app.get("opt")
        if opt:
            return json_ok(data={"config": vars(opt)})
        return json_error("Config not found")
    except Exception as e:
        logger.exception('admin_config exception:')
        return json_error(str(e))


async def admin_sessions(request):
    """Admin: 获取活跃的会话及其配置"""
    try:
        sessions_info = []
        for sid, avatar_session in session_manager.sessions.items():
            if avatar_session:
                s_opt = getattr(avatar_session, 'opt', None)
                s_data = {
                    "sessionid": sid,
                    "speaking": avatar_session.is_speaking() if hasattr(avatar_session, 'is_speaking') else False,
                    "recording": getattr(avatar_session, 'recording', False),
                }
                if s_opt:
                    s_data.update({
                        "model": getattr(s_opt, "model", ""),
                        "avatar_id": getattr(s_opt, "avatar_id", ""),
                        "REF_FILE": getattr(s_opt, "REF_FILE", ""),
                        "transport": getattr(s_opt, "transport", ""),
                        "batch_size": getattr(s_opt, "batch_size", 0),
                        "customopt": getattr(s_opt, "customopt", []),
                    })
                sessions_info.append(s_data)
        return json_ok(data={"sessions": sessions_info})
    except Exception as e:
        logger.exception('admin_sessions exception:')
        return json_error(str(e))


# ─── 路由注册 ──────────────────────────────────────────────────────────────

async def _workflow_start(request):
    """一键开播"""
    try:
        params = await request.json() if request.can_read_body else {}
    except Exception:
        params = {}
    result = workflow.start_live(params)
    return web.Response(content_type="application/json",
                        text=json.dumps(result, ensure_ascii=False))


async def _workflow_stop(request):
    """一键下播"""
    result = workflow.stop_live()
    return web.Response(content_type="application/json",
                        text=json.dumps(result, ensure_ascii=False))


def _default_page(request) -> str:
    """当前传输下的默认页面（产品化入口）。"""
    opt = request.app.get("opt")
    transport = getattr(opt, "transport", "") if opt else ""
    if transport == 'webrtc':
        # v1.3.2：网页端默认也进统一外壳 app.html（与桌面端同一份界面），
        # 不再把用户直接丢进旧的单页工作台 live_console.html。
        return 'app.html'
    if transport == 'rtmp':
        return 'rtmpapi.html'
    if transport == 'rtcpush':
        return 'rtcpushapi.html'
    return 'index.html'


async def index(request):
    """产品化默认入口：当前传输的默认页（webrtc 下是统一外壳 app.html）。"""
    raise web.HTTPFound('/' + _default_page(request))


async def legacy_desktop(request):
    """v1.3.2：旧的“仿 Windows 桌面”页（web/desktop.html）已退役。

    老链接 /desktop.html 302 到当前传输的默认页，避免网页端同时存在两套界面
    （桌面端宿主本来就把 /desktop.html 映射到自带的统一外壳，这里与它对齐）。
    页面文件仍留在 web/ 目录里仅作历史留档，不再由服务端提供。
    """
    raise web.HTTPFound('/' + _default_page(request))


def setup_routes(app):
    """注册所有路由到 aiohttp app"""
    # ── 登录与会员体系：中间件必须先注册，才能覆盖后面所有路由 ──
    from server.auth_routes import setup_auth_routes
    setup_auth_routes(app)

    app.router.add_get("/", index)
    app.router.add_get("/desktop.html", legacy_desktop)
    app.router.add_get("/favicon.ico", favicon)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/api/system/access", system_access)
    app.router.add_post("/human", human)
    app.router.add_post("/humanaudio", humanaudio)
    app.router.add_post("/set_audiotype", set_audiotype)
    app.router.add_post("/record", record)
    app.router.add_post("/interrupt_talk", interrupt_talk)
    app.router.add_post("/is_speaking", is_speaking)
    app.router.add_get("/api/admin/config", admin_config)
    app.router.add_get("/api/admin/sessions", admin_sessions)
    app.router.add_get('/sse', sse_handler)

    # ── Local ASR endpoint (SenseVoice/FunASR) ──
    try:
        from server.asr_server import asr_websocket_handler, is_funasr_available
        if is_funasr_available():
            app.router.add_get("/api/asr", asr_websocket_handler)
            logger.info("[ASR] Local SenseVoice ASR endpoint enabled at /api/asr")
        else:
            logger.info("[ASR] funasr not installed — local ASR endpoint disabled "
                        "(pip install funasr modelscope)")
    except Exception as e:
        logger.warning(f"[ASR] Failed to register ASR endpoint: {e}")

    # 注册子模块路由
    setup_avatar_routes(app)
    setup_llm_routes(app)
    setup_subtitle_routes(app)
    setup_live_routes(app)

    # ── 本机音色库（语音合成页用的 OpenAI 兼容接口）──
    try:
        from server.voice_routes import setup_voice_routes
        setup_voice_routes(app)
    except Exception as e:
        logger.warning(f"[音色] 本机音色接口注册失败：{e}")

    # ── 账号管理 ──
    from server.account_routes import setup_account_routes
    setup_account_routes(app)

    # ── 工作流编排 ──
    from server.workflow import workflow
    app.router.add_get("/api/workflow/status", lambda r: web.Response(
        content_type="application/json", text=json.dumps(workflow.status(), ensure_ascii=False)))
    app.router.add_post("/api/workflow/start",
                        lambda r: _workflow_start(r))
    app.router.add_post("/api/workflow/stop",
                        lambda r: _workflow_stop(r))
    app.router.add_get("/api/workflow/preflight", lambda r: web.Response(
        content_type="application/json", text=json.dumps(workflow.preflight(), ensure_ascii=False)))

    app.router.add_static('/', path='web')
