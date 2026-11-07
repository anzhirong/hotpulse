"""微博热搜。"""

from urllib.parse import quote

import httpx

from ..models import HotItem
from ..settings import get_settings
from .base import DEFAULT_HEADERS, Source, SourceError


class WeiboSource(Source):
    key = "weibo"
    name = "微博热搜"
    homepage = "https://s.weibo.com/top/summary"

    async def fetch(self, client: httpx.AsyncClient, limit: int) -> list[HotItem]:
        headers = {**DEFAULT_HEADERS, "Referer": "https://weibo.com/"}
        cookie = get_settings().weibo_cookie
        if cookie:
            headers["Cookie"] = cookie

        resp = await client.get("https://weibo.com/ajax/side/hotSearch", headers=headers)
        if resp.status_code != 200:
            raise SourceError(f"HTTP {resp.status_code}")

        realtime = (resp.json().get("data") or {}).get("realtime") or []
        if not realtime:
            raise SourceError("未返回 realtime 数据，通常需要在插件配置里填写微博 Cookie")

        items: list[HotItem] = []
        for row in realtime[:limit]:
            word = row.get("word") or row.get("note")
            if not word:
                continue
            num = row.get("num")
            note = row.get("note")
            items.append(
                HotItem(
                    rank=len(items) + 1,
                    title=word,
                    url=f"https://s.weibo.com/weibo?q=%23{quote(word)}%23",
                    hot=float(num) if isinstance(num, (int, float)) else None,
                    hot_text=f"{num}" if num else None,
                    summary=note if note and note != word else None,
                    extra={"label": row.get("label_name") or None},
                )
            )
        if not items:
            raise SourceError("解析后为空")
        return items
