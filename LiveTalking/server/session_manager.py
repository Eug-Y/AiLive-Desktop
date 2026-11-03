###############################################################################
#  全局会话管理器 (Session Manager)
###############################################################################

import asyncio
import threading
import time
import uuid
from typing import Dict, Optional

from utils.logger import logger
from avatars.base_avatar import BaseAvatar


class MaxSessionError(Exception):
    """会话数达到上限时抛出"""
    pass


def _rand_session_id() -> str:
    """生成 UUID session ID"""
    return str(uuid.uuid4())


class SessionManager:
    """
    全局数字人会话管理器。

    统一管理 avatar_sessions 生命周期，并在脱离 WebRTC 时依然保持服务可用。

    优化点：
    - 会话超时自动清理（默认 5 分钟无活动）
    - 清理时释放 GPU 显存
    - 线程安全的状态追踪
    """

    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not hasattr(self, "initialized"):
            self.sessions: Dict[str, BaseAvatar] = {}
            self._session_last_active: Dict[str, float] = {}
            self._session_lock = threading.RLock()
            self.build_session_fn = None
            self.max_session = 1
            self._idle_timeout_seconds = 300  # 5 分钟无活动自动清理
            self._cleanup_thread: Optional[threading.Thread] = None
            self._cleanup_stop = threading.Event()
            self.initialized = True

    def set_max_session(self, n: int):
        """设置最大并发会话数"""
        with self._session_lock:
            self.max_session = max(1, n)

    def init_builder(self, build_session_fn):
        """配置用于构建 avatar_session 的工厂函数"""
        self.build_session_fn = build_session_fn

    def get_session(self, sessionid: str) -> Optional[BaseAvatar]:
        """获取已存活的会话，同时更新最后活跃时间。"""
        with self._session_lock:
            session = self.sessions.get(sessionid)
            if session is not None:
                self._session_last_active[sessionid] = time.time()
        return session

    def has_session(self, sessionid: str) -> bool:
        """检查会话是否存在"""
        with self._session_lock:
            return sessionid in self.sessions and self.sessions[sessionid] is not None

    @property
    def active_count(self) -> int:
        """活跃会话数"""
        with self._session_lock:
            return sum(1 for s in self.sessions.values() if s is not None)

    async def create_session(self, params: dict, sessionid: str = None) -> str:
        """
        在异步环境中创建一个新会话
        如果 sessionid 为 None，则自动生成。
        """
        if self.build_session_fn is None:
            raise Exception("SessionManager builder not initialized")

        if sessionid is None:
            sessionid = _rand_session_id()

        # 检查是否达到最大会话数
        with self._session_lock:
            if sessionid in self.sessions:
                raise ValueError(f"Session already exists: {sessionid}")
            reserved_count = len(self.sessions)
            if reserved_count >= self.max_session:
                raise MaxSessionError(
                    f"Maximum session limit reached ({reserved_count}/{self.max_session})"
                )
            self.sessions[sessionid] = None
            self._session_last_active[sessionid] = time.time()

        logger.info('Creating sessionid=%s, current session num=%d', sessionid, reserved_count)
        # 预先占位防止重复
        with self._session_lock:
            self.sessions[sessionid] = None
            self._session_last_active[sessionid] = time.time()

        # 在线程池中构建 session（加载模型非常耗时）
        try:
            avatar_session = await asyncio.get_running_loop().run_in_executor(
                None, self.build_session_fn, sessionid, params
            )
        except Exception:
            with self._session_lock:
                self.sessions.pop(sessionid, None)
                self._session_last_active.pop(sessionid, None)
            raise
        with self._session_lock:
            self.sessions[sessionid] = avatar_session
            self._session_last_active[sessionid] = time.time()

        # 启动后台清理线程（惰性启动）
        self._ensure_cleanup_thread()
        return sessionid

    def add_session(self, sessionid: str, avatar_session: BaseAvatar):
        """同步添加静态或外部管理的会话（供非服务端入口调用）"""
        with self._session_lock:
            self.sessions[sessionid] = avatar_session
            self._session_last_active[sessionid] = time.time()
        self._ensure_cleanup_thread()

    def remove_session(self, sessionid: str):
        """销毁会话资源，释放 GPU 显存。"""
        session = None
        with self._session_lock:
            if sessionid in self.sessions:
                session = self.sessions.pop(sessionid, None)
                self._session_last_active.pop(sessionid, None)

        if session is not None:
            logger.info("Removing session %s - releasing resources", sessionid)
            try:
                self._cleanup_session(session)
            except Exception as e:
                logger.warning("Error during session cleanup: %s", e)

    def _cleanup_session(self, session: BaseAvatar):
        """释放单个 session 的资源。"""
        # 停止录制
        if getattr(session, 'recording', False):
            try:
                session.stop_recording()
            except Exception:
                pass

        # 释放 CUDA 缓存
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                logger.info("CUDA cache cleared after session cleanup")
        except Exception:
            pass

    def _ensure_cleanup_thread(self):
        """确保后台清理线程正在运行。"""
        if self._cleanup_thread is None or not self._cleanup_thread.is_alive():
            self._cleanup_stop.clear()
            self._cleanup_thread = threading.Thread(
                target=self._cleanup_idle_sessions,
                daemon=True,
                name="session-cleanup",
            )
            self._cleanup_thread.start()

    def _cleanup_idle_sessions(self):
        """后台线程：定期清理超时闲置会话。"""
        while not self._cleanup_stop.wait(timeout=60):
            now = time.time()
            to_remove = []
            with self._session_lock:
                for sid, last_active in list(self._session_last_active.items()):
                    if now - last_active > self._idle_timeout_seconds:
                        to_remove.append(sid)

            for sid in to_remove:
                logger.info("Auto-cleaning idle session: %s (idle for %.0fs)",
                            sid, now - self._session_last_active.get(sid, 0))
                self.remove_session(sid)

    def shutdown(self):
        """关闭所有会话并停止清理线程。"""
        self._cleanup_stop.set()
        with self._session_lock:
            session_ids = list(self.sessions.keys())
        for sid in session_ids:
            self.remove_session(sid)
        logger.info("SessionManager shutdown complete")


# 单例抛出
session_manager = SessionManager()
