"""核心抓取模块（自包含，仅依赖 httpx 与标准库）。

刻意不依赖 fastapi / pydantic：插件跑在 AstrBot 进程里，
只需要「抓取 + 归一化」这一层能力，HTTP 服务那套东西在这里是负担。
"""

from .cloud import (
    ACTIVITY_SITES,
    DEFAULT_SPEC,
    SPECS,
    Quote,
    QuoteResult,
    aliyun_signature,
    compare,
    extract_promo_lines,
    fetch_activities,
    fetch_aliyun_price,
    fetch_tencent_price,
    tencent_authorization,
)
from .fetcher import HotPulseFetcher
from .match import Cluster, cluster_hits, match_title, split_def, variants
from .models import HotItem, SourceResult
from .monitor import SubscriptionStore
from .settings import (
    Settings,
    configure,
    get_logger,
    get_settings,
    set_logger,
)
from .sources import ALL_SOURCES, resolve, resolve_many

__all__ = [
    "ACTIVITY_SITES",
    "ALL_SOURCES",
    "DEFAULT_SPEC",
    "SPECS",
    "Cluster",
    "HotItem",
    "HotPulseFetcher",
    "Quote",
    "QuoteResult",
    "Settings",
    "SourceResult",
    "SubscriptionStore",
    "aliyun_signature",
    "cluster_hits",
    "compare",
    "configure",
    "extract_promo_lines",
    "fetch_activities",
    "fetch_aliyun_price",
    "fetch_tencent_price",
    "get_logger",
    "get_settings",
    "match_title",
    "resolve",
    "resolve_many",
    "set_logger",
    "split_def",
    "tencent_authorization",
    "variants",
]
