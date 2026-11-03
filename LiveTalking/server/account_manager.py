###############################################################################
#  账号管理器 (Account Manager)
#  多账号管理：每个账号独立商品库、话术、佣金、设置、数据
###############################################################################

import json
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

from utils.logger import logger


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ACCOUNTS_ROOT = PROJECT_ROOT / "data" / "accounts"
REGISTRY_FILE = ACCOUNTS_ROOT / "registry.json"
ACCOUNT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")


def _validate_account_id(value: str) -> str:
    account_id = str(value or "").strip()
    if not ACCOUNT_ID_PATTERN.fullmatch(account_id):
        raise ValueError(
            "账号 ID 只能包含字母、数字、下划线和短横线，且长度为 1-80"
        )
    return account_id


class AccountStatus(Enum):
    ACTIVE = "active"         # 正常使用中
    PAUSED = "paused"         # 暂停
    BANNED = "banned"         # 被封禁
    ARCHIVED = "archived"     # 已归档


class AccountPlatform(Enum):
    DOUYIN = "douyin"
    WECHAT_CHANNELS = "wechat_channels"
    KUAISHOU = "kuaishou"
    OTHER = "other"


PLATFORM_LABELS = {
    "douyin": "抖音",
    "wechat_channels": "微信视频号",
    "kuaishou": "快手",
    "other": "其他平台",
}


@dataclass
class Account:
    """账号数据模型"""
    id: str
    name: str
    platform: str = "douyin"
    status: str = "active"
    # ── 平台信息 ──
    platform_account_id: str = ""      # 平台账号 ID
    platform_account_name: str = ""    # 平台账号名称
    # ── 数字人配置 ──
    avatar_id: str = ""                # 数字人形象
    model: str = "wav2lip"             # 模型类型
    tts: str = "edgetts"               # TTS 引擎
    tts_voice: str = ""                # TTS 音色
    # ── 直播设置 ──
    live_title: str = "AI 数字人直播间"
    ai_label: str = "AI 数字人 · 内容由人工值守"
    script_gap_seconds: float = 30.0
    # ── 元数据 ──
    tags: List[str] = field(default_factory=list)
    notes: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    last_active_at: float = 0.0
    # ── 统计 ──
    total_live_sessions: int = 0
    total_live_minutes: float = 0.0
    total_commission: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "platform": self.platform,
            "platform_label": PLATFORM_LABELS.get(self.platform, self.platform),
            "status": self.status,
            "platform_account_id": self.platform_account_id,
            "platform_account_name": self.platform_account_name,
            "avatar_id": self.avatar_id,
            "model": self.model,
            "tts": self.tts,
            "tts_voice": self.tts_voice,
            "live_title": self.live_title,
            "ai_label": self.ai_label,
            "script_gap_seconds": self.script_gap_seconds,
            "tags": self.tags,
            "notes": self.notes,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_active_at": self.last_active_at,
            "total_live_sessions": self.total_live_sessions,
            "total_live_minutes": round(self.total_live_minutes, 1),
            "total_commission": round(self.total_commission, 2),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Account":
        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            platform=data.get("platform", "douyin"),
            status=data.get("status", "active"),
            platform_account_id=data.get("platform_account_id", ""),
            platform_account_name=data.get("platform_account_name", ""),
            avatar_id=data.get("avatar_id", ""),
            model=data.get("model", "wav2lip"),
            tts=data.get("tts", "edgetts"),
            tts_voice=data.get("tts_voice", ""),
            live_title=data.get("live_title", "AI 数字人直播间"),
            ai_label=data.get("ai_label", "AI 数字人 · 内容由人工值守"),
            script_gap_seconds=data.get("script_gap_seconds", 30.0),
            tags=data.get("tags", []),
            notes=data.get("notes", ""),
            created_at=data.get("created_at", 0.0),
            updated_at=data.get("updated_at", 0.0),
            last_active_at=data.get("last_active_at", 0.0),
            total_live_sessions=data.get("total_live_sessions", 0),
            total_live_minutes=data.get("total_live_minutes", 0.0),
            total_commission=data.get("total_commission", 0.0),
        )


class AccountManager:
    """多账号管理器。

    功能：
    - 账号 CRUD（创建/读取/更新/删除）
    - 账号数据隔离（每个账号独立存储目录）
    - 账号切换（激活账号 → 自动加载对应商品库/话术/设置）
    - 多平台支持（抖音/视频号/快手/其他）
    - 账号状态管理（活跃/暂停/封禁/归档）
    - 账号数据导出/导入
    """

    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized"):
            return
        self._initialized = True
        self._lock = threading.RLock()
        self._accounts: Dict[str, Account] = {}
        self._active_account_id: str = ""
        self._load()
        self._ensure_bootstrap_account()

    def _ensure_bootstrap_account(self):
        """首次运行时创建默认账号，并迁移旧版单账号数据。"""
        with self._lock:
            changed = False
            if not self._accounts:
                now = time.time()
                account = Account(
                    id="default",
                    name="默认直播账号",
                    platform="douyin",
                    avatar_id=os.getenv("DEFAULT_AVATAR_ID", "wav2lip256_avatar1"),
                    created_at=now,
                    updated_at=now,
                )
                self._accounts[account.id] = account
                self._active_account_id = account.id
                self._ensure_account_dir(account.id)
                changed = True
                logger.info("Created bootstrap account: %s", account.id)
            elif self._active_account_id not in self._accounts:
                active = next(
                    (item for item in self._accounts.values() if item.status == "active"),
                    next(iter(self._accounts.values())),
                )
                self._active_account_id = active.id
                changed = True

            active_id = self._active_account_id
            if active_id:
                account_dir = self._ensure_account_dir(active_id)
                # Preserve data created by the legacy single-account build.
                for filename in ("live_studio.json", "sales_catalog.json"):
                    legacy_file = PROJECT_ROOT / "data" / filename
                    account_file = account_dir / filename
                    if legacy_file.exists() and not account_file.exists():
                        shutil.copy2(legacy_file, account_file)
                        logger.info("Migrated legacy data file to account: %s", filename)
                        changed = True

            if changed:
                self._save()

    # ── 持久化 ──────────────────────────────────────────────────────────

    def _load(self):
        ACCOUNTS_ROOT.mkdir(parents=True, exist_ok=True)
        if not REGISTRY_FILE.exists():
            logger.info("Account registry not found, creating empty registry")
            return
        try:
            data = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
            accounts_list = data.get("accounts", [])
            self._accounts = {}
            for item in accounts_list:
                account = Account.from_dict(item)
                account_id = str(account.id or "").strip()
                if account_id and ACCOUNT_ID_PATTERN.fullmatch(account_id):
                    account.id = account_id
                    self._accounts[account_id] = account
                elif account.id:
                    logger.warning("Ignoring invalid account id from registry: %s", account.id)
            self._active_account_id = data.get("active_account_id", "")
            if self._active_account_id not in self._accounts:
                self._active_account_id = ""
            logger.info("Loaded %d accounts, active=%s", len(self._accounts), self._active_account_id)
        except Exception:
            logger.exception("Failed to load account registry")

    def _save(self):
        ACCOUNTS_ROOT.mkdir(parents=True, exist_ok=True)
        temp = REGISTRY_FILE.with_suffix(".tmp")
        temp.write_text(
            json.dumps(
                {
                    "active_account_id": self._active_account_id,
                    "accounts": [a.to_dict() for a in self._accounts.values()],
                    "updated_at": time.time(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(temp, REGISTRY_FILE)

    # ── 账号目录管理 ────────────────────────────────────────────────────

    def get_account_dir(self, account_id: str) -> Path:
        """获取账号数据目录。"""
        return ACCOUNTS_ROOT / _validate_account_id(account_id)

    def _ensure_account_dir(self, account_id: str):
        """确保账号目录存在。"""
        acc_dir = self.get_account_dir(account_id)
        acc_dir.mkdir(parents=True, exist_ok=True)
        # 创建子目录
        (acc_dir / "backups").mkdir(exist_ok=True)
        return acc_dir

    def get_account_data_path(self, account_id: str, filename: str) -> Path:
        """获取账号数据文件路径。"""
        return self.get_account_dir(account_id) / filename

    # ── CRUD ────────────────────────────────────────────────────────────

    def create(self, params: dict) -> Account:
        """创建新账号。"""
        with self._lock:
            name = str(params.get("name", "")).strip()
            if not name:
                raise ValueError("账号名称不能为空")
            if len(self._accounts) >= 50:
                raise ValueError("最多支持 50 个账号")

            account_id = _validate_account_id(
                str(params.get("id") or uuid.uuid4().hex[:12])
            )
            if account_id in self._accounts:
                raise ValueError(f"账号 ID {account_id} 已存在")

            platform = str(params.get("platform", "douyin"))
            if platform not in PLATFORM_LABELS:
                platform = "other"

            now = time.time()
            account = Account(
                id=account_id,
                name=name,
                platform=platform,
                status="active",
                platform_account_id=str(params.get("platform_account_id", "")).strip(),
                platform_account_name=str(params.get("platform_account_name", "")).strip(),
                avatar_id=str(params.get("avatar_id", "")).strip(),
                model=str(params.get("model", "wav2lip")).strip(),
                tts=str(params.get("tts", "edgetts")).strip(),
                tts_voice=str(params.get("tts_voice", "")).strip(),
                live_title=str(params.get("live_title", "AI 数字人直播间")).strip()[:80],
                ai_label=str(params.get("ai_label", "AI 数字人 · 内容由人工值守")).strip()[:80],
                script_gap_seconds=min(300.0, max(3.0, float(params.get("script_gap_seconds", 30.0)))),
                tags=list(params.get("tags", [])),
                notes=str(params.get("notes", "")).strip()[:500],
                created_at=now,
                updated_at=now,
            )

            self._accounts[account_id] = account
            self._ensure_account_dir(account_id)
            self._save()
            logger.info("Account created: %s (%s)", name, account_id)
            return account

    def update(self, account_id: str, params: dict) -> Account:
        """更新账号信息。"""
        with self._lock:
            account = self._accounts.get(account_id)
            if not account:
                raise ValueError(f"账号 {account_id} 不存在")

            if "name" in params:
                account.name = str(params["name"]).strip() or account.name
            if "platform" in params:
                p = str(params["platform"])
                account.platform = p if p in PLATFORM_LABELS else account.platform
            if "status" in params:
                s = str(params["status"])
                if s in ("active", "paused", "banned", "archived"):
                    account.status = s
            if "platform_account_id" in params:
                account.platform_account_id = str(params["platform_account_id"]).strip()
            if "platform_account_name" in params:
                account.platform_account_name = str(params["platform_account_name"]).strip()
            if "avatar_id" in params:
                account.avatar_id = str(params["avatar_id"]).strip()
            if "model" in params:
                account.model = str(params["model"]).strip()
            if "tts" in params:
                account.tts = str(params["tts"]).strip()
            if "tts_voice" in params:
                account.tts_voice = str(params["tts_voice"]).strip()
            if "live_title" in params:
                account.live_title = str(params["live_title"]).strip()[:80]
            if "ai_label" in params:
                account.ai_label = str(params["ai_label"]).strip()[:80]
            if "script_gap_seconds" in params:
                account.script_gap_seconds = min(300.0, max(3.0, float(params["script_gap_seconds"])))
            if "tags" in params:
                account.tags = list(params["tags"])
            if "notes" in params:
                account.notes = str(params["notes"]).strip()[:500]

            account.updated_at = time.time()
            self._save()
            logger.info("Account updated: %s", account_id)
            return account

    def delete(self, account_id: str) -> dict:
        """删除账号（含数据目录）。"""
        with self._lock:
            if account_id not in self._accounts:
                raise ValueError(f"账号 {account_id} 不存在")
            if len(self._accounts) <= 1:
                raise ValueError("至少保留一个账号")

            account = self._accounts.pop(account_id)
            if self._active_account_id == account_id:
                self._active_account_id = ""

            self._save()

            # 删除数据目录
            acc_dir = self.get_account_dir(account_id)
            if acc_dir.exists():
                shutil.rmtree(acc_dir, ignore_errors=True)

            logger.info("Account deleted: %s (%s)", account.name, account_id)
            return {"deleted": account_id, "name": account.name}

    # ── 查询 ────────────────────────────────────────────────────────────

    def get(self, account_id: str) -> Optional[Account]:
        with self._lock:
            return self._accounts.get(account_id)

    def list_all(self) -> List[Account]:
        with self._lock:
            return list(self._accounts.values())

    def list_active(self) -> List[Account]:
        with self._lock:
            return [a for a in self._accounts.values() if a.status == "active"]

    @property
    def active_account(self) -> Optional[Account]:
        with self._lock:
            return self._accounts.get(self._active_account_id)

    @property
    def active_account_id(self) -> str:
        return self._active_account_id

    # ── 账号切换 ────────────────────────────────────────────────────────

    def switch_to(self, account_id: str) -> Account:
        """切换到指定账号。

        切换时自动：
        1. 更新 last_active_at
        2. 通知下游模块重载数据
        """
        with self._lock:
            if account_id not in self._accounts:
                raise ValueError(f"账号 {account_id} 不存在")

            account = self._accounts[account_id]
            if account.status == "banned":
                raise ValueError(f"账号 {account.name} 已被封禁，无法切换")

            old_id = self._active_account_id
            if old_id != account_id:
                try:
                    from server.live_studio import live_studio_manager
                    live_status = live_studio_manager.status()
                    if live_status.get("output_connected") or live_status.get("live_state") == "live":
                        raise ValueError("直播输出已连接或正在直播，请先停播后再切换账号")
                except ImportError:
                    pass
            self._active_account_id = account_id
            account.last_active_at = time.time()
            account.updated_at = time.time()
            self._save()

            logger.info("Account switched: %s → %s (%s)",
                        old_id or "none", account_id, account.name)

            # 触发数据重载
            self._reload_account_data(account_id)
            return account

    def _reload_account_data(self, account_id: str):
        """通知下游模块重载账号数据。"""
        try:
            from server.sales_catalog import product_catalog
            product_catalog.reload_for_account(account_id)
        except Exception as e:
            logger.warning("Failed to reload product catalog for account %s: %s", account_id, e)

        try:
            from server.live_studio import live_studio_manager
            live_studio_manager.reload_for_account(account_id)
        except Exception as e:
            logger.warning("Failed to reload live studio for account %s: %s", account_id, e)

    # ── 统计更新 ────────────────────────────────────────────────────────

    def record_live_session(self, account_id: str, duration_minutes: float, commission: float = 0.0):
        """记录一次直播会话。"""
        with self._lock:
            account = self._accounts.get(account_id)
            if account:
                account.total_live_sessions += 1
                account.total_live_minutes += duration_minutes
                account.total_commission += commission
                account.last_active_at = time.time()
                self._save()

    # ── 数据导出/导入 ────────────────────────────────────────────────────

    def export_account(self, account_id: str) -> dict:
        """导出账号完整数据（不含 API Key）。"""
        account = self.get(account_id)
        if not account:
            raise ValueError(f"账号 {account_id} 不存在")

        acc_dir = self.get_account_dir(account_id)
        export_data = {
            "format": "ailive-account-export",
            "version": 1,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "account": account.to_dict(),
        }

        # 导出商品库
        catalog_file = acc_dir / "sales_catalog.json"
        if catalog_file.exists():
            export_data["sales_catalog"] = json.loads(catalog_file.read_text(encoding="utf-8"))

        # 导出直播设置
        studio_file = acc_dir / "live_studio.json"
        if studio_file.exists():
            export_data["live_studio"] = json.loads(studio_file.read_text(encoding="utf-8"))

        return export_data

    def import_account(self, data: dict, overwrite: bool = False) -> Account:
        """导入账号数据。"""
        if not isinstance(data, dict):
            raise ValueError("导入数据必须是对象")
        account_data = data.get("account", {})
        if not isinstance(account_data, dict):
            raise ValueError("导入数据缺少有效的 account 对象")
        account_id = _validate_account_id(
            account_data.get("id") or uuid.uuid4().hex[:12]
        )
        account_data = dict(account_data)
        account_data["id"] = account_id

        with self._lock:
            if account_id in self._accounts and not overwrite:
                raise ValueError(f"账号 {account_id} 已存在，请使用 overwrite=true 覆盖")

            account = Account.from_dict(account_data)
            self._accounts[account_id] = account
            self._ensure_account_dir(account_id)

            # 导入商品库
            catalog = data.get("sales_catalog")
            if catalog:
                catalog_file = self.get_account_data_path(account_id, "sales_catalog.json")
                catalog_file.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")

            # 导入直播设置
            studio = data.get("live_studio")
            if studio:
                studio_file = self.get_account_data_path(account_id, "live_studio.json")
                studio_file.write_text(json.dumps(studio, ensure_ascii=False, indent=2), encoding="utf-8")

            self._save()
            logger.info("Account imported: %s (%s)", account.name, account_id)
            return account

    def status(self) -> dict:
        """获取账号管理状态。"""
        with self._lock:
            accounts_summary = []
            for a in self._accounts.values():
                accounts_summary.append({
                    "id": a.id,
                    "name": a.name,
                    "platform": a.platform,
                    "platform_label": PLATFORM_LABELS.get(a.platform, a.platform),
                    "status": a.status,
                    "avatar_id": a.avatar_id,
                    "total_live_sessions": a.total_live_sessions,
                    "total_live_minutes": round(a.total_live_minutes, 1),
                    "total_commission": round(a.total_commission, 2),
                    "last_active_at": a.last_active_at,
                    "is_active": a.id == self._active_account_id,
                })

            active = self._accounts.get(self._active_account_id)
            return {
                "accounts": accounts_summary,
                "total": len(self._accounts),
                "active_count": sum(1 for a in self._accounts.values() if a.status == "active"),
                "active_account_id": self._active_account_id,
                "active_account": active.to_dict() if active else None,
                "platforms": PLATFORM_LABELS,
            }


# 单例
account_manager = AccountManager()
account_manager._reload_account_data(account_manager.active_account_id)
