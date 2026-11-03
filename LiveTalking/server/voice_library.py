# -*- coding: utf-8 -*-
"""本机音色库（预录音色）。

为什么需要它
------------
语音合成页（web/tts/index.html）原本对接的是外部 vLLM Omni 服务（默认
``http://localhost:8091``）：音色列表、上传克隆音色、合成试听全在那边。本机没有这个
服务、也没有可用的零样本克隆模型（models 目录里只有 wav2lip.pth），所以页面里点
「上传克隆音色」必然失败。

本模块把「音色」落在本机磁盘上，按同一套 OpenAI 兼容接口对外提供，页面不用改协议：

    data/voices/index.json          音色索引（数组，每个元素一条音色）
    data/voices/<音色名>/sample.*   上传时的原始音频（m4a/mp3/wav…）
    data/voices/<音色名>/sample.wav 统一转成 16k 单声道，给数字人播报用
    data/voices/<音色名>/meta.json  单条音色的元数据

语义（诚实版）：这里的音色是**预录音色**——能试听、能选用、能原声播报，但不能用
它朗读任意新文本（那需要真正的音色克隆模型）。任意文本请用预设的 Edge TTS 音色。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # LiveTalking/
VOICE_DIR = ROOT / 'data' / 'voices'
INDEX_FILE = VOICE_DIR / 'index.json'

MAX_BYTES = 10 * 1024 * 1024
ALLOWED_EXT = ('.wav', '.mp3', '.m4a', '.mp4', '.aac', '.flac', '.ogg', '.webm', '.opus')
_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]+')


# ─── 外部工具 ────────────────────────────────────────────────────────────

def _find_tool(name: str):
    """找 ffmpeg/ffprobe：先看 PATH，再看 WinGet 安装目录（本机就是那种）。"""
    found = shutil.which(name)
    if found:
        return found
    base = Path.home() / 'AppData' / 'Local' / 'Microsoft' / 'WinGet' / 'Packages'
    if base.is_dir():
        for candidate in sorted(base.glob('Gyan.FFmpeg*/*/bin/%s.exe' % name)):
            if candidate.is_file():
                return str(candidate)
    return None


def _run(cmd):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=120, check=False)


def probe_duration(path: Path) -> float:
    """读时长（秒）；没有 ffprobe 时对 wav 用标准库兜底。"""
    ffprobe = _find_tool('ffprobe')
    if ffprobe:
        proc = _run([ffprobe, '-v', 'error', '-show_entries', 'format=duration',
                     '-of', 'default=nw=1:nk=1', str(path)])
        try:
            return max(0.0, float(proc.stdout.decode('utf-8', 'replace').strip()))
        except ValueError:
            pass
    if path.suffix.lower() == '.wav':
        try:
            import wave
            with wave.open(str(path), 'rb') as fp:
                return fp.getnframes() / float(fp.getframerate() or 1)
        except Exception:
            return 0.0
    return 0.0


def to_wav_16k(src: Path, dst: Path):
    """转 16k 单声道 wav（数字人播报/TTS 参考音都吃这个格式）。失败返回 None。"""
    ffmpeg = _find_tool('ffmpeg')
    if not ffmpeg:
        return None
    proc = _run([ffmpeg, '-y', '-loglevel', 'error', '-i', str(src),
                 '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(dst)])
    if proc.returncode == 0 and dst.is_file() and dst.stat().st_size > 0:
        return dst
    return None


def convert(src: Path, dst: Path):
    """按目标扩展名转封装（合成试听要 mp3/wav 时用）。返回是否成功。"""
    ffmpeg = _find_tool('ffmpeg')
    if not ffmpeg:
        return False
    proc = _run([ffmpeg, '-y', '-loglevel', 'error', '-i', str(src), str(dst)])
    return proc.returncode == 0 and dst.is_file() and dst.stat().st_size > 0


# ─── 索引 ────────────────────────────────────────────────────────────────

def sanitize_name(name: str) -> str:
    return _ILLEGAL.sub('_', (name or '').strip()).strip(' .')


def _slug(name: str) -> str:
    slug = _ILLEGAL.sub('_', name).strip(' .')
    return slug[:60] or 'voice'


def _read_index() -> list:
    if not INDEX_FILE.is_file():
        return []
    try:
        data = json.loads(INDEX_FILE.read_text(encoding='utf-8'))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_index(entries: list) -> None:
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = INDEX_FILE.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(INDEX_FILE)


def list_voices() -> list:
    """返回索引里仍然有效的音色（文件被人手动删掉的条目自动跳过）。"""
    alive = []
    for entry in _read_index():
        folder = VOICE_DIR / entry.get('dir', '')
        audio = folder / entry.get('audio', '')
        if entry.get('name') and audio.is_file():
            if not entry.get('duration'):
                entry['duration'] = round(probe_duration(audio), 2)
            alive.append(entry)
    return alive


def get_voice(name: str):
    name = sanitize_name(name)
    for entry in list_voices():
        if entry['name'] == name:
            return entry
    return None


def resolve_audio(entry: dict) -> Path:
    folder = VOICE_DIR / entry['dir']
    wav = folder / (entry.get('wav') or '')
    if entry.get('wav') and wav.is_file():
        return wav
    return folder / entry['audio']


# ─── 增删 ────────────────────────────────────────────────────────────────

def add_voice(name: str, data: bytes, filename: str = '', ref_text: str = '',
              description: str = '', overwrite: bool = False) -> dict:
    name = sanitize_name(name)
    if not name:
        raise ValueError('音色名称不能为空')
    if len(name) > 40:
        raise ValueError('音色名称太长（最多 40 个字符）')
    if not data:
        raise ValueError('音频内容为空')
    if len(data) > MAX_BYTES:
        raise ValueError('音频文件不能超过 10 MB（当前 %.1f MB）' % (len(data) / 1048576.0))
    ext = Path(filename or '').suffix.lower()
    if ext not in ALLOWED_EXT:
        raise ValueError('不支持的音频格式：%s（支持 %s）' % (ext or '未知', '、'.join(ALLOWED_EXT)))

    entries = list_voices()
    exist = next((e for e in entries if e['name'] == name), None)
    if exist and not overwrite:
        raise ValueError('音色「%s」已存在，换个名字或先删除' % name)

    slug = exist['dir'] if exist else _slug(name)
    folder = VOICE_DIR / slug
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob('sample.*'):
        old.unlink(missing_ok=True)

    audio = folder / ('sample' + ext)
    audio.write_bytes(data)
    duration = probe_duration(audio)
    wav = to_wav_16k(audio, folder / 'sample.wav')

    entry = {
        'name': name,
        'dir': slug,
        'audio': audio.name,
        'wav': wav.name if wav else '',
        'ext': ext,
        'bytes': len(data),
        'duration': round(duration, 2),
        'ref_text': ref_text or '',
        'speaker_description': description or '',
        'kind': 'sample',
        'created_at': int(time.time()),
    }
    (folder / 'meta.json').write_text(json.dumps(entry, ensure_ascii=False, indent=2),
                                      encoding='utf-8')
    entries = [e for e in entries if e['name'] != name] + [entry]
    _write_index(entries)
    return entry


def remove_voice(name: str) -> bool:
    name = sanitize_name(name)
    entries = list_voices()
    hit = next((e for e in entries if e['name'] == name), None)
    if not hit:
        return False
    folder = VOICE_DIR / hit['dir']
    if folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)
    _write_index([e for e in entries if e['name'] != name])
    return True
