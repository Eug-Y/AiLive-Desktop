###############################################################################
#  排品队列引擎 (Play Queue)
#  百货直播核心：商品排队 → 自动轮播 → 计时切换 → 预生成话术
###############################################################################

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

from utils.logger import logger


class PlayMode(Enum):
    SEQUENTIAL = "sequential"    # 顺序播放
    LOOP = "loop"                # 循环播放
    WEIGHTED = "weighted"        # 按佣金权重


@dataclass
class QueueItem:
    """排品队列中的每一项"""
    product_id: str
    product_name: str
    category: str                      # traffic / profit / welfare / regular
    duration: int                      # 讲解时长（秒）
    scripts: Dict[str, List[str]] = field(default_factory=dict)  # 所有类型话术
    played_count: int = 0
    total_played_seconds: float = 0.0
    started_at: Optional[float] = None


class PlayQueue:
    """排品队列引擎。

    管理百货直播的商品轮播流程：
    - 引流款 → 利润款 → 福利款 → 循环
    - 每个品自动计时，到时自动切换
    - 话术预生成，切换零延迟
    - 支持手动插播
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._items: List[QueueItem] = []
        self._current_index: int = -1
        self._mode = PlayMode.LOOP
        self._paused = False
        self._interrupt_event = threading.Event()
        self._last_switch_at: float = 0.0
        # 统计
        self._total_switches: int = 0
        self._total_play_seconds: float = 0.0
        self._switch_log: deque = deque(maxlen=200)

    # ── 队列管理 ──────────────────────────────────────────────────────

    def load_products(self, products: List[dict], script_generator):
        """从商品列表加载到排品队列。

        Args:
            products: 已核对已授权的商品列表
            script_generator: 话术生成函数 (product_id, script_type) -> List[str]
        """
        with self._lock:
            self._items = []
            for p in products:
                scripts = {}
                try:
                    for stype in ("hook", "present", "close", "transit", "welfare", "interact"):
                        scripts[stype] = script_generator(p["id"], stype)
                except Exception as e:
                    logger.warning("Failed to generate scripts for %s: %s", p["name"], e)
                    scripts["present"] = [f"接下来给大家介绍{p['name']}。"]

                item = QueueItem(
                    product_id=p["id"],
                    product_name=p["name"],
                    category=p.get("category", "regular"),
                    duration=p.get("play_duration", 300),
                    scripts=scripts,
                )
                self._items.append(item)

            self._current_index = -1
            self._total_switches = 0
            self._total_play_seconds = 0.0
            self._switch_log.clear()
            logger.info("PlayQueue loaded %d products", len(self._items))

    def set_mode(self, mode: PlayMode):
        with self._lock:
            self._mode = mode

    def insert_next(self, product: dict, scripts: Dict[str, List[str]]):
        """在当前品之后插入一个商品（手动插播）。"""
        with self._lock:
            item = QueueItem(
                product_id=product["id"],
                product_name=product["name"],
                category=product.get("category", "regular"),
                duration=product.get("play_duration", 120),
                scripts=scripts,
            )
            if self._current_index >= 0 and self._current_index < len(self._items):
                self._items.insert(self._current_index + 1, item)
            else:
                self._items.append(item)
            logger.info("Inserted product after current: %s", product["name"])

    def remove(self, product_id: str):
        with self._lock:
            self._items = [item for item in self._items if item.product_id != product_id]
            if self._current_index >= len(self._items):
                self._current_index = len(self._items) - 1

    # ── 轮播控制 ──────────────────────────────────────────────────────

    @property
    def current_item(self) -> Optional[QueueItem]:
        with self._lock:
            if 0 <= self._current_index < len(self._items):
                return self._items[self._current_index]
            return None

    def start(self):
        """开始排品（从第一个商品开始）。"""
        with self._lock:
            self._paused = False
            if self._items and self._current_index < 0:
                self._current_index = 0
                self._items[0].started_at = time.time()
                self._last_switch_at = time.time()
                logger.info("PlayQueue started: first product=%s", self._items[0].product_name)

    def pause(self):
        with self._lock:
            self._paused = True

    def resume(self):
        with self._lock:
            self._paused = False
            self._interrupt_event.set()

    def next(self) -> Optional[QueueItem]:
        """手动切换到下一个商品。"""
        return self._advance(manual=True)

    def _advance(self, manual: bool = False) -> Optional[QueueItem]:
        """切换到下一个商品，返回新的当前商品。"""
        with self._lock:
            if not self._items:
                return None

            # 结算当前品
            if self._current_index >= 0:
                current = self._items[self._current_index]
                if current.started_at:
                    current.total_played_seconds += time.time() - current.started_at
                    self._total_play_seconds += time.time() - current.started_at
                current.played_count += 1
                current.started_at = None

            # 选择下一个
            old_idx = self._current_index
            if self._mode == PlayMode.LOOP:
                self._current_index = (self._current_index + 1) % len(self._items)
            elif self._mode == PlayMode.SEQUENTIAL:
                if self._current_index + 1 < len(self._items):
                    self._current_index += 1
                else:
                    self._current_index = -1
                    return None
            else:  # WEIGHTED
                self._current_index = self._weighted_next()

            new_item = self._items[self._current_index]
            new_item.started_at = time.time()
            self._last_switch_at = time.time()
            self._total_switches += 1

            self._switch_log.append({
                "time": time.time(),
                "from": self._items[old_idx].product_name if old_idx >= 0 else "start",
                "to": new_item.product_name,
                "manual": manual,
            })

            logger.info("PlayQueue switched: %s → %s (mode=%s)",
                        self._items[old_idx].product_name if old_idx >= 0 else "start",
                        new_item.product_name, self._mode.value)
            return new_item

    def _weighted_next(self) -> int:
        """按佣金权重选择下一个商品（佣金高的概率大）。"""
        import random
        if not self._items:
            return -1
        # 简单实现：优先选 played_count 少的
        min_played = min(item.played_count for item in self._items)
        candidates = [i for i, item in enumerate(self._items) if item.played_count == min_played]
        if len(candidates) == 1:
            return candidates[0]
        # 从 candidates 中随机选一个非当前的
        others = [i for i in candidates if i != self._current_index]
        return random.choice(others) if others else candidates[0]

    def get_scripts(self, script_type: str = "present") -> List[str]:
        """获取当前商品指定类型的话术。"""
        item = self.current_item
        if item is None:
            return []
        return item.scripts.get(script_type, item.scripts.get("present", []))

    def get_play_sequence(self) -> List[dict]:
        """获取完整的排品序列（含当前进度）。"""
        with self._lock:
            result = []
            for i, item in enumerate(self._items):
                result.append({
                    "index": i,
                    "product_id": item.product_id,
                    "product_name": item.product_name,
                    "category": item.category,
                    "duration": item.duration,
                    "played_count": item.played_count,
                    "total_played_seconds": round(item.total_played_seconds, 1),
                    "is_current": i == self._current_index,
                    "progress": round(
                        (time.time() - item.started_at) / item.duration * 100, 1
                    ) if i == self._current_index and item.started_at else 0,
                })
            return result

    def status(self) -> dict:
        with self._lock:
            current = self.current_item
            return {
                "mode": self._mode.value,
                "paused": self._paused,
                "total_products": len(self._items),
                "current_index": self._current_index,
                "current_product": current.product_name if current else None,
                "current_category": current.category if current else None,
                "current_duration": current.duration if current else 0,
                "current_elapsed": round(time.time() - current.started_at, 1) if current and current.started_at else 0,
                "total_switches": self._total_switches,
                "total_play_seconds": round(self._total_play_seconds, 1),
                "sequence": self.get_play_sequence(),
                "recent_switches": list(self._switch_log)[-10:],
            }

    def analytics(self) -> dict:
        """导出直播数据分析。"""
        with self._lock:
            products_analysis = []
            for item in self._items:
                if item.played_count > 0:
                    products_analysis.append({
                        "product_id": item.product_id,
                        "name": item.product_name,
                        "category": item.category,
                        "played_count": item.played_count,
                        "total_seconds": round(item.total_played_seconds, 1),
                        "avg_seconds_per_play": round(
                            item.total_played_seconds / item.played_count, 1
                        ) if item.played_count > 0 else 0,
                    })
            products_analysis.sort(key=lambda x: x["total_seconds"], reverse=True)
            return {
                "total_play_seconds": round(self._total_play_seconds, 1),
                "total_switches": self._total_switches,
                "products_played": len(products_analysis),
                "products_detail": products_analysis,
                "switch_log": list(self._switch_log),
            }

    def clear(self):
        with self._lock:
            self._items = []
            self._current_index = -1
            self._switch_log.clear()
            self._total_switches = 0
            self._total_play_seconds = 0.0