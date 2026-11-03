"""话术音频缓存 TTS 插件（离线兜底 + 极低延迟）。

为什么需要它
------------
现有降级链（base_tts.TTS_FALLBACK_CHAIN）的终点是 ``edgetts``，
而 EdgeTTS 是**联网服务**——一旦断网，整条链全断，数字人直接哑掉。
比赛现场网络不可控，这是致命风险。

同时 T1 意图直答的话术是**有限的预置文本**，同一句话会被反复播报，
每次重新合成纯属浪费。

本插件的做法
------------
- 以 ``sha1(音色|文本)`` 为键，把合成结果缓存成 16k 单声道 WAV
- **命中缓存**：直接读本地 WAV 播放，不联网、零合成延迟
- **未命中**：用 edge-tts 合成一次并落盘（write-through），此后永久命中

因此只要提前预热（见 tools/prewarm_tts_cache.py），
就能在**完全断网**的情况下把高频问题答出来。

注册名：``ttscache``
"""

import asyncio
import hashlib
import os
import time
from io import BytesIO
from pathlib import Path

import numpy as np
import resampy
import soundfile as sf

from registry import register
from utils.logger import logger

from .base_tts import BaseTTS, State


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = PROJECT_ROOT / "data" / "tts_cache"
CONFIG_FILE = PROJECT_ROOT / "config.yaml"
# 内置默认音色与 config.yaml / 启动脚本保持一致，避免预热与播放用了两个音色。
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"


def configured_voice() -> str:
    """解析服务实际使用的音色：TTS_VOICE 环境变量 → config.yaml 的 REF_FILE → 内置默认。

    预热与播放必须使用**同一个音色**，否则 sha1(音色|文本) 的键不同，预热全部落空。
    """
    env_voice = (os.getenv("TTS_VOICE") or "").strip()
    if env_voice:
        return env_voice
    try:
        import yaml

        with open(CONFIG_FILE, "r", encoding="utf-8") as fp:
            config = yaml.safe_load(fp) or {}
        voice = str(config.get("REF_FILE", "") or "").strip()
        if voice:
            return voice
    except Exception:
        logger.warning("读取 %s 失败，回退内置默认音色", CONFIG_FILE)
    return DEFAULT_VOICE


def resolve_voice(opt=None, textevent=None) -> str:
    """按播放时的口径解析音色：opt.REF_FILE → 配置默认 → 事件里的 ref_file。"""
    voice = getattr(opt, "REF_FILE", "") or configured_voice()
    if textevent:
        voice = (textevent.get("tts", {}) or {}).get("ref_file", voice) or voice
    return voice


def coverage(phrases, voice: str = "") -> dict:
    """统计一批话术的缓存覆盖率（去重后计数）。"""
    voice = voice or configured_voice()
    unique = []
    for item in phrases or []:
        text = str(item or "").strip()
        if text and text not in unique:
            unique.append(text)
    cached = sum(1 for text in unique if cache_file(voice, text).exists())
    return {
        "voice": voice,
        "total": len(unique),
        "cached": cached,
        "missing": len(unique) - cached,
    }


def cache_file(voice: str, text: str) -> Path:
    """按 音色 + 文本 计算缓存文件路径。"""
    key = hashlib.sha1(f"{voice}|{text}".strip().encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{key}.wav"


def is_cached(voice: str, text: str) -> bool:
    return cache_file(voice, text).exists()


async def _synthesize_to_wav(voice: str, text: str, dest: Path) -> None:
    """用 edge-tts 合成并落盘为 16k 单声道 WAV。"""
    import edge_tts

    buffer = BytesIO()
    communicate = edge_tts.Communicate(text, voice)
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            buffer.write(chunk["data"])
    if buffer.getbuffer().nbytes <= 0:
        raise RuntimeError("EdgeTTS 未返回音频")

    buffer.seek(0)
    stream, sample_rate = sf.read(buffer)
    stream = np.asarray(stream, dtype=np.float32)
    if stream.ndim > 1:
        stream = stream[:, 0]
    if sample_rate != 16000 and stream.shape[0] > 0:
        stream = resampy.resample(x=stream, sr_orig=sample_rate, sr_new=16000)

    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_suffix(".tmp")
    # 必须显式指定 format：临时文件后缀是 .tmp，soundfile 无法据此推断格式
    sf.write(str(temp), stream.astype(np.float32), 16000, format="WAV")
    os.replace(temp, dest)


def warm_phrases(phrases, voice: str = "", on_progress=None) -> dict:
    """批量预热：把一组话术合成进缓存。返回统计信息。"""
    voice = voice or DEFAULT_VOICE
    unique = []
    for item in phrases or []:
        text = str(item or "").strip()
        if text and text not in unique:
            unique.append(text)

    created, skipped, failed = 0, 0, []
    for index, text in enumerate(unique, start=1):
        dest = cache_file(voice, text)
        if dest.exists():
            skipped += 1
        else:
            try:
                asyncio.run(_synthesize_to_wav(voice, text, dest))
                created += 1
            except Exception as exc:  # noqa: BLE001
                logger.exception("TTS 预热失败: %.30s", text)
                failed.append({"text": text, "error": str(exc)})
        if on_progress:
            on_progress(index, len(unique), text)

    return {
        "voice": voice,
        "total": len(unique),
        "created": created,
        "skipped": skipped,
        "failed": failed,
        "cache_dir": str(CACHE_DIR),
    }


@register("tts", "ttscache")
class TTSCache(BaseTTS):
    """先查本地缓存，未命中再联网合成并写回缓存。"""

    def __init__(self, opt, parent):
        super().__init__(opt, parent)
        self._tts_name = "ttscache"
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _load_stream(self, path: Path):
        stream, sample_rate = sf.read(str(path))
        stream = np.asarray(stream, dtype=np.float32)
        if stream.ndim > 1:
            stream = stream[:, 0]
        if sample_rate != self.sample_rate and stream.shape[0] > 0:
            stream = resampy.resample(
                x=stream, sr_orig=sample_rate, sr_new=self.sample_rate
            )
        if stream.shape[0] <= 0:
            raise RuntimeError("TTS 缓存音频为空")
        return stream

    def play_if_cached(self, msg: tuple[str, dict]) -> bool:
        """命中缓存才播放；未命中立即返回 False，绝不联网、绝不合成。"""
        text, textevent = msg
        dest = cache_file(resolve_voice(self.opt, textevent), text)
        if not dest.exists():
            return False
        start = time.perf_counter()
        stream = self._load_stream(dest)
        logger.info("tts cache HIT (%.4fs): %.30s", time.perf_counter() - start, text)
        try:
            self._stream_to_avatar(stream, text, textevent)
        except Exception:
            # 音频已经在往 Avatar 送，失败再重播一遍只会更糟，这里吞掉并记录
            logger.exception("tts 缓存播放失败（不重播）")
        return True

    def txt_to_audio(self, msg: tuple[str, dict]):
        text, textevent = msg
        voice = resolve_voice(self.opt, textevent)
        dest = cache_file(voice, text)

        start = time.perf_counter()
        stream = None
        if dest.exists():
            try:
                stream = self._load_stream(dest)
            except Exception:
                logger.warning("tts 缓存文件损坏，删除后重新合成: %s", dest.name)
                try:
                    dest.unlink()
                except OSError:
                    pass
        if stream is not None:
            logger.info(
                "tts cache HIT (%.4fs): %.30s", time.perf_counter() - start, text
            )
        else:
            try:
                asyncio.run(_synthesize_to_wav(voice, text, dest))
                stream = self._load_stream(dest)
                logger.info(
                    "tts cache MISS → 已合成并缓存 (%.4fs): %.30s",
                    time.perf_counter() - start, text,
                )
            except Exception:
                logger.exception("tts cache 合成失败，交由降级链处理")
                raise

        self._stream_to_avatar(stream, text, textevent)
