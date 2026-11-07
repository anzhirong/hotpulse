"""核心抓取模块（自包含，仅依赖 httpx 与标准库）。

刻意不依赖 fastapi / pydantic：插件跑在 AstrBot 进程里，
只需要「抓取 + 归一化」这一层能力，HTTP 服务那套东西在这里是负担。
"""

from .fetcher import HotPulseFetcher
from .models import HotItem, SourceResult
from .monitor import SubscriptionStore, match_title
from .settings import (
    Settings,
    configure,
    get_logger,
    get_settings,
    set_logger,
)
from .sources import ALL_SOURCES, resolve, resolve_many

__all__ = [
    "ALL_SOURCES",
    "HotItem",
    "HotPulseFetcher",
    "Settings",
    "SourceResult",
    "SubscriptionStore",
    "configure",
    "get_logger",
    "get_settings",
    "match_title",
    "resolve",
    "resolve_many",
    "set_logger",
]
