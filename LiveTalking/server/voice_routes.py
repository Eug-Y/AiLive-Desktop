# -*- coding: utf-8 -*-
"""OpenAI 兼容的语音接口（本机版）。

页面 web/tts/index.html 是按 OpenAI 风格调的：
    GET    /v1/audio/voices               音色列表
    POST   /v1/audio/voices               上传音色（multipart：audio_sample/name/ref_text/…）
    DELETE /v1/audio/voices/{name}        删除音色
    POST   /v1/audio/speech               合成试听

本机实现分成三类音色：
  * 预设语音（Edge TTS）：真能朗读任意文本，需要联网（项目里 tts 本来就是 edgetts）。
  * 本机语音（Windows SAPI）：系统自带的中文语音，**完全离线**，断网也能朗读任意文本；
    Edge 合成失败时会自动用它兜底，不会让「合成」按钮变成哑巴。
  * 已上传音色（本机录音，见 voice_library）：能试听、能原声播报；文本与参考文本不一致
    时返回 409 并说明原因——本机没有零样本克隆模型，硬凑一个别的声音反而不诚实。
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

from aiohttp import web

from server import voice_library
from utils.logger import logger

# 预设语音：值为展示名（页面上直接显示这个），合成时再映射回 Edge TTS 的 voice id。
PRESET_VOICES = {
    'zh-CN-XiaoxiaoNeural': '晓晓 · 女声（默认）',
    'zh-CN-XiaoyiNeural': '晓伊 · 女声（年轻）',
    'zh-CN-YunxiNeural': '云希 · 男声（年轻）',
    'zh-CN-YunjianNeural': '云健 · 男声（沉稳）',
    'zh-CN-YunyangNeural': '云扬 · 男声（播报）',
    'zh-CN-liaoning-XiaobeiNeural': '晓北 · 女声（东北话）',
    'zh-CN-shaanxi-XiaoniNeural': '晓妮 · 女声（陕西话）',
}
PRESET_BY_LABEL = {label: voice_id for voice_id, label in PRESET_VOICES.items()}

# 本机语音（Windows SAPI）id 前缀，用来和 Edge 的 voice id 区分
LOCAL_PREFIX = 'sapi:'

MIME = {
    '.mp3': 'audio/mpeg',
    '.wav': 'audio/wav',
    '.m4a': 'audio/mp4',
    '.mp4': 'audio/mp4',
    '.aac': 'audio/aac',
    '.flac': 'audio/flac',
    '.ogg': 'audio/ogg',
    '.opus': 'audio/ogg',
    '.webm': 'audio/webm',
}


def _voice_payload(entry: dict) -> dict:
    return {
        'name': entry['name'],
        'ref_text': entry.get('ref_text', ''),
        'speaker_description': entry.get('speaker_description', ''),
        'created_at': entry.get('created_at', 0),
        'duration': entry.get('duration', 0),
        'kind': 'sample',
        'audio_url': '/v1/audio/voices/%s/audio' % quote(entry['name']),
    }


# ─── 音色列表 ────────────────────────────────────────────────────────────

async def voices_list(request):
    uploaded = [_voice_payload(e) for e in voice_library.list_voices()]
    local = _local_voices()
    return web.json_response({
        'voices': [label for label in PRESET_VOICES.values()] + [n for _, n in local],
        'voice_details': [
            {'name': label, 'id': voice_id, 'kind': 'preset', 'offline': False}
            for voice_id, label in PRESET_VOICES.items()
        ] + [
            {'name': name, 'id': vid, 'kind': 'local', 'offline': True}
            for vid, name in local
        ],
        'uploaded_voices': uploaded,
        'engines': {'edge': True, 'windows_local': bool(local)},
    })


async def voices_create(request):
    try:
        form = await request.post()
    except Exception as exc:
        return web.json_response({'detail': '解析上传内容失败：%s' % exc}, status=400)

    field = form.get('audio_sample')
    if field is None or not getattr(field, 'file', None):
        return web.json_response({'detail': '缺少音频文件（字段名 audio_sample）'}, status=400)
    data = field.file.read()
    try:
        entry = voice_library.add_voice(
            name=(form.get('name') or '').strip(),
            data=data,
            filename=getattr(field, 'filename', '') or '',
            ref_text=(form.get('ref_text') or '').strip(),
            description=(form.get('speaker_description') or '').strip(),
            overwrite=str(form.get('overwrite') or '').lower() in ('1', 'true', 'yes'),
        )
    except ValueError as exc:
        return web.json_response({'detail': str(exc)}, status=400)
    except Exception as exc:
        logger.exception('[音色] 保存失败')
        return web.json_response({'detail': '保存失败：%s' % exc}, status=500)
    return web.json_response({'ok': True, 'voice': _voice_payload(entry)}, status=201)


async def voices_delete(request):
    name = request.match_info.get('name', '')
    if not voice_library.remove_voice(name):
        return web.json_response({'detail': '音色不存在：%s' % name}, status=404)
    return web.json_response({'ok': True})


async def voice_audio(request):
    entry = voice_library.get_voice(request.match_info.get('name', ''))
    if not entry:
        return web.json_response({'detail': '音色不存在'}, status=404)
    path = voice_library.resolve_audio(entry)
    if not path.is_file():
        return web.json_response({'detail': '音色文件丢失'}, status=404)
    return web.FileResponse(path, headers={
        'Content-Type': MIME.get(path.suffix.lower(), 'application/octet-stream'),
        'Accept-Ranges': 'bytes',
        'Cache-Control': 'no-store',
    })


# ─── 本机语音（Windows SAPI，离线可用） ─────────────────────────────────

_LOCAL_CACHE: dict | None = None


def _sapi_available() -> bool:
    try:
        import win32com.client  # noqa: F401
    except Exception:
        return False
    return True


def _friendly_name(desc: str) -> str:
    """把 SAPI 的英文描述翻成中文展示名。"""
    d = (desc or '').lower()
    gender = '女声' if any(k in d for k in ('huihui', 'zira', 'yaoyao', 'female')) else '男声'
    if 'chinese (simplified)' in d or 'zh-cn' in d or 'chinese' in d:
        return '本机 · %s（中文%s · 离线）' % (
            '慧慧' if 'huihui' in d else ('康康' if 'kangkang' in d else
                                          ('瑶瑶' if 'yaoyao' in d else '中文')), gender)
    if 'english' in d:
        return '本机 · %s（英文%s · 离线）' % ('Zira' if 'zira' in d else
                                              ('David' if 'david' in d else 'English'), gender)
    return '本机 · %s（离线）' % (desc or '默认')


def _local_voices() -> list[tuple[str, str]]:
    """返回 [(voice_id, 展示名)]；非 Windows 或没装 pywin32 时返回空列表。"""
    global _LOCAL_CACHE
    if _LOCAL_CACHE is not None:
        return list(_LOCAL_CACHE)
    items: list[tuple[str, str]] = []
    if _sapi_available() and sys.platform.startswith('win'):
        try:
            import pythoncom
            import win32com.client as wc
            pythoncom.CoInitialize()
            v = wc.Dispatch('SAPI.SpVoice')
            voices = v.GetVoices()
            for i in range(voices.Count):
                desc = str(voices.Item(i).GetDescription())
                items.append((LOCAL_PREFIX + desc, _friendly_name(desc)))
            del voices, v
        except Exception as exc:
            logger.warning('[音色] 枚举本机语音失败：%r', exc)
    # 中文排前面；枚举失败时不写缓存，下次还能再试
    items.sort(key=lambda kv: (0 if '中文' in kv[1] else 1, kv[1]))
    if items or not _sapi_available():
        _LOCAL_CACHE = items
    logger.info('[音色] 本机语音 %d 个：%s', len(items), [n for _, n in items])
    return list(items)


def _local_pick(prefer_chinese: bool = True) -> str | None:
    voices = _local_voices()
    if not voices:
        return None
    if prefer_chinese:
        for vid, label in voices:
            if '中文' in label:
                return vid
    return voices[0][0]


def _sapi_speak(text: str, voice_desc: str, speed: float = 1.0) -> bytes:
    """阻塞调用：用 Windows SAPI 合成 wav。必须在工作线程里跑。"""
    import os
    import pythoncom
    import win32com.client as wc

    pythoncom.CoInitialize()
    try:
        v = wc.Dispatch('SAPI.SpVoice')
        target = None
        voices = v.GetVoices()
        for i in range(voices.Count):
            tok = voices.Item(i)
            if str(tok.GetDescription()) == voice_desc:
                target = tok
                break
        if target is not None:
            v.Voice = target
        v.Rate = max(-10, min(10, int(round((max(0.25, min(4.0, speed)) - 1.0) * 10))))

        fd, path = tempfile.mkstemp(suffix='.wav')
        os.close(fd)
        try:
            fs = wc.Dispatch('SAPI.SpFileStream')
            fs.Open(path, 3)  # SSFMCreateForWrite
            v.AudioOutputStream = fs
            v.Speak(text)
            fs.Close()
            data = Path(path).read_bytes()
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        del fs, voices, v
        if not data:
            raise RuntimeError('SAPI 没有输出音频')
        return data
    finally:
        pass


async def _local_tts(text: str, voice_desc: str, speed: float, fmt: str) -> bytes:
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, lambda: _sapi_speak(text, voice_desc, speed))
    return _transcode(data, fmt)


def _transcode(wav_bytes: bytes, fmt: str) -> bytes:
    """wav -> mp3（有 ffmpeg 时）；否则原样返回 wav。"""
    if fmt in ('wav', 'pcm', '') or not shutil.which('ffmpeg'):
        return wav_bytes
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / 'in.wav'
        dst = Path(tmp) / 'out.mp3'
        src.write_bytes(wav_bytes)
        proc = subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(src),
                               '-codec:a', 'libmp3lame', '-q:a', '4', str(dst)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
        if proc.returncode == 0 and dst.is_file():
            return dst.read_bytes()
    return wav_bytes


# ─── 合成 ────────────────────────────────────────────────────────────────

async def _edge_tts(text: str, voice: str, speed: float, fmt: str) -> bytes:
    import edge_tts
    rate = '%+d%%' % int(round((max(0.25, min(4.0, speed)) - 1.0) * 100))
    communicate = edge_tts.Communicate(text, voice, rate=rate)
    raw = bytearray()
    async for chunk in communicate.stream():
        if chunk.get('type') == 'audio':
            raw.extend(chunk.get('data') or b'')
    data = bytes(raw)
    if not data:
        raise RuntimeError('Edge TTS 没有返回音频')
    if fmt in ('wav', 'pcm') and shutil.which('ffmpeg'):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / 'in.mp3'
            dst = Path(tmp) / 'out.wav'
            src.write_bytes(data)
            proc = subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(src),
                                   '-ar', '24000', '-ac', '1', str(dst)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
            if proc.returncode == 0 and dst.is_file():
                return dst.read_bytes()
    return data


async def speech(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'detail': '请求体必须是 JSON'}, status=400)

    text = (body.get('input') or '').strip()
    voice = (body.get('voice') or '').strip()
    fmt = (body.get('response_format') or 'mp3').lower()
    try:
        speed = float(body.get('speed') or 1.0)
    except (TypeError, ValueError):
        speed = 1.0

    if not voice:
        return web.json_response({'detail': '缺少 voice 参数'}, status=400)

    entry = voice_library.get_voice(voice)
    if entry:
        # 预录音色：只能回放原声；想读新文本必须换预设音色
        ref = (entry.get('ref_text') or '').strip()
        if text and text != ref:
            return web.json_response({
                'detail': ('音色「%s」是本机预录音色（原声 %.0f 秒），只能回放原声，'
                           '不能朗读新文本；要读自定义内容请选「预设语音」或「本机语音」。'
                           % (entry['name'], entry.get('duration') or 0))
            }, status=409)
        path = voice_library.resolve_audio(entry)
        if not path.is_file():
            return web.json_response({'detail': '音色文件丢失'}, status=404)
        return web.FileResponse(path, headers={
            'Content-Type': MIME.get(path.suffix.lower(), 'application/octet-stream'),
            'X-Ailive-Voice-Kind': 'sample',
        })

    local_vid = None
    if voice.startswith(LOCAL_PREFIX):
        local_vid = voice
    elif voice not in PRESET_VOICES:
        voice_id = PRESET_BY_LABEL.get(voice)
        if not voice_id:
            # 也可能是本机语音的展示名
            for vid, name in _local_voices():
                if name == voice:
                    local_vid = vid
                    break
            if not local_vid:
                return web.json_response({'detail': '未知音色：%s' % voice}, status=404)
    else:
        voice_id = voice
    if not text:
        return web.json_response({'detail': '合成文本不能为空'}, status=400)

    engine = 'preset'
    if local_vid:
        # 本机语音：直接离线合成
        try:
            data = await _local_tts(text, local_vid[len(LOCAL_PREFIX):], speed, fmt)
            engine = 'windows-local'
        except Exception as exc:
            logger.warning('[音色] 本机语音合成失败：%r', exc)
            return web.json_response({
                'detail': '本机语音合成失败：%s（若刚开关机可稍后重试）' % exc
            }, status=500)
    else:
        try:
            data = await _edge_tts(text, voice_id, speed, fmt)
        except Exception as exc:
            logger.warning('[音色] Edge TTS 合成失败：%r', exc)
            fallback = _local_pick()
            if fallback is None:
                return web.json_response({
                    'detail': _edge_error_hint(exc)
                }, status=502)
            # 联网失败就用系统自带语音兜底，保证「点合成就有声音」
            try:
                data = await _local_tts(text, fallback[len(LOCAL_PREFIX):], speed, fmt)
                engine = 'windows-local-fallback'
                logger.info('[音色] Edge 不可用，已用本机语音兜底：%s', fallback)
            except Exception as exc2:
                logger.warning('[音色] 本机语音兜底也失败：%r', exc2)
                return web.json_response({'detail': _edge_error_hint(exc)}, status=502)

    return web.Response(body=data, content_type=_audio_ctype(data, fmt), headers={
        'X-Ailive-Voice-Kind': 'preset' if engine == 'preset' else 'local',
        'X-Ailive-Voice-Engine': engine,
        'Cache-Control': 'no-store',
    })


def _audio_ctype(data: bytes, fmt: str) -> str:
    """按实际数据判断类型：转码失败时不能拿 mp3 的头去装 wav。"""
    if data[:4] == b'RIFF':
        return 'audio/wav'
    if data[:3] == b'ID3' or data[:2] in (b'\xff\xfb', b'\xff\xf3', b'\xff\xf2'):
        return 'audio/mpeg'
    return 'audio/mpeg' if fmt not in ('wav', 'pcm') else 'audio/wav'


def _edge_error_hint(exc: BaseException) -> str:
    msg = str(exc)
    if '403' in msg or 'WSServerHandshakeError' in msg:
        return ('微软 Edge TTS 拒绝了本次请求（403），多半是 edge-tts 版本过旧。'
                '在项目目录执行：env\\python.exe -m pip install -U edge-tts 后重启服务。'
                '（原始错误：%s）' % msg[:120])
    if 'Cannot connect' in msg or 'Timeout' in msg or 'DNS' in msg:
        return '连不上微软 Edge TTS，请检查网络/代理。（原始错误：%s）' % msg[:120]
    return '预设语音合成失败（Edge TTS）：%s' % msg[:200]


def setup_voice_routes(app):
    app.router.add_get('/v1/audio/voices', voices_list)
    app.router.add_post('/v1/audio/voices', voices_create)
    app.router.add_delete('/v1/audio/voices/{name}', voices_delete)
    app.router.add_get('/v1/audio/voices/{name}/audio', voice_audio)
    app.router.add_post('/v1/audio/speech', speech)
    logger.info('[音色] 本机音色接口已启用：/v1/audio/voices、/v1/audio/speech')
