"""抓取调度：并发、降级、缓存。

设计要点：
- 任何一个源失败都只影响自己，绝不抛给调用方
- 缓存按「源」维度，存全量后在返回时截断，避免不同条数互相击穿
- 失败也记一笔短缓存，避免某个源挂掉时每次查询都白打一次上游
- 连接池全局复用，省掉每次请求的 TLS 握手
"""

import asyncio
import time

import httpx

from .models import HotItem, SourceResult
from .settings import get_logger, get_settings
from .sources import ALL_SOURCES
from .sources.base import DEFAULT_HEADERS, Source


class HotPulseFetcher:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._cache: dict[str, tuple[float, list[HotItem]]] = {}
        self._inflight: dict[str, asyncio.Task] = {}
        self._fail: dict[str, float] = {}

    async def start(self) -> None:
        if self._client is not None:
            return
        settings = get_settings()
        self._client = httpx.AsyncClient(
            timeout=settings.timeout,
            headers=DEFAULT_HEADERS,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get_items(self, source: Source) -> tuple[list[HotItem], bool]:
        settings = get_settings()
        now = time.monotonic()

        hit = self._cache.get(source.key)
        if hit and hit[0] > now:
            return hit[1], True

        # 单飞：并发进来同一个源只真正请求一次
        existing = self._inflight.get(source.key)
        if existing is not None:
            return await existing, False

        if self._client is None:
            await self.start()

        task = asyncio.create_task(source.fetch(self._client, settings.max_items))
        self._inflight[source.key] = task
        try:
            items = await task
            self._cache[source.key] = (now + settings.cache_ttl, items)
            return items, False
        finally:
            self._inflight.pop(source.key, None)

    def _fresh_cache(self, key: str) -> bool:
        hit = self._cache.get(key)
        return bool(hit and hit[0] > time.monotonic())

    async def fetch_one(self, source: Source, limit: int) -> SourceResult:
        settings = get_settings()
        logger = get_logger()
        started = time.perf_counter()

        # 有新鲜缓存就优先用它；没有才看失败负缓存，避免「明明有数据却报失败」
        if not self._fresh_cache(source.key):
            until = self._fail.get(source.key, 0.0)
            if until > time.monotonic():
                return SourceResult(
                    source=source.key,
                    source_name=source.name,
                    ok=False,
                    error="刚刚失败，稍后自动重试",
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                )

        try:
            items, cached = await asyncio.wait_for(
                self._get_items(source), timeout=settings.timeout + 2
            )
            self._fail.pop(source.key, None)
            return SourceResult(
                source=source.key,
                source_name=source.name,
                ok=True,
                items=items[:limit],
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                cached=cached,
            )
        except TimeoutError:
            self._fail[source.key] = time.monotonic() + settings.fail_ttl
            logger.warning("热点源 %s 超时", source.key)
            return SourceResult(
                source=source.key,
                source_name=source.name,
                ok=False,
                error="上游超时",
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )
        except Exception as exc:
            self._fail[source.key] = time.monotonic() + settings.fail_ttl
            logger.warning("热点源 %s 失败: %s: %s", source.key, type(exc).__name__, exc)
            return SourceResult(
                source=source.key,
                source_name=source.name,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )

    async def fetch_many(self, sources: list[Source], limit: int) -> list[SourceResult]:
        if not sources:
            sources = list(ALL_SOURCES)
        return list(await asyncio.gather(*(self.fetch_one(s, limit) for s in sources)))

    def clear_cache(self) -> None:
        self._cache.clear()
        self._fail.clear()
