"""B 站热门。"""

import httpx

from ..models import HotItem
from .base import DEFAULT_HEADERS, Source, SourceError


class BilibiliSource(Source):
    key = "bilibili"
    name = "B站热门"
    homepage = "https://www.bilibili.com/v/popular/rank/all"

    async def fetch(self, client: httpx.AsyncClient, limit: int) -> list[HotItem]:
        headers = {**DEFAULT_HEADERS, "Referer": "https://www.bilibili.com/"}
        resp = await client.get(
            "https://api.bilibili.com/x/web-interface/ranking/v2",
            params={"rid": 0, "type": "all"},
            headers=headers,
        )
        if resp.status_code != 200:
            raise SourceError(f"HTTP {resp.status_code}")

        payload = resp.json()
        if payload.get("code") != 0:
            raise SourceError(f"接口返回 code={payload.get('code')}")

        rows = (payload.get("data") or {}).get("list") or []
        if not rows:
            raise SourceError("接口未返回 list 数据")

        items: list[HotItem] = []
        for row in rows[:limit]:
            title = row.get("title")
            if not title:
                continue
            bvid = row.get("bvid")
            view = (row.get("stat") or {}).get("view")
            url = f"https://www.bilibili.com/video/{bvid}" if bvid else row.get("short_link_v2")
            items.append(
                HotItem(
                    rank=len(items) + 1,
                    title=title,
                    url=url,
                    hot=float(view) if isinstance(view, int) else None,
                    hot_text=f"{view} 播放" if view else None,
                    summary=self._snippet(row.get("desc")),
                    cover=row.get("pic"),
                    extra={"up": (row.get("owner") or {}).get("name"), "bvid": bvid},
                )
            )
        if not items:
            raise SourceError("解析后为空")
        return items
