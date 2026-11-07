"""今日头条热榜。HotValue 是字符串，Image 是对象，都需转换。"""

import re

import httpx

from ..models import HotItem
from .base import DEFAULT_HEADERS, Source, SourceError

# 原始 Url 带一长串埋点参数（log_pb 等），单条能到 400 字符，直接发出去会刷屏。
# 只保留 trending 的 id 段，打开后是同一个页面。
_TRENDING_ID = re.compile(r"/trending/(\d+)")


def _clean_url(raw: str | None) -> str | None:
    if not raw:
        return None
    match = _TRENDING_ID.search(raw)
    if match:
        return f"https://www.toutiao.com/trending/{match.group(1)}/"
    return raw


class ToutiaoSource(Source):
    key = "toutiao"
    name = "头条热榜"
    homepage = "https://www.toutiao.com/"

    async def fetch(self, client: httpx.AsyncClient, limit: int) -> list[HotItem]:
        headers = {**DEFAULT_HEADERS, "Referer": "https://www.toutiao.com/"}
        resp = await client.get(
            "https://www.toutiao.com/hot-event/hot-board/",
            params={"origin": "toutiao_pc"},
            headers=headers,
        )
        if resp.status_code != 200:
            raise SourceError(f"HTTP {resp.status_code}")

        rows = resp.json().get("data") or []
        if not rows:
            raise SourceError("接口未返回 data 数据")

        items: list[HotItem] = []
        for row in rows[:limit]:
            title = row.get("Title") or row.get("title")
            if not title:
                continue
            hot_value = row.get("HotValue") or row.get("hotValue")
            cover = row.get("Image") or row.get("image")
            if isinstance(cover, dict):
                cover = cover.get("uri") or cover.get("url")
            elif not isinstance(cover, str):
                cover = None
            items.append(
                HotItem(
                    rank=len(items) + 1,
                    title=title,
                    url=_clean_url(row.get("Url") or row.get("url")),
                    hot=self._to_float(hot_value),
                    hot_text=str(hot_value) if hot_value else None,
                    summary=self._snippet(row.get("LabelDesc") or row.get("labelDesc")),
                    cover=cover,
                    extra={"label": row.get("Label") or None},
                )
            )
        if not items:
            raise SourceError("解析后为空")
        return items
