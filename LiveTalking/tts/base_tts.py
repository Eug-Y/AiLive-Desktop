import time
import threading
from threading import Thread
import queue
from queue import Queue
from io import BytesIO
from enum import Enum
from typing import TYPE_CHECKING, Optional

import numpy as np

if TYPE_CHECKING:
    from avatars.base_avatar import BaseAvatar

from utils.logger import logger


class State(Enum):
    RUNNING = 0
    PAUSE = 1


# ─── TTS 降级配置 ──────────────────────────────────────────────────────
# 当主 TTS 失败时，按顺序尝试备用 TTS
#
# 关于 ttscache：它是唯一**不依赖网络**的兜底。
# 之前所有引擎的终点都是 edgetts（联网服务），一旦断网整条链全断、数字人哑掉；
# 把 ttscache 放在 edgetts 之后，可以保证已预热过的话术在断网时仍能播报。
TTS_FALLBACK_CHAIN = {
    "ttscache": [],                      # 缓存引擎自己会联网补一次，无需再降级
    "edgetts": ["ttscache"],             # 断网时改用已预热的缓存音频
    "doubao": ["ttscache", "edgetts"],   # 豆包失败 → 缓存 → EdgeTTS
    "azuretts": ["ttscache", "edgetts"],
    "tencent": ["ttscache", "edgetts"],
    "gpt-sovits": ["ttscache", "edgetts"],
    "cosyvoice": ["ttscache", "edgetts"],
    "fishtts": ["ttscache", "edgetts"],
    "xtts": ["ttscache", "edgetts"],
    "indextts2": ["ttscache", "edgetts"],
    "qwentts": ["ttscache", "edgetts"],
    "omnitts": ["ttscache", "edgetts"],
}

TTS_MAX_RETRIES = 2  # 每个 TTS 引擎的最大重试次数


class BaseTTS:
    def __init__(self, opt, parent: "BaseAvatar"):
        self.opt = opt
        self.parent = parent
        self.sample_rate = 16000
        self.chunk = self.sample_rate // (opt.fps * 2)  # 320 samples per chunk (20ms)
        self.input_stream = BytesIO()
        self.msgqueue = Queue()
        self.state = State.RUNNING
        self._fallback_tts: Optional["BaseTTS"] = None
        self._tts_name: str = "base"
        self._generation = 0
        self._active_generation = 0

    def flush_talk(self):
        self._generation += 1
        while True:
            try:
                self.msgqueue.get_nowait()
            except queue.Empty:
                break
        self.state = State.PAUSE
        if self._fallback_tts:
            self._fallback_tts.flush_talk()

    def put_msg_txt(self, msg: str, datainfo: dict = {}):
        if len(msg) > 0:
            self.msgqueue.put((msg, datainfo, self._generation))

    def render(self, quit_event):
        process_thread = Thread(target=self.process_tts, args=(quit_event,))
        process_thread.start()

    def process_tts(self, quit_event):
        while not quit_event.is_set():
            try:
                msg = self.msgqueue.get(block=True, timeout=1)
                if len(msg) == 2:
                    msg = (*msg, self._generation)
                text, textevent, generation = msg
                if generation != self._generation:
                    continue
                self.state = State.RUNNING
            except queue.Empty:
                continue
            self._active_generation = generation
            self._try_synthesize((text, textevent), generation)
        self.stop_tts()
        logger.info('tts thread stop: %s', self._tts_name)

    def _try_synthesize(self, msg: tuple[str, dict], generation=None):
        """尝试合成，失败时自动降级到备用 TTS。

        重试逻辑：
        1. 主 TTS 尝试 TTS_MAX_RETRIES 次
        2. 全部失败后，按 TTS_FALLBACK_CHAIN 顺序尝试备用 TTS
        3. 最终兜底是 EdgeTTS（免费、稳定）
        """
        text, textevent = msg
        tts_name = self._tts_name
        generation = self._generation if generation is None else generation
        if generation != self._generation:
            return

        # 缓存优先：已预热话术直接本地播放。
        # 断网时不必先等在线引擎两次超时再降级，联网时也省一次云端调用。
        if self._play_cached(msg):
            return

        # 尝试主 TTS
        for attempt in range(1, TTS_MAX_RETRIES + 1):
            try:
                self.txt_to_audio(msg)
                if generation != self._generation:
                    return
                return  # 成功
            except Exception as e:
                if generation != self._generation:
                    return
                logger.warning(
                    "TTS [%s] attempt %d/%d failed: %s",
                    tts_name, attempt, TTS_MAX_RETRIES, e,
                )
                if attempt < TTS_MAX_RETRIES:
                    time.sleep(0.3 * attempt)

        # 降级到备用 TTS
        fallback_chain = TTS_FALLBACK_CHAIN.get(tts_name, ["edgetts"])
        for fallback_name in fallback_chain:
            logger.info("TTS falling back: %s → %s", tts_name, fallback_name)
            try:
                self._try_fallback_tts(fallback_name, msg)
                return
            except Exception as e:
                logger.error("TTS fallback [%s] also failed: %s", fallback_name, e)

        logger.error("All TTS attempts failed for text: %.50s...", text)

    def _cache_tts(self):
        """获取 ttscache 兜底引擎（延迟创建并复用）。"""
        import importlib

        import registry

        # 显式导入，确保 @register("tts", "ttscache") 已经执行。
        # 这里不能依赖「别的模块碰巧导入过 tts.cache」：离线兜底一旦静默失效，
        # 断网时数字人会直接哑掉，而这正是本机制要防的场景。
        importlib.import_module("tts.cache")

        if self._fallback_tts is None or getattr(self._fallback_tts, "_tts_name", "") != "ttscache":
            self._fallback_tts = registry.create(
                "tts", "ttscache", opt=self.opt, parent=self.parent,
            )
            self._fallback_tts._tts_name = "ttscache"
        return self._fallback_tts

    def _play_cached(self, msg: tuple[str, dict]) -> bool:
        """命中已预热的本地语音时直接播放；未命中返回 False，不影响原有链路。"""
        if self._tts_name == "ttscache":
            return False  # ttscache 自己就是先查缓存
        if "ttscache" not in TTS_FALLBACK_CHAIN.get(self._tts_name, []):
            return False
        try:
            engine = self._cache_tts()
            engine.state = State.RUNNING
            return engine.play_if_cached(msg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("本地语音缓存查询失败，继续在线合成: %s", exc)
            return False

    def _try_fallback_tts(self, fallback_name: str, msg: tuple[str, dict]):
        if self.is_cancelled():
            return
        """使用备用 TTS 引擎合成。"""
        import registry
        text, textevent = msg

        if self._fallback_tts is None or getattr(self._fallback_tts, "_tts_name", "") != fallback_name:
            # 延迟创建备用 TTS 实例（不同备用引擎各自建实例）
            self._fallback_tts = registry.create(
                "tts", fallback_name,
                opt=self.opt, parent=self.parent,
            )
            self._fallback_tts._tts_name = fallback_name

        self._fallback_tts.state = State.RUNNING
        self._fallback_tts.txt_to_audio(msg)

    def is_cancelled(self) -> bool:
        return (
            self.state != State.RUNNING
            or self._active_generation != self._generation
        )

    def _stream_to_avatar(self, stream, text: str, textevent: dict):
        """Send fixed-size PCM chunks and close short utterances cleanly."""
        stream = np.asarray(stream, dtype=np.float32).reshape(-1)
        if stream.size == 0:
            raise RuntimeError("TTS returned empty audio")

        total = stream.size
        index = 0
        first = True
        generation = getattr(self.parent, "_talk_generation", 0)
        while index < total and not self.is_cancelled():
            end = min(index + self.chunk, total)
            frame = stream[index:end]
            if frame.size < self.chunk:
                frame = np.pad(frame, (0, self.chunk - frame.size))

            eventpoint = {}
            if first:
                eventpoint["status"] = "start"
                first = False
            elif end >= total:
                eventpoint["status"] = "end"
            eventpoint.update(**(textevent or {}))
            if not self.parent.put_audio_frame(frame, eventpoint, generation):
                break
            index = end

        if first is False and total <= self.chunk and not self.is_cancelled():
            eventpoint = {"status": "end", "text": text}
            eventpoint.update(**(textevent or {}))
            self.parent.put_audio_frame(
                np.zeros(self.chunk, dtype=np.float32),
                eventpoint,
                generation,
            )

    def txt_to_audio(self, msg: tuple[str, dict]):
        """子类实现具体的 TTS 合成逻辑。"""
        pass

    def stop_tts(self):
        pass
