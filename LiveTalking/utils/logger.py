import logging
import logging.handlers
import os
from pathlib import Path

# ── 日志配置 ──────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).resolve().parents[1] / "logs"
LOG_DIR.mkdir(exist_ok=True)

logger = logging.getLogger("livetalking")
logger.setLevel(logging.DEBUG)

# 格式
formatter = logging.Formatter(
    '%(asctime)s [%(levelname)s] %(name)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)

# 1. 主日志 — 按大小轮转（10MB × 5 个备份）
main_handler = logging.handlers.RotatingFileHandler(
    LOG_DIR / "livetalking.log",
    maxBytes=10 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
main_handler.setLevel(logging.INFO)
main_handler.setFormatter(formatter)
logger.addHandler(main_handler)

# 2. 错误日志 — 单独记录 WARNING 及以上
error_handler = logging.handlers.RotatingFileHandler(
    LOG_DIR / "error.log",
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8",
)
error_handler.setLevel(logging.WARNING)
error_handler.setFormatter(formatter)
logger.addHandler(error_handler)

# 3. 控制台输出 — DEBUG 级别（开发调试用）
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.DEBUG)
console_handler.setFormatter(logging.Formatter(
    '[%(levelname)s] %(name)s - %(message)s'
))
logger.addHandler(console_handler)

# 禁止日志传播到 root logger
logger.propagate = False