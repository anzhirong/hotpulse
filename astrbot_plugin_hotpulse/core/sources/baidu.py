"""百度热搜榜。注意真实结构是 cards[].content[].content[]，多一层 component 包裹。"""

import httpx

from ..models import HotItem
from .base import DEFAULT_HEADERS, Source, SourceError


class BaiduSource(Source):
    key = "baidu"
    name = "百度热搜"
    homepage = "https://top.baidu.com/board?tab=realtime"

    async def fetch(self, client: httpx.AsyncClient, limit: int) -> list[HotItem]:
        headers = {**DEFAULT_HEADERS, "Referer": "https://top.baidu.com/board?tab=realtime"}
        resp = await client.get(
            "https://top.baidu.com/api/board",
            params={"platform": "wise", "tab": "realtime"},
            headers=headers,
        )
        if resp.status_code != 200:
            raise SourceError(f"HTTP {resp.status_code}")

        cards = (resp.json().get("data") or {}).get("cards") or []
        rows: list[dict] = []
        for card in cards:
            for block in card.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("word"):
                    rows.append(block)
                else:
                    rows.extend(b for b in (block.get("content") or []) if isinstance(b, dict))
        if not rows:
            raise SourceError("接口未返回 content 数据")

        items: list[HotItem] = []
        for row in rows[:limit]:
            word = row.get("word") or row.get("query")
            if not word:
                continue
            score = row.get("hotScore")
            items.append(
                HotItem(
                    rank=len(items) + 1,
                    title=word,
                    url=row.get("url") or row.get("rawUrl") or row.get("appUrl"),
                    hot=float(score) if isinstance(score, (int, float)) else self._to_float(score),
                    hot_text=f"{score}" if score else None,
                    summary=self._snippet(row.get("desc")),
                    cover=row.get("img"),
                    extra={"tag": row.get("newHotName") or None},
                )
            )
        if not items:
            raise SourceError("解析后为空")
        return items
