"""36 氪热榜。POST 接口，需毫秒时间戳。"""

import time

import httpx

from ..models import HotItem
from .base import DEFAULT_HEADERS, Source, SourceError


class Kr36Source(Source):
    key = "36kr"
    name = "36氪热榜"
    homepage = "https://36kr.com/hot-list/renqi"

    async def fetch(self, client: httpx.AsyncClient, limit: int) -> list[HotItem]:
        headers = {
            **DEFAULT_HEADERS,
            "Content-Type": "application/json",
            "Referer": "https://36kr.com/",
            "Origin": "https://36kr.com",
        }
        body = {
            "partner_id": "wap",
            "param": {"siteId": 1, "platformId": 2},
            "timestamp": int(time.time() * 1000),
        }
        resp = await client.post(
            "https://gateway.36kr.com/api/mis/nav/home/nav/rank/hot",
            json=body,
            headers=headers,
        )
        if resp.status_code != 200:
            raise SourceError(f"HTTP {resp.status_code}")

        payload = resp.json()
        rows = (payload.get("data") or {}).get("hotRankList") or []
        if not rows:
            raise SourceError(f"接口未返回 hotRankList（code={payload.get('code')}）")

        items: list[HotItem] = []
        for row in rows[:limit]:
            material = row.get("templateMaterial") or {}
            title = material.get("widgetTitle")
            if not title:
                continue
            item_id = row.get("itemId")
            score = material.get("statRead")
            items.append(
                HotItem(
                    rank=len(items) + 1,
                    title=title,
                    url=f"https://36kr.com/p/{item_id}" if item_id else None,
                    hot=float(score) if isinstance(score, (int, float)) else None,
                    # statRead 是阅读数。带上单位，否则一条「8808」摆在别家
                    # 「295.5万」旁边，用户会以为数据出错了
                    hot_text=f"{score}阅读" if score else None,
                    summary=self._snippet(material.get("summary")),
                    cover=material.get("widgetImage"),
                    extra={"author": material.get("authorName")},
                )
            )
        if not items:
            raise SourceError("解析后为空")
        return items
