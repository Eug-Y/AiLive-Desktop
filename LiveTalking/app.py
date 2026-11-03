###############################################################################
#  Copyright (C) 2024 LiveTalking@lipku https://github.com/lipku/LiveTalking
#  email: lipku@foxmail.com
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
###############################################################################

import asyncio
import copy
import importlib
import json
import os
import random
import argparse

import torch.multiprocessing as mp
from aiohttp import web
import aiohttp_cors
from dotenv import load_dotenv

from avatars.base_avatar import BaseAvatar
from llm import llm_response
import registry
from server.routes import setup_routes
from server.rtc_manager import RTCManager
from server.session_manager import session_manager
from server.context import init_context, get_context
from utils.logger import logger


# ─── 全局变量（迁移至 AppContext 中）─────────────────────────────────────
opt = None
model = None
global_avatars = {}  # avatar_id → payload
rtc_manager = None


# ─── 工具函数 ───────────────────────────────────────────────────────────

def randN(N: int) -> int:
    """生成长度为 N 的随机数"""
    min_val = pow(10, N - 1)
    max_val = pow(10, N)
    return random.randint(min_val, max_val - 1)


# ─── Avatar Session 工厂 ─────────────────────────────────────────────────

def build_avatar_session(sessionid: str, params: dict) -> BaseAvatar:
    """创建 Avatar 会话实例。"""
    global opt, model, global_avatars

    opt_this = copy.deepcopy(opt)
    opt_this.sessionid = sessionid

    avatar_id = params.get('avatar', opt.avatar_id)
    opt_this.avatar_id = avatar_id
    ref_audio = params.get('refaudio', '')
    ref_text = params.get('reftext', '')

    if avatar_id and avatar_id != opt.avatar_id:
        if avatar_id not in global_avatars:
            global_avatars[avatar_id] = load_avatar(avatar_id)
        avatar_this = global_avatars[avatar_id]
    else:
        avatar_this = global_avatars.get(opt.avatar_id)

    if ref_audio:
        opt_this.REF_FILE = ref_audio
        opt_this.REF_TEXT = ref_text

    custom_config = params.get('custom_config', '')
    if custom_config:
        opt_this.customopt = json.loads(custom_config)

    avatar_session = registry.create("avatar", opt.model, opt=opt_this, model=model, avatar=avatar_this)
    return avatar_session


# ─── WebRTC 路由 ─────────────────────────────────────────────────────────

async def offer(request):
    return await rtc_manager.handle_offer(request)


async def whep(request):
    return await rtc_manager.handle_whep(request)


async def on_shutdown(app):
    """优雅关闭：先停 RTC，再停 session。"""
    logger.info("Server shutting down...")
    if rtc_manager:
        await rtc_manager.shutdown()
    session_manager.shutdown()
    # 关闭 live_studio
    try:
        from server.live_studio import live_studio_manager
        live_studio_manager.shutdown()
    except Exception:
        pass
    logger.info("Server shutdown complete")


async def download_record(request):
    sessionid = request.match_info.get('sessionid')
    if not sessionid:
        return web.Response(status=400, text="sessionid is required")

    record_file = os.path.join('data', 'record', f"{sessionid}.mp4")
    if os.path.exists(record_file):
        return web.FileResponse(record_file)
    return web.Response(status=404, text="Record not found")


# ─── 主入口 ──────────────────────────────────────────────────────────────

def main():
    global rtc_manager, opt, model, load_avatar

    # 解析命令行参数
    from config import parse_args
    opt = parse_args()

    # ─── 加载 avatar 插件 ──────────────────────────────────────────────
    _avatar_modules = {
        'musetalk':   'avatars.musetalk_avatar',
        'wav2lip':    'avatars.wav2lip_avatar',
        'ultralight': 'avatars.ultralight_avatar',
    }
    avatar_mod = importlib.import_module(_avatar_modules[opt.model])
    load_model = avatar_mod.load_model
    load_avatar = avatar_mod.load_avatar
    warm_up = avatar_mod.warm_up
    logger.info(opt)

    # ─── 加载模型 ──────────────────────────────────────────────────────
    if opt.model == 'musetalk':
        model = load_model()
        global_avatars[opt.avatar_id] = load_avatar(opt.avatar_id)
        warm_up(opt.batch_size, model)
    elif opt.model == 'wav2lip':
        model = load_model("./models/wav2lip.pth")
        global_avatars[opt.avatar_id] = load_avatar(opt.avatar_id)
        warm_up(opt.batch_size, model, 256)
    elif opt.model == 'ultralight':
        model = load_model(opt)
        global_avatars[opt.avatar_id] = load_avatar(opt.avatar_id)
        warm_up(opt.batch_size, global_avatars[opt.avatar_id], 160)

    # ─── 初始化 AppContext ─────────────────────────────────────────────
    ctx = init_context(opt, model=model, avatars=global_avatars)
    ctx.set_component_health("model", True)

    # ─── 初始化 Session Manager ─────────────────────────────────────────
    session_manager.set_max_session(opt.max_session)
    session_manager.init_builder(build_avatar_session)
    rtc_manager = RTCManager(opt)

    # ─── 虚拟摄像头 / RTMP 模式 ─────────────────────────────────────────
    if opt.transport == 'virtualcam' or opt.transport == 'rtmp':
        from threading import Thread, Event
        thread_quit = Event()
        params = {}
        session_manager.add_session('0', build_avatar_session('0', params))
        rendthrd = Thread(target=session_manager.get_session('0').render, args=(thread_quit,))
        rendthrd.start()
        if opt.transport == 'virtualcam':
            logger.info("[VirtualCam] Virtual camera output enabled")

    # ─── 构建 aiohttp 应用 ─────────────────────────────────────────────
    appasync = web.Application(client_max_size=1024**2 * 100)
    appasync["llm_response"] = llm_response
    appasync["opt"] = opt
    appasync["rtc_manager"] = rtc_manager

    appasync.on_shutdown.append(on_shutdown)
    appasync.router.add_post("/offer", offer)
    appasync.router.add_get("/record/{sessionid}", download_record)

    # 注册通用 API 路由
    setup_routes(appasync)

    # CORS
    cors = aiohttp_cors.setup(appasync, defaults={
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=True,
            expose_headers="*",
            allow_headers="*",
        )
    })
    for route in list(appasync.router.routes()):
        cors.add(route)

    # /whep 注册在 CORS 之后
    whep_resource = appasync.router.add_resource('/whep')
    whep_resource.add_route('POST', whep)
    whep_resource.add_route('OPTIONS', lambda _: web.Response(status=200))

    logger.info('Server started; http://<serverip>:' + str(opt.listenport))

    def run_server(runner):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, '0.0.0.0', opt.listenport)
        loop.run_until_complete(site.start())
        if opt.transport == 'rtcpush':
            for k in range(opt.max_session):
                push_url = opt.push_url
                if k != 0:
                    push_url = opt.push_url + str(k)
                loop.run_until_complete(rtc_manager.handle_rtcpush(push_url, str(k)))
        loop.run_forever()

    run_server(web.AppRunner(appasync))


if __name__ == '__main__':
    mp.set_start_method('spawn')
    load_dotenv()
    main()
