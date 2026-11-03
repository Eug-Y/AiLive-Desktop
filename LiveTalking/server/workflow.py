###############################################################################
#  工作流编排器 (Workflow Orchestrator)
#  一键开播/下播 — 自动串联所有步骤，5 秒完成全链路初始化
###############################################################################

import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional

from utils.logger import logger


class WorkflowStep(Enum):
    """工作流步骤状态"""
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass
class StepResult:
    step: str
    label: str
    status: WorkflowStep
    duration_ms: float = 0
    message: str = ""
    detail: dict = None

    def to_dict(self):
        return {
            "step": self.step,
            "label": self.label,
            "status": self.status.value,
            "duration_ms": round(self.duration_ms, 1),
            "message": self.message,
            "detail": self.detail or {},
        }


class WorkflowOrchestrator:
    """一键开播/下播编排器。

    用法:
        orchestrator = WorkflowOrchestrator()
        result = orchestrator.start_live()   # 一键开播
        result = orchestrator.stop_live()    # 一键下播
        status = orchestrator.status()       # 统一仪表盘
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized"):
            return
        self._initialized = True
        self._lock = threading.RLock()
        self._last_start_steps: List[StepResult] = []
        self._last_stop_steps: List[StepResult] = []
        self._start_duration_ms: float = 0
        self._stop_duration_ms: float = 0

    # ── 一键开播 ──────────────────────────────────────────────────────

    def start_live(self, params: dict = None) -> dict:
        """一键开播：自动完成所有初始化步骤。

        步骤：
        1. 验证账号 → 2. 检查商品 → 3. 初始化排品队列
        4. 开始排品轮播 → 5. 启动氛围管理 → 6. 启动字幕 OCR（可选）
        7. 标记开播状态 → 8. 返回仪表盘

        Args:
            params: 可选参数
                - account_id: 指定账号（默认用当前活跃账号）
                - play_mode: 排品模式 (loop/sequential/weighted)
                - atmosphere_interval: 氛围间隔秒数
                - subtitle_ocr: 是否启动字幕 OCR
        """
        params = params or {}
        steps: List[StepResult] = []
        total_start = time.perf_counter()

        def _step(step_id: str, label: str, fn):
            ts = time.perf_counter()
            try:
                result = fn()
                duration = (time.perf_counter() - ts) * 1000
                steps.append(StepResult(step_id, label, WorkflowStep.DONE, duration, "ok", result))
                return result
            except Exception as e:
                duration = (time.perf_counter() - ts) * 1000
                steps.append(StepResult(step_id, label, WorkflowStep.FAILED, duration, str(e)))
                raise

        try:
            from server.account_manager import account_manager
            from server.live_studio import live_studio_manager

            # ── 步骤 1: 验证账号 ──
            account_id = params.get("account_id", account_manager.active_account_id)
            if not account_id:
                raise ValueError("没有活跃账号，请先创建或切换账号")
            account = account_manager.get(account_id)
            if not account:
                raise ValueError(f"账号 {account_id} 不存在")
            _step("account", f"验证账号: {account.name}", lambda: account.to_dict())

            live_status = live_studio_manager.status()
            if not live_status.get("output_connected") and not params.get("allow_without_output"):
                raise ValueError("请先打开直播输出窗口并点击“启动直播画面”")
            _step("output", "数字人输出已连接", lambda: {
                "sessionid": live_status.get("sessionid", ""),
            })

            # ── 步骤 2: 检查商品 ──
            from server.sales_catalog import product_catalog
            products = product_catalog.list_ready()
            if not products:
                raise ValueError("没有可用的已核对商品，请先在商品库录入并核对至少 1 个商品")
            _step("products", f"检查商品: {len(products)} 个可用", lambda: {"count": len(products)})

            # ── 步骤 3-4: 单品播报或多品排品轮播 ──
            if len(products) >= 2:
                mode = params.get("play_mode", "loop")
                pq_status = live_studio_manager.init_play_queue(mode)
                _step("playqueue", f"排品队列: {pq_status['total_products']} 个商品 ({mode})",
                      lambda: pq_status)
                live_studio_manager.start_play_queue()
                _step("autopilot", "排品轮播已启动", lambda: {"autopilot": True})
            else:
                current = product_catalog.current()
                if not current:
                    live_studio_manager.set_current_product(products[0]["id"])
                live_studio_manager.generate_product_scripts(products[0]["id"], apply=True)
                live_studio_manager.set_autopilot(True)
                _step("autopilot", "单品自动话术已启动", lambda: {
                    "autopilot": True,
                    "product_id": products[0]["id"],
                })

            # ── 步骤 5: 启动氛围管理 ──
            interval = params.get("atmosphere_interval", 180)
            atm = live_studio_manager.start_atmosphere(interval)
            _step("atmosphere", f"氛围管理: 每 {interval}s 自动互动", lambda: atm)

            # ── 步骤 6: 字幕 OCR（可选）──
            if params.get("subtitle_ocr"):
                try:
                    from server.subtitle_ocr import subtitle_ocr_manager
                    ocr_status = subtitle_ocr_manager.start(params.get("ocr_config", {}))
                    _step("ocr", "字幕 OCR 已启动", lambda: ocr_status)
                except Exception as e:
                    _step("ocr", f"字幕 OCR 跳过: {e}", lambda: {"skipped": True})
            else:
                _step("ocr", "字幕 OCR 未启用", lambda: {"skipped": True})

            # ── 步骤 7: 标记开播 ──
            live_studio_manager.mark_live_started()
            _step("live", "直播状态: 已开播", lambda: {"live_state": "live"})

            # ── 成功 ──
            self._start_duration_ms = (time.perf_counter() - total_start) * 1000
            self._last_start_steps = steps
            logger.info("Workflow: start_live completed in %.0fms", self._start_duration_ms)
            return self._build_result("start", steps, self._start_duration_ms)

        except Exception as e:
            self._start_duration_ms = (time.perf_counter() - total_start) * 1000
            self._last_start_steps = steps
            logger.error("Workflow: start_live failed: %s", e)
            return self._build_result("start", steps, self._start_duration_ms, str(e))

    # ── 一键下播 ──────────────────────────────────────────────────────

    def stop_live(self) -> dict:
        """一键下播：自动关闭所有模块，导出数据。

        步骤：
        1. 停止氛围管理 → 2. 停止排品轮播 → 3. 停止字幕 OCR
        4. 标记下播 → 5. 导出数据报告 → 6. 返回汇总
        """
        steps: List[StepResult] = []
        total_start = time.perf_counter()

        def _step(step_id: str, label: str, fn):
            ts = time.perf_counter()
            try:
                result = fn()
                duration = (time.perf_counter() - ts) * 1000
                steps.append(StepResult(step_id, label, WorkflowStep.DONE, duration, "ok", result))
                return result
            except Exception as e:
                duration = (time.perf_counter() - ts) * 1000
                steps.append(StepResult(step_id, label, WorkflowStep.FAILED, duration, str(e)))

        try:
            from server.live_studio import live_studio_manager

            # ── 步骤 1: 停止氛围 ──
            _step("atmosphere", "停止氛围管理", lambda: live_studio_manager.stop_atmosphere())

            # ── 步骤 2: 停止字幕 OCR ──
            try:
                from server.subtitle_ocr import subtitle_ocr_manager
                _step("ocr", "停止字幕 OCR", lambda: subtitle_ocr_manager.stop())
            except Exception:
                _step("ocr", "字幕 OCR 已停止（或未运行）", lambda: {"stopped": True})

            # ── 步骤 3: 停止排品 ──
            _step("autopilot", "停止自动排品", lambda: live_studio_manager.set_autopilot(False))

            # ── 步骤 4: 标记下播 ──
            live_studio_manager.mark_live_ended()
            _step("live", "标记直播结束", lambda: {"live_state": "ended"})

            # ── 步骤 5: 导出数据 ──
            try:
                analytics = live_studio_manager.export_analytics()
                _step("analytics", "数据报告已生成",
                      lambda: {"total_commission": analytics.get("commission", {}).get("total_estimated_commission", 0)})
            except Exception as e:
                _step("analytics", f"数据导出失败: {e}", lambda: {})

            self._stop_duration_ms = (time.perf_counter() - total_start) * 1000
            self._last_stop_steps = steps
            logger.info("Workflow: stop_live completed in %.0fms", self._stop_duration_ms)
            return self._build_result("stop", steps, self._stop_duration_ms)

        except Exception as e:
            self._stop_duration_ms = (time.perf_counter() - total_start) * 1000
            self._last_stop_steps = steps
            logger.error("Workflow: stop_live failed: %s", e)
            return self._build_result("stop", steps, self._stop_duration_ms, str(e))

    # ── 统一仪表盘 ────────────────────────────────────────────────────

    def status(self) -> dict:
        """统一仪表盘：聚合所有模块状态。"""
        result = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "account": None,
            "live": None,
            "health": {"score": 0, "checks": [], "suggestions": []},
        }

        # ── 账号 ──
        try:
            from server.account_manager import account_manager
            active = account_manager.active_account
            if active:
                result["account"] = {
                    "id": active.id,
                    "name": active.name,
                    "platform": active.platform,
                    "status": active.status,
                    "total_live_sessions": active.total_live_sessions,
                    "total_commission": round(active.total_commission, 2),
                }
        except Exception as e:
            result["account"] = {"error": str(e)}

        # ── 直播状态 ──
        try:
            from server.live_studio import live_studio_manager
            result["live"] = live_studio_manager.status()
        except Exception as e:
            result["live"] = {"error": str(e)}

        # ── 健康检查 ──
        result["health"] = self._health_check()

        return result

    def _health_check(self) -> dict:
        """健康检查 + 智能建议。"""
        checks = []
        suggestions = []
        score = 100

        try:
            from server.account_manager import account_manager
            active = account_manager.active_account
            if not active:
                checks.append({"label": "活跃账号", "passed": False, "level": "blocker"})
                suggestions.append("请先创建或切换一个账号")
                score -= 30
            else:
                checks.append({"label": f"活跃账号: {active.name}", "passed": True, "level": "ok"})
        except Exception:
            checks.append({"label": "账号管理器", "passed": False, "level": "blocker"})
            score -= 30

        try:
            from server.sales_catalog import product_catalog
            products = product_catalog.list_ready()
            if not products:
                checks.append({"label": "可用商品", "passed": False, "level": "blocker"})
                suggestions.append("请录入并核对至少 1 个商品")
                score -= 30
            else:
                checks.append({"label": f"可用商品: {len(products)} 个", "passed": True, "level": "ok"})
        except Exception:
            checks.append({"label": "商品库", "passed": False, "level": "blocker"})
            score -= 30

        try:
            from server.session_manager import session_manager
            from server.live_studio import live_studio_manager
            ls = live_studio_manager.status()
            if ls.get("output_connected"):
                checks.append({"label": "数字人输出已连接", "passed": True, "level": "ok"})
            else:
                checks.append({"label": "数字人输出未连接", "passed": False, "level": "warning"})
                suggestions.append("请打开直播输出窗口并点击「启动直播画面」")
                score -= 10
        except Exception:
            pass

        try:
            from llm import llm_config_manager
            llm_health = llm_config_manager.check_health()
            if llm_health.get("available"):
                checks.append({"label": "大模型已连接", "passed": True, "level": "ok"})
            else:
                checks.append({"label": "大模型未配置", "passed": False, "level": "warning"})
                suggestions.append("请配置大模型 API Key（大模型管理页面）")
                score -= 10
        except Exception:
            pass

        return {
            "score": max(0, score),
            "checks": checks,
            "suggestions": suggestions,
            "ready": score >= 70,
        }

    # ── 预检 ──────────────────────────────────────────────────────────

    def preflight(self) -> dict:
        """开播前完整预检。"""
        checks = []

        def add(label, passed, level="blocker", detail=""):
            checks.append({"label": label, "passed": passed, "level": level, "detail": detail})

        # 1. 账号
        try:
            from server.account_manager import account_manager
            active = account_manager.active_account
            add("账号已选择", bool(active),
                detail=active.name if active else "")
            if active and active.status == "banned":
                add("账号未被封禁", False, "blocker", f"账号 {active.name} 已被封禁")
        except Exception as e:
            add("账号管理器正常", False, "blocker", str(e))

        # 2. 商品
        try:
            from server.sales_catalog import product_catalog
            products = product_catalog.list_ready()
            add("有可用商品", len(products) > 0, "blocker",
                detail=f"{len(products)} 个已核对商品")
        except Exception as e:
            add("商品库正常", False, "blocker", str(e))

        # 3. 数字人
        try:
            from server.live_studio import live_studio_manager
            ls = live_studio_manager.status()
            add("数字人输出已连接", ls.get("output_connected", False), "warning",
                detail="请打开输出窗口" if not ls.get("output_connected") else "已连接")
        except Exception as e:
            add("直播引擎正常", False, "warning", str(e))

        # 4. LLM
        try:
            from llm import llm_config_manager
            llm_health = llm_config_manager.check_health()
            add("大模型已配置", llm_health.get("available", False), "warning",
                detail="已连接" if llm_health.get("available") else "请配置 API Key")
        except Exception as e:
            add("大模型", False, "warning", str(e))

        blockers = [c for c in checks if c["level"] == "blocker" and not c["passed"]]
        warnings = [c for c in checks if c["level"] == "warning" and not c["passed"]]

        return {
            "ready": not blockers,
            "passed": sum(1 for c in checks if c["passed"]),
            "total": len(checks),
            "blockers": blockers,
            "warnings": warnings,
            "checks": checks,
        }

    # ── 内部方法 ──────────────────────────────────────────────────────

    def _build_result(self, action: str, steps: List[StepResult], duration_ms: float,
                      error: str = None) -> dict:
        succeeded = sum(1 for s in steps if s.status == WorkflowStep.DONE)
        failed = sum(1 for s in steps if s.status == WorkflowStep.FAILED)
        return {
            "action": action,
            "success": error is None,
            "error": error,
            "duration_ms": round(duration_ms, 1),
            "steps": [s.to_dict() for s in steps],
            "summary": f"{succeeded} 成功, {failed} 失败, {len(steps)} 总计",
            "dashboard": self.status() if error is None else None,
        }


# 单例
workflow = WorkflowOrchestrator()
