"""数据源基类。"""

from abc import ABC, abstractmethod

import httpx

from ..models import HotItem

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


class SourceError(Exception):
    """适配器内部错误，上层会捕获并降级为该源失败。"""


class Source(ABC):
    key: str = ""
    name: str = ""
    homepage: str = ""

    @abstractmethod
    async def fetch(self, client: httpx.AsyncClient, limit: int) -> list[HotItem]:
        """抓取并返回标准化热点列表，失败抛 SourceError。"""

    @staticmethod
    def _to_float(value) -> float | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip().replace(",", "").replace(" ", "")
        if not text:
            return None
        multiplier = 1.0
        if text.endswith("万"):
            multiplier, text = 1e4, text[:-1]
        elif text.endswith("亿"):
            multiplier, text = 1e8, text[:-1]
        try:
            return float(text) * multiplier
        except ValueError:
            return None

    @staticmethod
    def _snippet(text: str | None, size: int = 120) -> str | None:
        if not text:
            return None
        text = " ".join(str(text).split())
        return text if len(text) <= size else text[:size] + "…"
