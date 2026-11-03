###############################################################################
#  应用上下文 (Application Context)
#  统一管理全局依赖，替代分散的全局变量和单例导入
###############################################################################

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from utils.logger import logger


@dataclass
class AppContext:
    """共享应用上下文，集中管理所有核心组件引用。

    替代原有的全局变量:
      - opt (argparse.Namespace)
      - model (torch model)
      - global_avatars (dict)
      - rtc_manager
    """

    # ── 配置 ──────────────────────────────────────────────────────────
    opt: Any = None  # argparse.Namespace

    # ── 模型与资源 ────────────────────────────────────────────────────
    model: Any = None  # 数字人推理模型
    avatars: Dict[str, Any] = field(default_factory=dict)  # avatar_id → payload

    # ── 管理器引用（延迟注入，避免循环导入）──────────────────────────
    rtc_manager: Any = None
    session_manager: Any = None
    live_studio: Any = None
    llm_config: Any = None
    product_catalog: Any = None
    subtitle_ocr: Any = None

    # ── 运行状态 ──────────────────────────────────────────────────────
    started_at: float = field(default_factory=time.time)
    _health_components: Dict[str, bool] = field(default_factory=dict)

    def health_report(self) -> Dict:
        """生成健康检查报告。"""
        import torch

        report = {
            "status": "ok",
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "transport": getattr(self.opt, "transport", "unknown") if self.opt else "unknown",
            "model": getattr(self.opt, "model", "unknown") if self.opt else "unknown",
            "avatar_id": getattr(self.opt, "avatar_id", "unknown") if self.opt else "unknown",
        }

        # GPU 状态
        if torch.cuda.is_available():
            report["gpu"] = {
                "name": torch.cuda.get_device_name(0),
                "memory_allocated_mb": round(torch.cuda.memory_allocated(0) / 1024**2, 1),
                "memory_reserved_mb": round(torch.cuda.memory_reserved(0) / 1024**2, 1),
                "memory_total_mb": round(torch.cuda.get_device_properties(0).total_memory / 1024**2, 1),
            }
        else:
            report["gpu"] = {"available": False}

        # 会话状态
        if self.session_manager:
            active = sum(1 for s in self.session_manager.sessions.values() if s is not None)
            report["sessions"] = {
                "active": active,
                "max": self.session_manager.max_session,
            }

        # LLM 状态
        if self.llm_config:
            try:
                report["llm"] = self.llm_config.check_health()
            except Exception as e:
                report["llm"] = {"available": False, "error": str(e)}

        # 组件健康
        report["components"] = dict(self._health_components)

        return report

    def set_component_health(self, name: str, healthy: bool):
        self._health_components[name] = healthy


# 全局上下文实例（向后兼容旧代码的逐步迁移）
_app_context: Optional[AppContext] = None


def init_context(opt, model=None, avatars=None) -> AppContext:
    """初始化全局应用上下文。"""
    global _app_context
    _app_context = AppContext(
        opt=opt,
        model=model,
        avatars=avatars or {},
    )
    logger.info("AppContext initialized: transport=%s model=%s",
                opt.transport if opt else "?", opt.model if opt else "?")
    return _app_context


def get_context() -> Optional[AppContext]:
    """获取当前应用上下文。"""
    return _app_context


def require_context() -> AppContext:
    """获取上下文，不存在时抛出异常。"""
    if _app_context is None:
        raise RuntimeError("AppContext not initialized. Call init_context() first.")
    return _app_context