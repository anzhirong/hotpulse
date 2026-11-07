"""运行期配置。插件里由 main.py 注入，独立使用时从环境变量读取。"""

import logging
import os
from dataclasses import dataclass


@dataclass
class Settings:
    cache_ttl: int = 300
    timeout: float = 8.0
    max_items: int = 50
    fail_ttl: int = 30
    weibo_cookie: str = ""
    zhihu_cookie: str = ""


_settings = Settings(
    weibo_cookie=os.environ.get("WEIBO_COOKIE", ""),
    zhihu_cookie=os.environ.get("ZHIHU_COOKIE", ""),
)

# 内核默认用自己的 logger；插件初始化时会把 AstrBot 的 logger 注入进来，
# 这样内核对外的报错才能出现在 AstrBot 的日志页里。
_logger: logging.Logger = logging.getLogger("astrbot_plugin_hotpulse")


def get_settings() -> Settings:
    return _settings


def get_logger() -> logging.Logger:
    return _logger


def set_logger(logger: logging.Logger) -> None:
    global _logger
    _logger = logger


def configure(**kwargs) -> None:
    """由插件在初始化时调用，覆盖默认配置。"""
    for key, value in kwargs.items():
        if value is not None and hasattr(_settings, key):
            setattr(_settings, key, value)
