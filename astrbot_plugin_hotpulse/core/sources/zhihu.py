"""知乎热榜。2026 年起强制登录校验，无 cookie 必然 401。"""

import httpx

from ..models import HotItem
from ..settings import get_settings
from .base import DEFAULT_HEADERS, Source, SourceError


class ZhihuSource(Source):
    key = "zhihu"
    name = "知乎热榜"
    homepage = "https://www.zhihu.com/hot"

    async def fetch(self, client: httpx.AsyncClient, limit: int) -> list[HotItem]:
        headers = {**DEFAULT_HEADERS, "Referer": "https://www.zhihu.com/hot"}
        cookie = get_settings().zhihu_cookie
        if not cookie:
            raise SourceError("知乎已强制登录校验，请在插件配置中填写知乎 Cookie")
        headers["Cookie"] = cookie

        resp = await client.get(
            "https://www.zhihu.com/api/v3/feed/topstory/hot-lists/total",
            params={"limit": min(limit, 50), "desktop": "true"},
            headers=headers,
        )
        if resp.status_code != 200:
            raise SourceError(f"HTTP {resp.status_code}")

        rows = resp.json().get("data") or []
        if not rows:
            raise SourceError("接口未返回 data 数据")

        items: list[HotItem] = []
        for row in rows[:limit]:
            target = row.get("target") or {}
            title = target.get("title") or row.get("title")
            if not title:
                continue
            detail = row.get("detail_text") or ""
            items.append(
                HotItem(
                    rank=len(items) + 1,
                    title=title,
                    url=target.get("url") or row.get("url"),
                    hot=self._to_float(detail.replace("热度", "")) if detail else None,
                    hot_text=detail or None,
                    summary=self._snippet(target.get("excerpt")),
                    cover=target.get("image_url"),
                )
            )
        if not items:
            raise SourceError("解析后为空")
        return items
