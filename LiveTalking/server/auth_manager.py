"""登录 / 角色 / 会员（套餐）核心。

本模块只依赖标准库，不引入 torch 等重依赖，方便单独做冒烟测试。

职责：
  1. 用户库（注册、登录、改密、禁用、角色与套餐）
  2. 会话令牌（Cookie + Bearer 双通道）
  3. 激活码 / 兑换码（管理员生成，用户兑换后升级为付费会员）
  4. 功能矩阵与路由分级规则（前后端共用同一份定义）

存储：``LiveTalking/data/auth/*.json``（data 目录已在 .gitignore 中）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ────────────────────────────── 常量与存储 ──────────────────────────────

SESSION_COOKIE = "ailive_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600
PASSWORD_ITERATIONS = 200_000
MIN_PASSWORD_LENGTH = 8
DEFAULT_ADMIN_USER = "admin"
DEFAULT_ADMIN_PASSWORD = "admin123456"

# 兑换码字母表：去掉 0/O/1/I 等易混字符
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_PREFIX = "AILIVE"
CODE_GROUPS = 3
CODE_GROUP_LEN = 4

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-\u4e00-\u9fa5]{3,24}$")

# 套餐等级：越大权限越高
LEVEL_FREE = 0
LEVEL_PRO = 1
LEVEL_ADMIN = 2

PLAN_LEVELS = {"free": LEVEL_FREE, "pro": LEVEL_PRO, "admin": LEVEL_ADMIN}

PLANS: Dict[str, Dict[str, Any]] = {
    "free": {
        "key": "free",
        "label": "免费版",
        "level": LEVEL_FREE,
        "price": "¥0",
        "summary": "注册即可使用，可查看工作台、商品库与试听模拟评论，不能连接数字人或开播。",
        "bullets": [
            "登录直播工作台，查看开播 SOP 与状态",
            "查看商品库与已生成话术",
            "模拟评论测试（只看判定链路，不让数字人播报）",
            "查看教程与帮助文档",
        ],
    },
    "pro": {
        "key": "pro",
        "label": "专业版",
        "level": LEVEL_PRO,
        "price": "¥99 / 30 天",
        "summary": "购买后解锁全部直播功能：连接数字人、一键开播、自动话术、录制与字幕回应。",
        "bullets": [
            "连接数字人并打开 9:16 直播输出窗口",
            "一键开播 / 下播、自动话术与氛围互动",
            "商品与佣金管理、话术生成与备份导出",
            "人工播报与打断、直播录制、字幕自动回应",
            "达人账号切换与管理",
        ],
    },
    "admin": {
        "key": "admin",
        "label": "管理员",
        "level": LEVEL_ADMIN,
        "price": "内部授权",
        "summary": "拥有全部功能，并可管理用户、生成激活码、配置大模型密钥。",
        "bullets": [
            "包含专业版全部功能",
            "用户管理：改角色 / 改套餐 / 重置密码 / 禁用",
            "生成与作废激活码",
            "大模型 API Key 配置与数字人素材管理",
        ],
    },
}

# 功能矩阵：前端的 data-feature 与服务端路由规则共用
FEATURES: Dict[str, Dict[str, str]] = {
    "console.view": {"label": "查看直播工作台", "plan": "free"},
    "catalog.view": {"label": "查看商品库", "plan": "free"},
    "simulate": {"label": "模拟评论测试", "plan": "free"},
    "help.view": {"label": "教程与帮助", "plan": "free"},
    "avatar.connect": {"label": "连接数字人 / 输出画面", "plan": "pro"},
    "live.start": {"label": "一键开播", "plan": "pro"},
    "live.settings": {"label": "开播设置与检查项", "plan": "pro"},
    "scripts.edit": {"label": "话术编辑与生成", "plan": "pro"},
    "autopilot": {"label": "自动话术与氛围互动", "plan": "pro"},
    "speak.manual": {"label": "人工播报", "plan": "pro"},
    "record": {"label": "直播录制", "plan": "pro"},
    "subtitle": {"label": "字幕自动回应", "plan": "pro"},
    "product.write": {"label": "商品与佣金管理", "plan": "pro"},
    "account.manage": {"label": "达人账号管理", "plan": "pro"},
    "export": {"label": "备份运营配置", "plan": "pro"},
    "llm.config": {"label": "大模型配置", "plan": "admin"},
    "avatar.manage": {"label": "数字人素材与训练", "plan": "admin"},
    "admin.users": {"label": "用户与激活码管理", "plan": "admin"},
    # 参赛 / 项目材料：入口与页面都只对管理员可见（普通用户连入口都不显示）
    "competition.view": {"label": "参赛项目与项目材料", "plan": "admin"},
}

# 安全操作永不被套餐拦截：免费用户也必须能停播、紧急停止
SAFETY_FEATURES = {"live.stop", "live.emergency"}

# 路由分级规则：(method, 路径前缀, 所需功能)。method 为 "*" 表示任意方法。
# 规则按顺序匹配，第一条命中即生效；功能为 None 表示“登录即可”。
ROUTE_RULES: List[Tuple[str, str, Optional[str]]] = [
    # ── 安全操作：任何已登录用户都放行 ──
    ("POST", "/api/workflow/stop", None),
    ("POST", "/api/live/emergency-stop", None),
    ("POST", "/api/live/interrupt", None),
    # ── 工作流 / 开播 ──
    ("POST", "/api/workflow/start", "live.start"),
    ("GET", "/api/workflow/preflight", "live.start"),
    ("POST", "/api/live/started", "live.start"),
    ("POST", "/api/live/ended", "live.start"),
    ("POST", "/api/live/workflow", "live.settings"),
    ("POST", "/api/live/settings", "live.settings"),
    ("POST", "/api/live/competition-demo", "competition.view"),
    ("POST", "/api/live/autopilot", "autopilot"),
    ("POST", "/api/live/atmosphere/", "autopilot"),
    ("POST", "/api/live/playqueue/", "autopilot"),
    ("POST", "/api/live/recording", "record"),
    ("POST", "/api/live/speak", "speak.manual"),
    ("POST", "/api/live/simulate", "simulate"),
    ("PUT", "/api/live/scripts", "scripts.edit"),
    ("GET", "/api/live/export", "export"),
    ("GET", "/api/live/analytics", "export"),
    ("GET", "/api/live/commission", "product.write"),
    # ── 商品库：读免费，写要会员 ──
    ("GET", "/api/live/products", "catalog.view"),
    ("DELETE", "/api/live/products", "product.write"),
    ("POST", "/api/live/products/scripts", "scripts.edit"),
    ("POST", "/api/live/products", "product.write"),
    # ── 其余 /api/live/* 为只读状态，登录即可 ──
    ("*", "/api/live/", "console.view"),
    # ── 数字人连接与输出 ──
    ("POST", "/api/live/register", "avatar.connect"),
    ("POST", "/api/live/unregister", "avatar.connect"),
    ("*", "/whep", "avatar.connect"),
    ("*", "/offer", "avatar.connect"),
    ("*", "/human", "avatar.connect"),
    ("*", "/humanaudio", "avatar.connect"),
    ("*", "/set_audiotype", "avatar.connect"),
    ("*", "/interrupt_talk", "avatar.connect"),
    ("*", "/is_speaking", "console.view"),
    ("*", "/sse", "console.view"),
    ("*", "/record", "record"),
    # ── 字幕自动回应 ──
    ("POST", "/api/subtitle/start", "subtitle"),
    ("POST", "/api/subtitle/stop", "subtitle"),
    ("GET", "/api/subtitle/screenshot", "subtitle"),
    ("GET", "/api/subtitle/status", "console.view"),
    # ── 达人账号：读免费，写要会员 ──
    ("GET", "/api/accounts", "catalog.view"),
    ("*", "/api/accounts", "account.manage"),
    # ── 大模型：只读配置公开，写入仅管理员 ──
    ("GET", "/api/llm/config", "help.view"),
    ("*", "/api/llm/", "llm.config"),
    # ── 数字人素材与后台仅管理员 ──
    ("*", "/api/avatar/", "avatar.manage"),
    ("*", "/api/admin/", "admin.users"),
    # ── 登录相关后台仅管理员 ──
    ("*", "/api/auth/admin/", "admin.users"),
    # ── 系统信息：登录即可 ──
    ("*", "/api/system/", "console.view"),
    ("*", "/api/workflow/", "console.view"),
]

# 页面级规则：路径 → 所需功能。未列出的 .html 只需登录。
PAGE_RULES: Dict[str, Optional[str]] = {
    "/live_output.html": "avatar.connect",
    "/subtitle.html": "subtitle",
    "/llm.html": "llm.config",
    "/avatar.html": "avatar.manage",
    "/avatar-en.html": "avatar.manage",
    "/admin.html": "admin.users",
    "/admin-en.html": "admin.users",
    "/admin_users.html": "admin.users",
    "/dashboard.html": "admin.users",
    "/console.html": "admin.users",
    "/virtualcam.html": "avatar.manage",
    "/rtmpapi.html": "avatar.manage",
    "/rtmpchat.html": "avatar.manage",
    "/rtcpushapi.html": "avatar.manage",
    "/rtcpushchat.html": "avatar.manage",
    "/webrtcapi.html": "avatar.manage",
    "/webrtcapi-asr.html": "avatar.manage",
    "/live_console.html": "console.view",
    "/index.html": "help.view",
    "/affiliate_tutorial.html": "help.view",
    "/competition.html": "competition.view",
    "/competition_ai_digital_human.html": "competition.view",
    "/accounts.html": "catalog.view",
    "/upgrade.html": None,
    "/login.html": None,
    "/forbidden.html": None,
}

# 无需登录即可访问的路径 / 静态资源后缀
PUBLIC_PATHS = {
    "/login.html",
    "/forbidden.html",
    "/health",
    "/api/auth/login",
    "/api/auth/register",
    "/api/auth/logout",
    "/api/auth/me",
    "/api/auth/plans",
}
# 本机音色接口（server/voice_routes.py）：音色库与朗读都跑在本机，
# 桌面版未登录时也要能用，否则「语音合成」页一进来就是 401。
# 注意：上传音色（POST /v1/audio/voices）仍然要求登录，避免匿名写文件。
PUBLIC_PREFIXES = (
    "/v1/audio/voices/",
    "/v1/audio/speech",
)
PUBLIC_SUFFIXES = (
    ".css", ".js", ".mjs", ".map", ".png", ".jpg", ".jpeg", ".gif", ".svg",
    ".ico", ".webp", ".woff", ".woff2", ".ttf", ".otf", ".json", ".yaml",
    ".yml", ".txt", ".webmanifest",
)


class AuthError(Exception):
    """业务错误：带 HTTP 状态码与错误码，路由层直接转成 JSON。"""

    def __init__(self, message: str, code: int = -1, status: int = 200):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


# ────────────────────────────── 时间与哈希工具 ──────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: Optional[datetime]) -> Optional[str]:
    if moment is None:
        return None
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def hash_password(password: str, salt: Optional[str] = None) -> str:
    """PBKDF2-SHA256，返回 ``pbkdf2_sha256$迭代次数$salt$hash``。"""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), PASSWORD_ITERATIONS
    ).hex()
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt}${digest}"


def verify_password(stored: str, password: str) -> bool:
    """常量时间比对，兼容旧格式。"""
    if not stored or not password:
        return False
    try:
        algorithm, iterations, salt, digest = stored.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iterations)
        ).hex()
        return hmac.compare_digest(candidate, digest)
    except (ValueError, TypeError):
        return False


def _random_code() -> str:
    groups = [
        "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_GROUP_LEN))
        for _ in range(CODE_GROUPS)
    ]
    return f"{CODE_PREFIX}-" + "-".join(groups)


def normalize_code(raw: Any) -> str:
    """把用户输入规整成标准兑换码：大写、去空格、补前缀。"""
    text = re.sub(r"[\s_]+", "", str(raw or "")).upper().replace("—", "-")
    text = text.strip("-")
    if not text:
        return ""
    if text.startswith(CODE_PREFIX):
        body = text[len(CODE_PREFIX):].lstrip("-")
    else:
        body = text
    total = CODE_GROUPS * CODE_GROUP_LEN
    if len(body) == total and "-" not in body:
        body = "-".join(
            body[i:i + CODE_GROUP_LEN] for i in range(0, total, CODE_GROUP_LEN)
        )
    return f"{CODE_PREFIX}-{body}"


def password_problem(password: str) -> Optional[str]:
    """返回密码不合规的原因，合规返回 None。"""
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        return f"密码至少需要 {MIN_PASSWORD_LENGTH} 位"
    if len(password) > 128:
        return "密码过长（最多 128 位）"
    if password.isdigit() or password.isalpha():
        return "密码需要同时包含字母和数字"
    return None


def username_problem(username: str) -> Optional[str]:
    if not username:
        return "请填写用户名"
    if not USERNAME_RE.match(username):
        return "用户名需为 3-24 位中英文、数字、下划线或短横线"
    return None


# ────────────────────────────── 主管理器 ──────────────────────────────


class AuthManager:
    """线程安全的用户 / 会话 / 兑换码存储。"""

    def __init__(self, data_dir: Optional[Path] = None):
        env_dir = os.environ.get("AILIVE_AUTH_DIR")
        self.data_dir = Path(data_dir or env_dir or (Path(__file__).resolve().parent.parent / "data" / "auth"))
        self.users_file = self.data_dir / "users.json"
        self.codes_file = self.data_dir / "codes.json"
        self.sessions_file = self.data_dir / "sessions.json"
        self.config_file = self.data_dir / "config.json"
        self._lock = threading.RLock()
        self._login_failures: Dict[str, List[float]] = {}
        self._register_hits: Dict[str, List[float]] = {}
        self.users: Dict[str, Dict[str, Any]] = {}
        self.codes: Dict[str, Dict[str, Any]] = {}
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.config: Dict[str, Any] = {}
        self._load()

    # ── 配置 ──────────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        override = os.environ.get("AILIVE_AUTH")
        if override is not None and str(override).strip().lower() in {"0", "off", "false", "no"}:
            return False
        return bool(self.config.get("enabled", True))

    @property
    def allow_register(self) -> bool:
        override = os.environ.get("AILIVE_ALLOW_REGISTER")
        if override is not None and str(override).strip().lower() in {"0", "off", "false", "no"}:
            return False
        return bool(self.config.get("allow_register", True))

    @property
    def single_session(self) -> bool:
        """同一账号是否只允许一处在线（默认开；管理员不受限，见 login）。

        目的：避免一个付费账号被多人/多台设备同时使用。
        环境变量 AILIVE_SINGLE_SESSION=off 可整体关掉（自检脚本等场景）。
        """
        override = os.environ.get("AILIVE_SINGLE_SESSION")
        if override is not None and str(override).strip().lower() in {"0", "off", "false", "no"}:
            return False
        return bool(self.config.get("single_session", True))

    def default_config(self) -> Dict[str, Any]:
        return {
            "enabled": True,
            "allow_register": True,
            "single_session": True,
            "pro_days_default": 30,
            "promo_days_default": 7,
            "contact": "微信 / 电话：请填写你的客服联系方式",
            "purchase_note": "扫码或联系客服付款后，把激活码发给用户，用户在“升级会员”页输入即可解锁。",
            "updated_at": _iso(_now()),
        }

    # ── 读写 ──────────────────────────────────────────────────────────

    def _read_json(self, path: Path, fallback: Any) -> Any:
        try:
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError):
            return fallback

    def _write_json(self, path: Path, payload: Any) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _load(self) -> None:
        with self._lock:
            self.users = self._read_json(self.users_file, {}) or {}
            self.codes = self._read_json(self.codes_file, {}) or {}
            self.sessions = self._read_json(self.sessions_file, {}) or {}
            self.config = {**self.default_config(), **(self._read_json(self.config_file, {}) or {})}
            self._purge_sessions()

    def _save_users(self) -> None:
        self._write_json(self.users_file, self.users)

    def _save_codes(self) -> None:
        self._write_json(self.codes_file, self.codes)

    def _save_sessions(self) -> None:
        self._write_json(self.sessions_file, self.sessions)

    def save_config(self, patch: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        with self._lock:
            if patch:
                allowed = {"enabled", "allow_register", "single_session", "pro_days_default",
                           "promo_days_default", "contact", "purchase_note"}
                for key, value in patch.items():
                    if key in allowed:
                        self.config[key] = value
            self.config["updated_at"] = _iso(_now())
            self._write_json(self.config_file, self.config)
            return dict(self.config)

    def reload(self) -> None:
        self._load()

    # ── 启动引导 ──────────────────────────────────────────────────────

    def ensure_bootstrap(self) -> Dict[str, Any]:
        """首次启动创建管理员账号，返回引导信息（含是否使用默认密码）。"""
        with self._lock:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            if not self.config_file.exists():
                self._write_json(self.config_file, self.config)
            if self.users:
                return {"created": False, "default_password": False}

            username = (os.environ.get("AILIVE_ADMIN_USER") or DEFAULT_ADMIN_USER).strip()
            password = (os.environ.get("AILIVE_ADMIN_PASSWORD") or "").strip()
            used_default = not password
            if used_default:
                password = DEFAULT_ADMIN_PASSWORD
            self.users[username.lower()] = {
                "username": username,
                "display_name": "超级管理员",
                "password": hash_password(password),
                "role": "admin",
                "plan": "admin",
                "plan_expires_at": None,
                "plan_started_at": _iso(_now()),
                "disabled": False,
                "note": "系统初始化创建",
                "created_at": _iso(_now()),
                "created_by": "system",
                "last_login_at": None,
                "login_count": 0,
                "must_change_password": used_default,
            }
            self._save_users()
            hint = self.data_dir / "初始管理员密码.txt"
            try:
                hint.write_text(
                    "AiLive 首次启动创建的管理员账号\n"
                    f"用户名：{username}\n"
                    f"密码：{password}\n"
                    "登录后请立即在“用户与激活码管理”中修改密码。\n",
                    encoding="utf-8",
                )
            except OSError:
                pass
            return {"created": True, "username": username,
                    "default_password": used_default, "hint_file": str(hint)}

    # ── 用户查询 ──────────────────────────────────────────────────────

    def _key(self, username: str) -> str:
        return str(username or "").strip().lower()

    def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self.users.get(self._key(username))

    def list_users(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = [self.public_user(user) for user in self.users.values()]
        rows.sort(key=lambda item: (item["role"] != "admin", item["username"]))
        return rows

    def public_user(self, user: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """对外安全的用户视图（绝不包含密码哈希）。"""
        if not user:
            return None
        level = self.plan_level(user)
        expires = _parse_iso(user.get("plan_expires_at"))
        days_left = None
        if expires is not None:
            days_left = max(0, int((expires - _now()).total_seconds() // 86400))
        return {
            "username": user.get("username", ""),
            "display_name": user.get("display_name") or user.get("username", ""),
            "role": user.get("role", "user"),
            "plan": user.get("plan", "free"),
            "plan_label": PLANS.get(user.get("plan", "free"), PLANS["free"])["label"],
            "plan_expires_at": user.get("plan_expires_at"),
            "plan_days_left": days_left,
            "plan_expired": bool(
                user.get("plan") in {"pro", "admin"}
                and user.get("plan") != "admin"
                and expires is not None
                and expires <= _now()
            ),
            "level": level,
            "is_admin": user.get("role") == "admin",
            "is_paid": level >= LEVEL_PRO,
            "disabled": bool(user.get("disabled")),
            "note": user.get("note", ""),
            "created_at": user.get("created_at"),
            "last_login_at": user.get("last_login_at"),
            "login_count": user.get("login_count", 0),
            "must_change_password": bool(user.get("must_change_password")),
        }

    # ── 权限判定 ──────────────────────────────────────────────────────

    def plan_level(self, user: Optional[Dict[str, Any]]) -> int:
        if not user or user.get("disabled"):
            return -1
        if user.get("role") == "admin":
            return LEVEL_ADMIN
        plan = user.get("plan", "free")
        if plan == "pro":
            expires = _parse_iso(user.get("plan_expires_at"))
            if expires is None or expires > _now():
                return LEVEL_PRO
            return LEVEL_FREE
        return PLAN_LEVELS.get(plan, LEVEL_FREE)

    def has_feature(self, user: Optional[Dict[str, Any]], feature: Optional[str]) -> bool:
        if feature is None or feature in SAFETY_FEATURES:
            return True
        if not user or user.get("disabled"):
            return False
        meta = FEATURES.get(feature)
        required = PLAN_LEVELS.get((meta or {}).get("plan", "free"), LEVEL_FREE)
        return self.plan_level(user) >= required

    def upgrade_hint(self, feature: Optional[str]) -> Dict[str, Any]:
        meta = FEATURES.get(feature or "", {"label": "该功能", "plan": "pro"})
        return {
            "feature": feature,
            "label": meta["label"],
            "required_plan": meta["plan"],
            "required_plan_label": PLANS.get(meta["plan"], PLANS["pro"])["label"],
        }

    # ── 注册 / 登录 ───────────────────────────────────────────────────

    def register(self, username: str, password: str, display_name: str = "",
                 remote: str = "", invite: str = "") -> Dict[str, Any]:
        username = str(username or "").strip()
        problem = username_problem(username)
        if problem:
            raise AuthError(problem, code=-2, status=400)
        pw_problem = password_problem(password)
        if pw_problem:
            raise AuthError(pw_problem, code=-2, status=400)

        with self._lock:
            if not self.allow_register and invite != "admin":
                raise AuthError("管理员已关闭自助注册，请联系管理员开号", code=403, status=403)
            if self._key(username) in self.users:
                raise AuthError("该用户名已被注册", code=-3, status=409)
            # 限流只针对自助注册：管理员开号是已登录的授权操作，
            # 同一个后台 IP 批量开号不应该被"注册过于频繁"卡住。
            if invite != "admin" and self._too_many(
                    self._register_hits, remote or "unknown", limit=5, window=3600):
                raise AuthError("注册过于频繁，请稍后再试", code=429, status=429)

            user = {
                "username": username,
                "display_name": (display_name or username).strip()[:32],
                "password": hash_password(password),
                "role": "user",
                "plan": "free",
                "plan_expires_at": None,
                "plan_started_at": _iso(_now()),
                "disabled": False,
                "note": "",
                "created_at": _iso(_now()),
                "created_by": "self-register",
                "last_login_at": None,
                "login_count": 0,
                "must_change_password": False,
            }
            self.users[self._key(username)] = user
            self._save_users()
            if invite != "admin":
                self._register_hits.setdefault(remote or "unknown", []).append(time.time())
            return self.public_user(user)

    def login(self, username: str, password: str, remote: str = "",
              user_agent: str = "") -> Tuple[Dict[str, Any], str, int]:
        """登录成功返回 (用户视图, 会话令牌, 被挤下线的旧会话数)。

        同一账号只允许一处在线：新登录会把该账号在其他设备/浏览器上的会话作废
        （"后来者顶下去"）。管理员不受此限 —— 管理员常有多个浏览器，
        且自检脚本要用管理员账号，否则每次跑自检都会把本人的登录挤掉。
        设置里的 single_session 关掉，或用 AILIVE_SINGLE_SESSION=off 全局关闭。
        """
        key = self._key(username)
        throttle_key = f"{key}|{remote or 'unknown'}"
        with self._lock:
            if self._too_many(self._login_failures, throttle_key, limit=8, window=300):
                raise AuthError("登录失败次数过多，请 5 分钟后再试", code=429, status=429)
            user = self.users.get(key)
            if not user or not verify_password(user.get("password", ""), password):
                self._login_failures.setdefault(throttle_key, []).append(time.time())
                raise AuthError("用户名或密码不正确", code=-4, status=401)
            if user.get("disabled"):
                raise AuthError("该账号已被管理员禁用", code=403, status=403)
            self._login_failures.pop(throttle_key, None)
            user["last_login_at"] = _iso(_now())
            user["login_count"] = int(user.get("login_count", 0)) + 1
            self._save_users()

            token = secrets.token_urlsafe(32)
            self.sessions[token] = {
                "username": user["username"],
                "created_at": _iso(_now()),
                "expires_at": _iso(_now() + timedelta(seconds=SESSION_TTL_SECONDS)),
                "remote": remote or "",
                "user_agent": (user_agent or "")[:180],
            }
            self._save_sessions()
            kicked = 0
            if self.single_session and user.get("role") != "admin":
                kicked = self._revoke_other_tokens(user["username"], keep=token)
            return self.public_user(user), token, kicked

    def logout(self, token: Optional[str]) -> bool:
        if not token:
            return False
        with self._lock:
            if self.sessions.pop(token, None) is None:
                return False
            self._save_sessions()
            return True

    def logout_user_everywhere(self, username: str) -> int:
        key = self._key(username)
        with self._lock:
            tokens = [t for t, s in self.sessions.items() if self._key(s.get("username", "")) == key]
            for token in tokens:
                self.sessions.pop(token, None)
            if tokens:
                self._save_sessions()
            return len(tokens)

    def session_user(self, token: Optional[str]) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        with self._lock:
            record = self.sessions.get(token)
            if not record:
                return None
            expires = _parse_iso(record.get("expires_at"))
            if expires is not None and expires <= _now():
                self.sessions.pop(token, None)
                self._save_sessions()
                return None
            user = self.users.get(self._key(record.get("username", "")))
            if not user or user.get("disabled"):
                return None
            return user

    def _purge_sessions(self) -> None:
        now = _now()
        stale = [
            token for token, record in self.sessions.items()
            if (_parse_iso(record.get("expires_at")) or now) <= now
        ]
        for token in stale:
            self.sessions.pop(token, None)
        if stale:
            self._save_sessions()

    def _too_many(self, bucket: Dict[str, List[float]], key: str,
                  limit: int, window: int) -> bool:
        hits = [stamp for stamp in bucket.get(key, []) if time.time() - stamp < window]
        bucket[key] = hits
        return len(hits) >= limit

    def change_password(self, username: str, old_password: str, new_password: str,
                        token: Optional[str] = None) -> bool:
        with self._lock:
            user = self.users.get(self._key(username))
            if not user:
                raise AuthError("用户不存在", code=-5, status=404)
            if not verify_password(user.get("password", ""), old_password):
                raise AuthError("原密码不正确", code=-4, status=401)
            problem = password_problem(new_password)
            if problem:
                raise AuthError(problem, code=-2, status=400)
            user["password"] = hash_password(new_password)
            user["must_change_password"] = False
            self._save_users()
            self._revoke_other_tokens(user["username"], keep=token)
            return True

    def reset_password(self, username: str, new_password: str, by: str = "admin") -> None:
        problem = password_problem(new_password)
        if problem:
            raise AuthError(problem, code=-2, status=400)
        with self._lock:
            user = self.users.get(self._key(username))
            if not user:
                raise AuthError("用户不存在", code=-5, status=404)
            user["password"] = hash_password(new_password)
            user["must_change_password"] = False
            self._save_users()
            self._revoke_other_tokens(user["username"])

    def _revoke_other_tokens(self, username: str, keep: Optional[str] = None) -> int:
        """作废该账号除 keep 之外的所有会话，返回被作废的数量。调用方须已持锁。"""
        key = self._key(username)
        tokens = [t for t, s in self.sessions.items()
                  if self._key(s.get("username", "")) == key and t != keep]
        for token in tokens:
            self.sessions.pop(token, None)
        if tokens:
            self._save_sessions()
        return len(tokens)

    # ── 管理员：用户维护 ──────────────────────────────────────────────

    def update_user(self, username: str, patch: Dict[str, Any], by: str = "admin") -> Dict[str, Any]:
        with self._lock:
            user = self.users.get(self._key(username))
            if not user:
                raise AuthError("用户不存在", code=-5, status=404)
            if "display_name" in patch:
                user["display_name"] = str(patch["display_name"] or "").strip()[:32] or user["username"]
            if "note" in patch:
                user["note"] = str(patch["note"] or "")[:200]
            if "disabled" in patch:
                if user.get("role") == "admin" and bool(patch["disabled"]) and self._admin_count() <= 1:
                    raise AuthError("至少需要保留一个可用管理员", code=-6, status=400)
                user["disabled"] = bool(patch["disabled"])
                if user["disabled"]:
                    self._revoke_other_tokens(user["username"])
            if "role" in patch:
                role = str(patch["role"])
                if role not in {"user", "admin"}:
                    raise AuthError("角色只能是 user 或 admin", code=-2, status=400)
                if role == "user" and user.get("role") == "admin" and self._admin_count() <= 1:
                    raise AuthError("至少需要保留一个管理员", code=-6, status=400)
                user["role"] = role
                if role == "admin":
                    user["plan"] = "admin"
                    user["plan_expires_at"] = None
                elif user.get("plan") == "admin":
                    user["plan"] = "free"
                    user["plan_expires_at"] = None
            if "plan" in patch:
                plan = str(patch["plan"])
                if plan not in PLANS:
                    raise AuthError("套餐只能是 free / pro / admin", code=-2, status=400)
                user["plan"] = plan
                user["plan_started_at"] = _iso(_now())
                days = patch.get("days")
                if plan == "free":
                    user["plan_expires_at"] = None
                elif plan == "admin":
                    user["plan_expires_at"] = None
                    user["role"] = "admin"
                else:
                    expires = self._extend_expiry(user.get("plan_expires_at"), int(days or self.config.get("pro_days_default", 30)))
                    user["plan_expires_at"] = expires
            if "plan_expires_at" in patch and patch["plan_expires_at"] is not None:
                moment = _parse_iso(patch["plan_expires_at"])
                if moment is None:
                    raise AuthError("到期时间格式不正确，需为 ISO8601 时间", code=-2, status=400)
                user["plan_expires_at"] = _iso(moment)
            if "password" in patch and patch["password"]:
                self.reset_password(user["username"], str(patch["password"]), by=by)
            self._save_users()
            return self.public_user(user)

    def delete_user(self, username: str) -> None:
        with self._lock:
            user = self.users.get(self._key(username))
            if not user:
                raise AuthError("用户不存在", code=-5, status=404)
            if user.get("role") == "admin" and self._admin_count() <= 1:
                raise AuthError("至少需要保留一个管理员", code=-6, status=400)
            self.users.pop(self._key(username), None)
            self._save_users()
            self._revoke_other_tokens(username)

    def _admin_count(self) -> int:
        return sum(1 for u in self.users.values()
                   if u.get("role") == "admin" and not u.get("disabled"))

    def _extend_expiry(self, current: Any, days: int) -> str:
        base = _parse_iso(current)
        now = _now()
        if base is None or base <= now:
            base = now
        return _iso(base + timedelta(days=max(1, int(days))))

    # ── 激活码 ────────────────────────────────────────────────────────

    def create_codes(self, plan: str = "pro", days: int = 30, count: int = 1,
                     max_uses: int = 1, valid_days: int = 0, note: str = "",
                     created_by: str = "admin") -> List[Dict[str, Any]]:
        if plan not in PLANS:
            raise AuthError("套餐只能是 free / pro / admin", code=-2, status=400)
        if plan == "free":
            raise AuthError("免费版无需激活码", code=-2, status=400)
        count = max(1, min(200, int(count)))
        days = max(1, min(3650, int(days)))
        max_uses = max(1, min(1000, int(max_uses)))
        with self._lock:
            created = []
            for _ in range(count):
                code = _random_code()
                while code in self.codes:
                    code = _random_code()
                record = {
                    "code": code,
                    "plan": plan,
                    "days": days,
                    "max_uses": max_uses,
                    "used_count": 0,
                    "used_by": [],
                    "note": str(note or "")[:120],
                    "created_at": _iso(_now()),
                    "created_by": created_by,
                    "expires_at": _iso(_now() + timedelta(days=int(valid_days))) if valid_days else None,
                    "revoked": False,
                }
                self.codes[code] = record
                created.append(dict(record))
            self._save_codes()
            return created

    def code_status(self, record: Dict[str, Any]) -> str:
        if record.get("revoked"):
            return "revoked"
        expires = _parse_iso(record.get("expires_at"))
        if expires is not None and expires <= _now():
            return "expired"
        if int(record.get("used_count", 0)) >= int(record.get("max_uses", 1)):
            return "used"
        return "unused"

    def list_codes(self, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock:
            rows = []
            for record in self.codes.values():
                item = dict(record)
                item["status"] = self.code_status(record)
                rows.append(item)
        rows.sort(key=lambda item: item.get("created_at") or "", reverse=True)
        return rows[:limit]

    def revoke_code(self, code: str) -> Dict[str, Any]:
        key = normalize_code(code)
        with self._lock:
            record = self.codes.get(key)
            if not record:
                raise AuthError("兑换码不存在", code=-5, status=404)
            record["revoked"] = True
            self._save_codes()
            item = dict(record)
            item["status"] = self.code_status(record)
            return item

    def redeem(self, username: str, raw_code: str) -> Dict[str, Any]:
        code = normalize_code(raw_code)
        if not code:
            raise AuthError("请输入激活码", code=-2, status=400)
        with self._lock:
            user = self.users.get(self._key(username))
            if not user:
                raise AuthError("用户不存在", code=-5, status=404)
            record = self.codes.get(code)
            if not record:
                raise AuthError("激活码不存在，请核对后重试", code=-7, status=404)
            status = self.code_status(record)
            if status == "revoked":
                raise AuthError("该激活码已被作废", code=-7, status=400)
            if status == "expired":
                raise AuthError("该激活码已过期", code=-7, status=400)
            if status == "used":
                raise AuthError("该激活码已被使用", code=-7, status=400)
            if self._key(user["username"]) in {
                self._key(name) for name in record.get("used_by", [])
            }:
                raise AuthError("你已经使用过该激活码", code=-7, status=400)

            plan = record.get("plan", "pro")
            days = int(record.get("days", 30))
            if plan == "admin":
                user["role"] = "admin"
                user["plan"] = "admin"
                user["plan_expires_at"] = None
            else:
                user["plan"] = "pro"
                user["plan_started_at"] = _iso(_now())
                user["plan_expires_at"] = self._extend_expiry(user.get("plan_expires_at"), days)

            record["used_count"] = int(record.get("used_count", 0)) + 1
            record.setdefault("used_by", []).append(user["username"])
            record.setdefault("used_at", []).append(_iso(_now()))
            self._save_codes()
            self._save_users()
            return {
                "user": self.public_user(user),
                "code": record["code"],
                "plan": plan,
                "days": days,
                "plan_expires_at": user.get("plan_expires_at"),
            }

    # ── 概览 ──────────────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            users = list(self.users.values())
            codes = list(self.codes.values())
            sessions = len(self.sessions)
        return {
            "total_users": len(users),
            "admins": sum(1 for u in users if u.get("role") == "admin"),
            "paid_users": sum(1 for u in users if self.plan_level(u) == LEVEL_PRO),
            "free_users": sum(1 for u in users if self.plan_level(u) <= LEVEL_FREE and u.get("role") != "admin"),
            "disabled_users": sum(1 for u in users if u.get("disabled")),
            "total_codes": len(codes),
            "unused_codes": sum(1 for c in codes if self.code_status(c) == "unused"),
            "active_sessions": sessions,
            "enabled": self.enabled,
            "allow_register": self.allow_register,
            "single_session": self.single_session,
        }

    def entitlements(self, user: Optional[Dict[str, Any]]) -> Dict[str, bool]:
        return {key: self.has_feature(user, key) for key in FEATURES}


# ────────────────────────────── 单例与请求级辅助 ──────────────────────────────

_manager: Optional[AuthManager] = None
_manager_lock = threading.Lock()


def get_auth_manager() -> AuthManager:
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = AuthManager()
    return _manager


def set_auth_manager(manager: Optional[AuthManager]) -> None:
    """测试用：替换单例。"""
    global _manager
    with _manager_lock:
        _manager = manager


def auth_enabled() -> bool:
    return get_auth_manager().enabled


def request_token(request: Any) -> str:
    """从 Cookie 或 Authorization 头取会话令牌。"""
    try:
        token = request.cookies.get(SESSION_COOKIE, "")
    except AttributeError:
        token = ""
    if token:
        return token
    try:
        header = request.headers.get("Authorization", "")
    except AttributeError:
        header = ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""


def request_user(request: Any) -> Optional[Dict[str, Any]]:
    """获取当前请求的登录用户：优先取中间件写入的结果。"""
    cached = None
    try:
        cached = request.get("auth_user")
    except (AttributeError, TypeError):
        cached = None
    if cached is not None:
        return cached or None
    if not auth_enabled():
        return None
    user = get_auth_manager().session_user(request_token(request))
    try:
        request["auth_user"] = user
    except (AttributeError, TypeError, KeyError):
        pass
    return user


def request_can(request: Any, feature: Optional[str]) -> bool:
    """当前请求是否具备某功能。登录功能关闭时一律放行（兼容旧行为）。"""
    if not auth_enabled():
        return True
    if feature is None or feature in SAFETY_FEATURES:
        return True
    return get_auth_manager().has_feature(request_user(request), feature)


def match_route_rule(method: str, path: str) -> Tuple[bool, Optional[str]]:
    """返回 (是否命中规则, 所需功能)。"""
    for rule_method, prefix, feature in ROUTE_RULES:
        if rule_method != "*" and rule_method != method:
            continue
        if path == prefix or path.startswith(prefix):
            return True, feature
    return False, None


def match_page_rule(path: str) -> Tuple[bool, Optional[str]]:
    if path in PAGE_RULES:
        return True, PAGE_RULES[path]
    return False, None


def is_public_path(path: str, method: str = "GET") -> bool:
    if path in PUBLIC_PATHS:
        return True
    if method == "GET" and path.lower().endswith(PUBLIC_SUFFIXES):
        return True
    # 本机音色：列表 / 原声 / 朗读都放行；上传音色（POST 列表接口）要登录
    if path == "/v1/audio/voices":
        return method in ("GET", "HEAD", "OPTIONS")
    for prefix in PUBLIC_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


# ────────────────────────────── 命令行工具 ──────────────────────────────

def _cli(argv: List[str]) -> int:
    manager = get_auth_manager()
    if not argv or argv[0] in {"help", "-h", "--help"}:
        print(
            "用法：\n"
            "  python -m server.auth_manager list                  # 列出用户\n"
            "  python -m server.auth_manager set-password <用户名> <新密码>\n"
            "  python -m server.auth_manager make-admin <用户名>\n"
            "  python -m server.auth_manager new-code [天数] [数量]  # 生成兑换码\n"
        )
        return 0
    action = argv[0]
    try:
        if action == "list":
            for row in manager.list_users():
                print(f"{row['username']:<16} {row['role']:<6} {row['plan']:<6} "
                      f"{row['plan_expires_at'] or '-':<26} disabled={row['disabled']}")
            return 0
        if action == "set-password" and len(argv) >= 3:
            manager.reset_password(argv[1], argv[2])
            print(f"已重置 {argv[1]} 的密码")
            return 0
        if action == "make-admin" and len(argv) >= 2:
            manager.update_user(argv[1], {"role": "admin"})
            print(f"{argv[1]} 已设为管理员")
            return 0
        if action == "new-code":
            days = int(argv[1]) if len(argv) > 1 else 30
            count = int(argv[2]) if len(argv) > 2 else 1
            for item in manager.create_codes(days=days, count=count, created_by="cli"):
                print(item["code"], f"({item['days']} 天)")
            return 0
    except AuthError as exc:
        print(f"失败：{exc.message}", file=sys.stderr)
        return 1
    print("参数不完整，运行 `python -m server.auth_manager help` 查看用法", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))