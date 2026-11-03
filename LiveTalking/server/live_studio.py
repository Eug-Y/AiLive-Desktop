import json
import os
import random
import threading
import time
from collections import deque
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

from llm import get_llm_public_config, get_llm_executor, llm_response
from server.sales_catalog import product_catalog, sales_compliance, SCRIPT_TYPES
from server.session_manager import session_manager
from server.subtitle_ocr import subtitle_ocr_manager
from server.play_queue import PlayQueue, PlayMode
from server.intent_router import intent_router
from tts.cache import coverage as tts_cache_coverage
from utils.logger import logger


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = PROJECT_ROOT / "data" / "live_studio.json"

# 反录播护栏：固定顺序 + 固定节拍的循环播报，是平台判定"录播/无人直播"的典型特征。
LOOP_GAP_FLOOR_SECONDS = 15.0   # 开启循环时，两条话术之间的最小间隔
GAP_JITTER_RATIO = 0.35         # 间隔随机抖动比例，避免机械节拍被识别

DEFAULT_SETTINGS = {
    "platform": "douyin",
    "title": "AI 数字人直播间",
    "ai_label": "AI 数字人 · 内容由人工值守",
    "layout": "contain",
    "script_gap_seconds": 30.0,
    "repeat_scripts": True,
    "shuffle_scripts": True,   # 防录播：每轮打乱话术顺序
}

DEFAULT_WORKFLOW_CHECKS = {
    "permissions_confirmed": False,
    "product_card_matched": False,
    "platform_product_attached": False,
    "audio_video_checked": False,
    "private_rehearsal_done": False,
    "human_supervisor_ready": False,
    "supervision_visible": False,
    "category_qualified": False,
}


class LiveState(Enum):
    """直播状态机"""
    IDLE = "idle"               # 未开始
    PREPARING = "preparing"     # 准备中（录入商品、生成话术）
    READY = "ready"             # 准备就绪（检查全部通过）
    REHEARSAL = "rehearsal"     # 私密彩排中
    LIVE = "live"               # 正式开播
    STOPPING = "stopping"       # 停播中
    ENDED = "ended"             # 已结束


class LiveStudioManager:
    """Coordinates the clean live output, scripts and operator takeover.

    优化点：
    - 直播状态机 (LiveState) 管理完整生命周期
    - 事件驱动 Autopilot (Condition) 替代轮询
    - 使用 LLM 专用线程池
    - 商品切换日志
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        # ── 事件驱动 Autopilot ──
        self._dispatch_condition = threading.Condition(self._lock)
        self._dispatch_requested = False

        self._settings = dict(DEFAULT_SETTINGS)
        self._workflow_checks = dict(DEFAULT_WORKFLOW_CHECKS)
        self._scripts: List[str] = []
        self._script_index = 0
        self._play_order: List[int] = []   # 防录播：本轮的播报顺序（scripts 的下标排列）
        self._gap_target = 0.0             # 本次播报后的实际等待时长（含随机抖动）
        self._sessionid = ""
        self._account_id: str = ""  # 所属账号 ID
        self._output_registered_at: Optional[float] = None
        self._autopilot = False
        self._thinking = False
        self._last_dispatch_at = 0.0
        self._last_spoken_text = ""
        self._last_reply = ""
        self._history = deque(maxlen=80)

        # ── 状态机 ──
        self._live_state = LiveState.IDLE
        self._live_started_at: Optional[float] = None
        self._stat_product_switches: int = 0
        self._stat_scripts_played: int = 0
        self._stat_manual_interventions: int = 0
        self._product_switch_log: deque = deque(maxlen=50)

        # ── 排品队列（百货直播核心）──
        self._play_queue = PlayQueue()
        self._use_play_queue = False  # 是否启用排品模式
        self._play_queue_thread: Optional[threading.Thread] = None

        # ── 氛围管理 ──
        self._atmosphere_enabled = False
        self._atmosphere_thread: Optional[threading.Thread] = None
        self._atmosphere_interval = 180  # 每 3 分钟发送氛围话术
        self._atmosphere_last_at = 0.0
        self._welcome_messages = [
            "欢迎新朋友来到直播间，点个关注不迷路！",
            "刚进来的朋友，先点关注，马上发福利。",
            "欢迎欢迎！喜欢主播的点点小红心。",
        ]
        self._thank_messages = [
            "感谢关注！有问题直接打在公屏上。",
            "谢谢支持！今天还有很多好货要给大家看。",
        ]
        self._interact_messages = [
            "在看的扣个 1，我看看有多少人在线。",
            "小红心点到 1000，马上给大家上福利款。",
            "想要什么类型的商品，打在公屏上告诉我。",
            "还没加粉丝团的抓紧，福利优先给粉丝。",
        ]

        self._load()
        self._worker = threading.Thread(
            target=self._autopilot_loop,
            daemon=True,
            name="live-studio-autopilot",
        )
        self._worker.start()

    # ── 状态机方法 ──────────────────────────────────────────────────────

    def reload_for_account(self, account_id: str):
        """切换到指定账号的数据，重新加载设置和话术。"""
        with self._lock:
            self._account_id = account_id
            self._scripts = []
            self._script_index = 0
            self._play_order = []
            self._autopilot = False
            self._use_play_queue = False
            self._play_queue = PlayQueue()
            self._settings = dict(DEFAULT_SETTINGS)
            self._workflow_checks = dict(DEFAULT_WORKFLOW_CHECKS)
            self._load()
            # 从账号配置覆盖设置
            from server.account_manager import account_manager
            account = account_manager.get(account_id)
            if account:
                self._settings["title"] = account.live_title
                self._settings["ai_label"] = account.ai_label
                self._settings["script_gap_seconds"] = account.script_gap_seconds
            # 账号侧覆盖后必须重新过一遍反录播护栏
            self._normalize_settings()
            logger.info("LiveStudio reloaded for account %s", account_id)

    def _transition_state(self, new_state: LiveState, reason: str = ""):
        old = self._live_state
        self._live_state = new_state
        if new_state == LiveState.LIVE:
            self._live_started_at = time.time()
        elif new_state in (LiveState.ENDED, LiveState.IDLE):
            self._live_started_at = None
        logger.info("LiveState: %s → %s (reason: %s)", old.value, new_state.value, reason or "—")
        self._add_history("state", f"状态变更: {old.value} → {new_state.value}")

    def mark_live_started(self):
        """标记正式开播。"""
        with self._lock:
            self._transition_state(LiveState.LIVE, "operator started live")

    def mark_live_ended(self):
        """标记直播结束。"""
        with self._lock:
            self._autopilot = False
            self._dispatch_requested = False
            self._transition_state(LiveState.ENDED, "operator ended live")
            self._stat_product_switches = 0
            self._stat_scripts_played = 0
            self._stat_manual_interventions = 0

    # ── 持久化 ──────────────────────────────────────────────────────────

    def _load(self):
        state_file = self._get_state_file()
        if not state_file.exists():
            self._transition_state(LiveState.IDLE, "first run")
            return
        try:
            saved = json.loads(state_file.read_text(encoding="utf-8"))
            if isinstance(saved.get("settings"), dict):
                self._settings.update(saved["settings"])
            if isinstance(saved.get("scripts"), list):
                self._scripts = [str(item).strip() for item in saved["scripts"] if str(item).strip()]
            if isinstance(saved.get("workflow_checks"), dict):
                for key in DEFAULT_WORKFLOW_CHECKS:
                    if key in saved["workflow_checks"]:
                        self._workflow_checks[key] = bool(saved["workflow_checks"][key])
            self._normalize_settings()
            self._transition_state(LiveState.IDLE, "state loaded")
        except Exception:
            logger.exception("Failed to load live studio state")

    def _get_state_file(self) -> Path:
        if self._account_id:
            from server.account_manager import account_manager
            return account_manager.get_account_data_path(self._account_id, "live_studio.json")
        return STATE_FILE

    def _persist(self):
        state_file = self._get_state_file()
        state_file.parent.mkdir(parents=True, exist_ok=True)
        temp = state_file.with_suffix(".tmp")
        temp.write_text(
            json.dumps(
                {
                    "settings": self._settings,
                    "scripts": self._scripts,
                    "workflow_checks": self._workflow_checks,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(temp, state_file)

    def _normalize_settings(self):
        if self._settings.get("platform") not in {"douyin", "wechat_channels", "other"}:
            self._settings["platform"] = "douyin"
        if self._settings.get("layout") not in {"contain", "cover"}:
            self._settings["layout"] = "contain"
        self._settings["title"] = str(self._settings.get("title", DEFAULT_SETTINGS["title"]))[:80]
        self._settings["ai_label"] = str(
            self._settings.get("ai_label", DEFAULT_SETTINGS["ai_label"])
        )[:80]
        self._settings["script_gap_seconds"] = min(
            300.0, max(3.0, float(self._settings.get(
                "script_gap_seconds", DEFAULT_SETTINGS["script_gap_seconds"]
            )))
        )
        self._settings["repeat_scripts"] = bool(self._settings.get("repeat_scripts", True))
        self._settings["shuffle_scripts"] = bool(
            self._settings.get("shuffle_scripts", DEFAULT_SETTINGS["shuffle_scripts"])
        )
        if self._settings["repeat_scripts"]:
            # 反录播：循环播放时不允许低于间隔下限（含旧数据自动上浮）
            self._settings["script_gap_seconds"] = max(
                LOOP_GAP_FLOOR_SECONDS, self._settings["script_gap_seconds"]
            )

    def _add_history(self, event_type: str, text: str, status: str = "ok"):
        with self._lock:
            self._history.appendleft({
                "time": time.time(),
                "type": event_type,
                "text": str(text)[:500],
                "status": status,
            })

    def _reset_live_checks(self, include_permissions: bool = False):
        keys = list(DEFAULT_WORKFLOW_CHECKS)
        if not include_permissions:
            keys.remove("permissions_confirmed")
        for key in keys:
            self._workflow_checks[key] = False

    def _reset_product_checks(self):
        for key in ("product_card_matched", "platform_product_attached", "private_rehearsal_done"):
            self._workflow_checks[key] = False

    # ── 事件驱动：通知 Autopilot ────────────────────────────────────────

    def _notify_autopilot(self):
        """通知 autopilot 线程有新事件（说话结束、话术更新等）。"""
        with self._lock:
            self._dispatch_requested = True
            self._dispatch_condition.notify_all()

    # ── 公开 API ────────────────────────────────────────────────────────

    def register_output(self, sessionid: str):
        sessionid = str(sessionid).strip()
        if not session_manager.has_session(sessionid):
            raise ValueError("数字人输出会话不存在")
        with self._lock:
            self._sessionid = sessionid
            self._output_registered_at = time.time()
            self._workflow_checks["audio_video_checked"] = False
            self._persist()
            self._transition_state(LiveState.PREPARING, "output registered")
        self._add_history("system", "直播输出窗口已连接")
        logger.info("Live output registered: sessionid=%s", sessionid)
        return self.status()

    def unregister_output(self, sessionid: str):
        with self._lock:
            if not sessionid or sessionid == self._sessionid:
                self._sessionid = ""
                self._output_registered_at = None
                self._autopilot = False
                self._dispatch_requested = False
                if self._live_state not in (LiveState.ENDED, LiveState.IDLE):
                    self._transition_state(LiveState.IDLE, "output unregistered")
        self._add_history("system", "直播输出窗口已断开")
        return self.status()

    def update_settings(self, params: Dict):
        with self._lock:
            for key in DEFAULT_SETTINGS:
                if key in params:
                    self._settings[key] = params[key]
            self._normalize_settings()
            self._gap_target = 0.0  # 让新的间隔立即生效
            self._workflow_checks["audio_video_checked"] = False
            self._workflow_checks["private_rehearsal_done"] = False
            self._persist()
        self._add_history("settings", "直播设置已更新")
        return self.status()

    def set_scripts(self, scripts: List[str], repeat: Optional[bool] = None):
        if not isinstance(scripts, list):
            raise ValueError("scripts must be a list")
        cleaned = []
        for item in scripts[:500]:
            text = str(item).strip()
            if text:
                text = text[:500]
                sales_compliance.assert_safe(text, product_catalog.current())
                cleaned.append(text)
        with self._lock:
            self._scripts = cleaned
            self._script_index = 0
            self._play_order = []
            self._rebuild_play_order()
            if repeat is not None:
                self._settings["repeat_scripts"] = bool(repeat)
            if not cleaned:
                self._autopilot = False
            self._workflow_checks["private_rehearsal_done"] = False
            self._persist()
            self._notify_autopilot()
        self._add_history("scripts", f"话术队列已更新，共 {len(cleaned)} 条")
        return self.status()

    def set_autopilot(self, enabled: bool):
        enabled = bool(enabled)
        with self._lock:
            if enabled:
                if not session_manager.has_session(self._sessionid):
                    raise ValueError("请先打开并连接直播输出窗口")
                if not self._scripts:
                    raise ValueError("请先保存至少一条直播话术")
            self._autopilot = enabled
            if enabled:
                self._last_dispatch_at = 0.0
                self._gap_target = 0.0
                self._script_index = 0
                self._rebuild_play_order()
                self._notify_autopilot()
        self._add_history("autopilot", "自动话术已启动" if enabled else "自动话术已暂停")
        return self.status()

    def speak(self, text: str, mode: str = "echo", interrupt: bool = False):
        text = str(text).strip()
        if not text:
            raise ValueError("发送内容不能为空")
        with self._lock:
            sessionid = self._sessionid
            self._stat_manual_interventions += 1
        avatar = session_manager.get_session(sessionid)
        if avatar is None:
            raise ValueError("直播输出会话未连接")
        if interrupt:
            avatar.flush_talk()

        if mode == "chat":
            if self._thinking:
                raise RuntimeError("大模型正在生成上一条回答")
            self._thinking = True
            threading.Thread(
                target=self._chat_response,
                args=(text, avatar),
                daemon=True,
                name="live-studio-chat",
            ).start()
            self._add_history("question", text)
        else:
            sales_compliance.assert_safe(text, product_catalog.current())
            avatar.put_msg_txt(text, {"source": "manual"})
            with self._lock:
                self._last_spoken_text = text
                self._last_dispatch_at = time.time()
            self._add_history("manual", text)
        return self.status()

    def simulate_comment(self, text: str, speak: bool = False,
                         use_flavor: bool = True, allow_llm: bool = True) -> Dict:
        """模拟一条观众评论，走真实的意图/大模型链路，并返回完整判定过程。

        用途有三个：
        1. 开播前自检——不用真的开播、不用 OCR，就能验证"观众问什么、系统答什么"；
        2. 现场演示——点评一句就出结果，不依赖屏幕框选，演示可控；
        3. 排障——把"评论识别"与"回应播报"两段分开定位问题。

        判定逻辑复用 ``subtitle_ocr_manager.route_comment``，与字幕监控线程同一套口径。
        """
        text = str(text or "").strip()
        if not text:
            raise ValueError("评论内容不能为空")

        started = time.perf_counter()
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        product = product_catalog.current() or {}

        try:
            decision = subtitle_ocr_manager.route_comment(
                text, lines=lines or [text],
                use_flavor=use_flavor, allow_llm=allow_llm,
            )
        except Exception as exc:  # 大模型未配置、网络异常等都在这里兜住，不能让测试入口 500
            if "API Key" in str(exc):
                logger.warning("模拟评论：大模型未配置，本次只有意图判定结果：%s", exc)
            else:
                logger.exception("模拟评论：应答生成失败")
            decision = {
                "route": "llm", "intent": "", "intent_label": "", "keyword": "",
                "question": "", "response": "", "note": f"生成应答失败：{exc}",
            }

        route = decision.get("route") or ""
        response = decision.get("response") or ""
        note = decision.get("note") or ""

        # 这句话断网时能不能播出来：命中的话术是否已预热进本地语音缓存
        offline_ready = False
        if response:
            try:
                offline_ready = tts_cache_coverage([response])["cached"] > 0
            except Exception:
                logger.exception("模拟评论：离线缓存检查失败")

        spoken = False
        if speak and response:
            with self._lock:
                sessionid = self._sessionid
            avatar = session_manager.get_session(sessionid)
            if avatar is None:
                note = (note + "；" if note else "") + "直播输出窗口未连接，本次未播报"
            else:
                try:
                    avatar.put_msg_txt(response, {
                        "source": "comment-simulator",
                        "intent": decision.get("intent", ""),
                        "product_id": product.get("id", ""),
                    })
                    spoken = True
                    with self._lock:
                        self._last_spoken_text = response
                        self._last_dispatch_at = time.time()
                except Exception as exc:
                    logger.exception("模拟评论：播报失败")
                    note = (note + "；" if note else "") + f"播报失败：{exc}"

        label = {"intent": "T1 意图直答", "llm": "T2 大模型回答"}.get(route, "未判定")
        if response:
            self._add_history(
                "simulate",
                f"{label}｜{text} → {response[:60]}",
                "error" if (route == "llm" and not response) else "ok",
            )
        else:
            self._add_history("simulate", f"{label}｜{text} → 未生成应答", "error")

        return {
            "input": text,
            "lines": lines or [text],
            "route": route,
            "route_label": label,
            "intent": decision.get("intent", ""),
            "intent_label": decision.get("intent_label", ""),
            "keyword": decision.get("keyword", ""),
            "question": decision.get("question", ""),
            "response": response,
            "offline_ready": offline_ready,
            "spoken": spoken,
            "output_connected": session_manager.has_session(self._sessionid),
            "product": product.get("name", ""),
            "note": note,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        }

    def _chat_response(self, text, avatar):
        try:
            prompt = product_catalog.build_answer_prompt(text, source="operator")
            response = llm_response(prompt, None, raise_errors=True)
            response = sales_compliance.safe_response(response, product_catalog.current())
            avatar.put_msg_txt(response, {
                "source": "manual-product-chat",
                "product_id": product_catalog.status()["current_product_id"],
            })
            with self._lock:
                self._last_reply = response
                self._last_spoken_text = response
                self._last_dispatch_at = time.time()
            self._add_history("answer", response or "模型未返回内容", "ok" if response else "error")
        except Exception as exc:
            self._add_history("error", str(exc), "error")
            logger.exception("Live studio chat failed")
        finally:
            with self._lock:
                self._thinking = False
            self._notify_autopilot()

    def interrupt(self):
        avatar = session_manager.get_session(self._sessionid)
        if avatar is not None:
            avatar.flush_talk()
        self._add_history("operator", "人工打断播报")
        self._notify_autopilot()
        return self.status()

    def emergency_stop(self):
        with self._lock:
            self._autopilot = False
            self._dispatch_requested = False
        try:
            subtitle_ocr_manager.stop()
        except Exception:
            logger.exception("Failed to stop subtitle OCR during emergency stop")
        avatar = session_manager.get_session(self._sessionid)
        if avatar is not None:
            avatar.flush_talk()
        self._add_history("emergency", "已停止自动话术、字幕回应并清空当前播报")
        return self.status()

    def recording(self, action: str):
        avatar = session_manager.get_session(self._sessionid)
        if avatar is None:
            raise ValueError("直播输出会话未连接")
        if action == "start":
            avatar.start_recording()
            self._add_history("record", "开始本地录制")
        elif action == "stop":
            avatar.stop_recording()
            self._add_history("record", "停止本地录制")
        else:
            raise ValueError("action must be start or stop")
        return self.status()

    def upsert_product(self, params: Dict):
        requested_id = str(params.get("id", "")).strip() if isinstance(params, dict) else ""
        was_current = product_catalog.status()["current_product_id"] == requested_id
        product = product_catalog.upsert(params)
        if was_current:
            with self._lock:
                self._autopilot = False
                self._scripts = []
                self._script_index = 0
                self._reset_product_checks()
                self._persist()
        self._add_history("product", f"商品已保存：{product['name']}")
        return product_catalog.status()

    def delete_product(self, product_id: str):
        product = product_catalog.get(product_id)
        was_current = product_catalog.status()["current_product_id"] == product_id
        result = product_catalog.delete(product_id)
        if was_current:
            with self._lock:
                self._autopilot = False
                self._scripts = []
                self._script_index = 0
                self._reset_product_checks()
                self._persist()
        self._add_history("product", f"商品已删除：{product['name'] if product else product_id}")
        return result

    def set_current_product(self, product_id: str):
        previous_id = product_catalog.status()["current_product_id"]
        result = product_catalog.set_current(product_id)
        product = result["current_product"]
        if previous_id != product["id"]:
            self._log_product_switch(previous_id, product["id"])
            with self._lock:
                self._autopilot = False
                self._scripts = []
                self._script_index = 0
                self._reset_product_checks()
                self._persist()
                self._stat_product_switches += 1
        self._add_history("product", f"当前讲解商品：{product['name']}")
        return result

    def next_product(self):
        previous_id = product_catalog.status()["current_product_id"]
        result = product_catalog.next_ready()
        product = result["current_product"]
        if previous_id != product["id"]:
            self._log_product_switch(previous_id, product["id"])
            with self._lock:
                self._autopilot = False
                self._scripts = []
                self._script_index = 0
                self._reset_product_checks()
                self._persist()
                self._stat_product_switches += 1
        self._add_history("product", f"已切换下一件商品：{product['name']}")
        return result

    def _log_product_switch(self, from_id: str, to_id: str):
        self._product_switch_log.appendleft({
            "time": time.time(),
            "from": from_id,
            "to": to_id,
        })

    def generate_product_scripts(self, product_id: str = "", apply: bool = True):
        if product_id:
            self.set_current_product(product_id)
        scripts = product_catalog.generate_scripts(product_id)
        if apply:
            self.set_scripts(scripts, repeat=True)
        product = product_catalog.get(product_id) if product_id else product_catalog.current()
        self._add_history("product-scripts", f"已为 {product['name']} 生成 {len(scripts)} 条合规话术")
        return {
            "scripts": scripts,
            "applied": bool(apply),
            "sales": product_catalog.status(),
            "live": self.status(),
        }

    def update_workflow(self, params: Dict):
        if not isinstance(params, dict):
            raise ValueError("workflow payload must be an object")
        with self._lock:
            if params.get("reset_session"):
                self._reset_live_checks(include_permissions=False)
            for key in DEFAULT_WORKFLOW_CHECKS:
                if key in params:
                    self._workflow_checks[key] = bool(params[key])
            self._persist()
        self._add_history("workflow", "开播检查清单已更新")
        return self.status(include_internal_sales=True)

    def readiness(self):
        sales = product_catalog.status(include_internal=True)
        product = sales["current_product"]
        llm = get_llm_public_config()["active"]
        subtitle = subtitle_ocr_manager.status()
        try:
            tts_cache = tts_cache_coverage(
                intent_router.collect_phrases(product_catalog.list_ready())
            )
        except Exception:
            logger.exception("离线语音缓存统计失败")
            tts_cache = {"cached": 0, "total": 0, "missing": 0}
        with self._lock:
            output_connected = session_manager.has_session(self._sessionid)
            scripts_ready = bool(self._scripts)
            settings = dict(self._settings)
            manual = dict(self._workflow_checks)

        checks = []

        def add(key, label, passed, level="blocker", action=""):
            checks.append({
                "key": key,
                "label": label,
                "passed": bool(passed),
                "level": level,
                "action": action,
            })

        add("current_product", "已选择当前讲解商品", product is not None, action="在商品库选择一件已核对、已授权商品")
        add("scripts", "当前商品话术已生成并复核", scripts_ready, action="点击「生成并应用话术」，再人工检查")
        add("output", "数字人输出窗口已连接", output_connected, action="打开9:16输出窗口并点击「启动直播画面」")
        add("ai_label", "直播画面包含 AI 标识", "ai" in settings["ai_label"].lower(), action="在画面设置中保留「AI 数字人」标识")
        add("permissions_confirmed", "达人带货和开播权限已确认", manual["permissions_confirmed"], action="在平台后台确认直播、带货和类目权限")
        add("product_card_matched", "工作台商品与平台商品卡一致", manual["product_card_matched"], action="逐项核对商品ID、价格、优惠和卖点")
        add("platform_product_attached", "当前商品已挂到直播间", manual["platform_product_attached"], action="在官方直播工具挂载同一商品")
        add("audio_video_checked", "画面、声音和口型已检查", manual["audio_video_checked"], action="人工播报一段测试话术并观察音频电平")
        add("private_rehearsal_done", "已完成私密/本地彩排", manual["private_rehearsal_done"], action="至少彩排1分钟，检查字幕和换品")
        add("human_supervisor_ready", "真人值守和紧急接管已就绪", manual["human_supervisor_ready"], action="确保值守人员能立即打断、切品或停播")
        add("supervision_visible", "值守痕迹可观测：真人实时读评论并回应", manual["supervision_visible"], action="开播中由值守人员实时读评论、回答提问，避免整场只有固定循环播报")
        add("category_qualified", "百货类目资质已核对", manual["category_qualified"], action="逐一核对所售子类目（化妆品、食品、保健品、医疗器械、3C 电器、儿童用品等）是否需要资质或备案")
        add(
            "anti_replay",
            "防录播："
            + (
                "已关闭循环"
                if not settings["repeat_scripts"]
                else ("顺序已打乱" if settings.get("shuffle_scripts", True) else "固定顺序")
            )
            + f"，循环间隔 {settings['script_gap_seconds']:.0f} 秒",
            (
                not settings["repeat_scripts"]
                or (
                    settings.get("shuffle_scripts", True)
                    and settings["script_gap_seconds"] >= LOOP_GAP_FLOOR_SECONDS
                )
            ),
            level="warning",
            action="开启「打乱话术顺序」并把循环间隔设为 15 秒以上；最稳的做法是关闭循环，由值守人员逐轮手动继续",
        )
        add("llm", "大模型已配置，可进行智能答疑", llm["api_key_configured"], level="warning", action="在大模型管理中配置并测试 Key；不启用智能答疑可忽略")
        add("platform_product_id", "当前商品已填写平台商品 ID", bool(product and product["platform_product_id"]), level="warning", action="填写平台商品 ID，降低挂错商品风险")
        add("product_price", "当前商品已填写已核对价格", bool(product and product["price"]), level="warning", action="对照商品卡填写价格，变价后重新核对")
        add("selling_points", "当前商品至少有一条已核对卖点", bool(product and product["selling_points"]), level="warning", action="补充已由商家/商品卡确认的卖点")
        add("ocr", "字幕 OCR 已启动", subtitle["running"], level="optional", action="需要自动识别评论时再框选并启动")
        add(
            "tts_cache",
            f"离线语音缓存已覆盖高频话术（{tts_cache['cached']}/{tts_cache['total']}）",
            tts_cache["missing"] == 0,
            level="warning",
            action="联网时运行 E:\\AiLive\\tools\\prewarm_tts_cache.py 预热；新增商品或改话术后需重跑",
        )

        blockers = [item for item in checks if item["level"] == "blocker" and not item["passed"]]
        warnings = [item for item in checks if item["level"] == "warning" and not item["passed"]]
        passed_required = sum(1 for item in checks if item["level"] == "blocker" and item["passed"])
        required_total = sum(1 for item in checks if item["level"] == "blocker")

        # 自动更新状态机
        with self._lock:
            if not blockers and self._live_state in (LiveState.PREPARING, LiveState.IDLE):
                self._transition_state(LiveState.READY, "all checks passed")

        return {
            "ready": not blockers,
            "score": round(passed_required * 100 / required_total) if required_total else 100,
            "required_passed": passed_required,
            "required_total": required_total,
            "blockers": blockers,
            "warnings": warnings,
            "checks": checks,
            "manual_checks": manual,
        }

    def export_snapshot(self):
        with self._lock:
            settings = dict(self._settings)
            scripts = list(self._scripts)
            workflow = dict(self._workflow_checks)
        return {
            "format": "ailive-affiliate-backup",
            "version": 1,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "notice": "备份不包含任何大模型 API Key。佣金属于内部运营信息，请妥善保管。",
            "settings": settings,
            "scripts": scripts,
            "workflow_checks": workflow,
            "sales": product_catalog.status(include_internal=True),
            "llm": get_llm_public_config()["active"],
            "subtitle_config": subtitle_ocr_manager.status().get("config", {}),
        }

    # ── 反录播：播放顺序与节拍 ──────────────────────────────────────────

    def _rebuild_play_order(self, avoid_first: Optional[int] = None):
        """重建本轮播报顺序。

        开启「打乱话术顺序」时随机排列；关闭时保持作者顺序（与改动前行为一致）。
        avoid_first 用于让新一轮的首条不与上一轮末条相同，减少可识别的固定周期。
        """
        count = len(self._scripts)
        if count == 0:
            self._play_order = []
            self._script_index = 0
            return
        order = list(range(count))
        if self._settings.get("shuffle_scripts", True) and count > 1:
            random.shuffle(order)
            if avoid_first is not None and order[0] == avoid_first:
                order.append(order.pop(0))
        self._play_order = order

    def _next_gap_target(self) -> float:
        """下一次播报的等待时长：在设定间隔上叠加随机抖动，避免机械节拍。"""
        base = float(self._settings["script_gap_seconds"])
        return max(
            3.0, base * random.uniform(1.0 - GAP_JITTER_RATIO, 1.0 + GAP_JITTER_RATIO)
        )

    # ── 事件驱动 Autopilot ──────────────────────────────────────────────

    def _autopilot_loop(self):
        """事件驱动自动话术循环（替代轮询）。"""
        while not self._stop_event.is_set():
            with self._lock:
                # 等待通知或超时
                if not self._autopilot or self._thinking:
                    self._dispatch_condition.wait(timeout=5.0)
                    continue

                scripts = list(self._scripts)
                if len(self._play_order) != len(scripts):
                    # 排品模式等直接改写 _scripts 的路径，这里兜底重建顺序
                    self._rebuild_play_order()
                order = list(self._play_order)
                position = self._script_index
                gap = self._gap_target or self._settings["script_gap_seconds"]
                repeat = self._settings["repeat_scripts"]
                last_dispatch = self._last_dispatch_at
                sessionid = self._sessionid

            if not scripts:
                with self._lock:
                    self._dispatch_condition.wait(timeout=5.0)
                continue

            avatar = session_manager.get_session(sessionid)
            if avatar is None:
                with self._lock:
                    self._autopilot = False
                continue

            subtitle_status = subtitle_ocr_manager.status()
            if avatar.is_speaking() or subtitle_status.get("responding"):
                # 等 avatar 说完再试
                with self._lock:
                    self._dispatch_condition.wait(timeout=1.0)
                continue

            elapsed = time.time() - last_dispatch
            if elapsed < gap:
                with self._lock:
                    self._dispatch_condition.wait(timeout=gap - elapsed + 0.1)
                continue

            if position >= len(order):
                if repeat:
                    with self._lock:
                        # 新一轮重新打乱，首条避开上一轮末条
                        self._rebuild_play_order(avoid_first=order[-1] if order else None)
                        order = list(self._play_order)
                        position = 0
                        self._script_index = 0
                else:
                    with self._lock:
                        self._autopilot = False
                    self._add_history("autopilot", "话术队列已播放完毕")
                    continue

            text = scripts[order[position]]
            try:
                sales_compliance.assert_safe(text, product_catalog.current())
                avatar.put_msg_txt(text, {"source": "autopilot"})
            except Exception as exc:
                self._add_history("error", str(exc), "error")
                logger.exception("Live studio autopilot dispatch failed")
                time.sleep(1)
                continue

            with self._lock:
                self._script_index = position + 1
                self._last_dispatch_at = time.time()
                self._gap_target = self._next_gap_target()
                self._last_spoken_text = text
                self._stat_scripts_played += 1
            self._add_history("autopilot", text)

    # ── 状态查询 ────────────────────────────────────────────────────────

    def status(self, include_internal_sales: bool = False):
        with self._lock:
            sessionid = self._sessionid
            avatar = session_manager.get_session(sessionid)
            settings = dict(self._settings)
            scripts = list(self._scripts)
            history = list(self._history)
            result = {
                "sessionid": sessionid,
                "output_connected": avatar is not None,
                "output_registered_at": self._output_registered_at,
                "speaking": avatar.is_speaking() if avatar is not None else False,
                "recording": bool(getattr(avatar, "recording", False)) if avatar is not None else False,
                "autopilot": self._autopilot,
                "thinking": self._thinking,
                "settings": settings,
                "scripts": scripts,
                "script_index": self._script_index,
                "play_order": list(self._play_order),
                "gap_target_seconds": round(self._gap_target, 1),
                "last_spoken_text": self._last_spoken_text,
                "last_reply": self._last_reply,
                "history": history,
                # ── 状态机信息 ──
                "live_state": self._live_state.value,
                "live_started_at": self._live_started_at,
                "stats": {
                    "product_switches": self._stat_product_switches,
                    "scripts_played": self._stat_scripts_played,
                    "manual_interventions": self._stat_manual_interventions,
                },
                # ── 百货直播 ──
                "play_queue": self._play_queue.status() if self._use_play_queue else None,
                "atmosphere": {
                    "enabled": self._atmosphere_enabled,
                    "interval_seconds": self._atmosphere_interval,
                },
            }
        result["subtitle"] = subtitle_ocr_manager.status()
        result["llm"] = get_llm_public_config()["active"]
        result["sales"] = product_catalog.status(include_internal=include_internal_sales)
        result["workflow"] = self.readiness()
        return result

    # ── 排品队列（百货直播核心）──────────────────────────────────────

    def init_play_queue(self, mode: str = "loop"):
        """初始化排品队列：从商品库加载所有已核对商品，预生成话术。"""
        products = product_catalog.list_ready()
        if len(products) < 2:
            raise ValueError("至少需要 2 个已核对已授权的商品才能启用排品模式")

        self._play_queue.load_products(products, product_catalog.generate_scripts_by_type)
        self._play_queue.set_mode(PlayMode(mode) if mode in ("sequential", "loop", "weighted") else PlayMode.LOOP)
        self._use_play_queue = True
        product_catalog.reset_commission_tracker()
        self._add_history("playqueue", f"排品队列已初始化：{len(products)} 个商品，模式={mode}")
        return self._play_queue.status()

    def start_play_queue(self):
        """开始排品轮播。"""
        if not self._use_play_queue:
            raise ValueError("请先初始化排品队列")
        self._play_queue.start()
        self._autopilot = True
        self._last_dispatch_at = 0.0
        self._notify_autopilot()

        if self._play_queue_thread is None or not self._play_queue_thread.is_alive():
            self._play_queue_thread = threading.Thread(
                target=self._play_queue_timer,
                daemon=True,
                name="play-queue-timer",
            )
            self._play_queue_thread.start()
        self._add_history("playqueue", "排品轮播已启动")
        return self._play_queue.status()

    def _play_queue_timer(self):
        """排品计时线程：每个品播完后自动切换。"""
        while not self._stop_event.is_set() and self._use_play_queue:
            item = self._play_queue.current_item
            if item and item.started_at:
                elapsed = time.time() - item.started_at
                remaining = item.duration - elapsed
                if remaining <= 0:
                    self._advance_play_queue()
                elif remaining < 30 and not self._thinking:
                    self._dispatch_close_scripts(item)
            self._stop_event.wait(timeout=1.0)

    def _advance_play_queue(self):
        """切换到下一个商品（含转款话术）。"""
        old_item = self._play_queue.current_item
        if old_item:
            transit_scripts = old_item.scripts.get("transit", [])
            if transit_scripts:
                avatar = session_manager.get_session(self._sessionid)
                if avatar:
                    avatar.put_msg_txt(transit_scripts[0], {"source": "playqueue-transit"})
                    time.sleep(2)

        new_item = self._play_queue.next()
        if new_item:
            product_catalog.set_current(new_item.product_id)
            product_catalog.record_play(new_item.product_id)
            self._add_history("playqueue", f"切换商品：{new_item.product_name}")

            hook_scripts = new_item.scripts.get("hook", [])
            if hook_scripts:
                avatar = session_manager.get_session(self._sessionid)
                if avatar:
                    for s in hook_scripts[:2]:
                        avatar.put_msg_txt(s, {"source": "playqueue-hook"})
                        time.sleep(1.5)

            self._scripts = new_item.scripts.get("present", [])
            self._script_index = 0
            self._rebuild_play_order()
            self._last_dispatch_at = 0.0
            self._notify_autopilot()

    def _dispatch_close_scripts(self, item):
        """在商品即将结束时发送逼单话术。"""
        close_scripts = item.scripts.get("close", [])
        if close_scripts and not self._thinking:
            avatar = session_manager.get_session(self._sessionid)
            if avatar and not avatar.is_speaking():
                avatar.put_msg_txt(random.choice(close_scripts), {"source": "playqueue-close"})

    def get_play_queue_status(self):
        return self._play_queue.status()

    def play_queue_analytics(self):
        return self._play_queue.analytics()

    def next_product_in_queue(self):
        """手动切换到排品队列中的下一个商品。"""
        if not self._use_play_queue:
            raise ValueError("排品队列未启用")
        self._advance_play_queue()
        return self._play_queue.status()

    # ── 氛围管理 ──────────────────────────────────────────────────

    def start_atmosphere(self, interval_seconds: int = 180):
        """启动直播间氛围管理（自动欢迎/互动/感谢）。"""
        self._atmosphere_enabled = True
        self._atmosphere_interval = max(60, min(600, interval_seconds))
        self._atmosphere_last_at = time.time()

        if self._atmosphere_thread is None or not self._atmosphere_thread.is_alive():
            self._atmosphere_thread = threading.Thread(
                target=self._atmosphere_loop,
                daemon=True,
                name="atmosphere-manager",
            )
            self._atmosphere_thread.start()
        self._add_history("atmosphere", f"氛围管理已启动，间隔 {self._atmosphere_interval}s")
        return {"enabled": True, "interval_seconds": self._atmosphere_interval}

    def stop_atmosphere(self):
        self._atmosphere_enabled = False
        self._add_history("atmosphere", "氛围管理已停止")
        return {"enabled": False}

    def _atmosphere_loop(self):
        """氛围管理循环：定期发送欢迎/互动/感谢消息。"""
        while not self._stop_event.is_set() and self._atmosphere_enabled:
            avatar = session_manager.get_session(self._sessionid)
            if avatar and not avatar.is_speaking():
                self._atmosphere_last_at = time.time()
                msg_type = random.choice(["welcome", "interact", "welcome", "interact", "thank"])
                if msg_type == "welcome":
                    msg = random.choice(self._welcome_messages)
                elif msg_type == "thank":
                    msg = random.choice(self._thank_messages)
                else:
                    msg = random.choice(self._interact_messages)

                avatar.put_msg_txt(msg, {"source": "atmosphere"})
                self._add_history("atmosphere", msg)
            self._stop_event.wait(timeout=self._atmosphere_interval)

    # ── 数据分析 ──────────────────────────────────────────────────

    def export_analytics(self) -> dict:
        """导出完整直播数据报告。"""
        with self._lock:
            live_duration = 0.0
            if self._live_started_at:
                live_duration = time.time() - self._live_started_at

        return {
            "format": "ailive-analytics",
            "version": 1,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "summary": {
                "live_duration_seconds": round(live_duration, 1),
                "live_duration_minutes": round(live_duration / 60, 1),
                "product_switches": self._stat_product_switches,
                "scripts_played": self._stat_scripts_played,
                "manual_interventions": self._stat_manual_interventions,
                "live_state": self._live_state.value,
            },
            "commission": product_catalog.commission_report(),
            "play_queue": self._play_queue.analytics(),
            "switch_log": list(self._product_switch_log),
        }

    def shutdown(self):
        self._stop_event.set()
        self._atmosphere_enabled = False
        self._use_play_queue = False
        with self._lock:
            self._dispatch_condition.notify_all()
        if self._worker.is_alive():
            self._worker.join(timeout=2)
        if self._play_queue_thread and self._play_queue_thread.is_alive():
            self._play_queue_thread.join(timeout=2)
        if self._atmosphere_thread and self._atmosphere_thread.is_alive():
            self._atmosphere_thread.join(timeout=2)


live_studio_manager = LiveStudioManager()
