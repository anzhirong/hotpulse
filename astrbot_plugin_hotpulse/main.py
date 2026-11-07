"""hotpulse · AstrBot 插件

在飞书 / QQ / Telegram 等平台里直接查询各平台实时热榜，并支持关键词监控：
命中关注词的标题会主动推送到订阅过的会话。抓取逻辑完全内嵌，不需要额外起服务。
"""

import asyncio
import importlib.util
import sys
from datetime import datetime
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:  # 主动推送需要构造消息链；拿不到就只降级掉监控功能，不影响查询
    from astrbot.core.message.message_event_result import MessageChain
except Exception:  # pragma: no cover
    MessageChain = None  # type: ignore[assignment]

# ---- 加载随插件分发的核心包 ----
# 这里不用相对导入：AstrBot 不同版本加载插件的方式不一样（按包导入 / 按文件导入），
# 相对导入在前者可行、在后者会报 "no known parent package"。
# 用 importlib 把 core/ 显式注册成独立包，两种加载方式都能工作。
_CORE_PKG = "astrbot_plugin_hotpulse_core"


def _load_core():
    if _CORE_PKG in sys.modules:
        return sys.modules[_CORE_PKG]

    core_dir = Path(__file__).resolve().parent / "core"
    spec = importlib.util.spec_from_file_location(
        _CORE_PKG,
        core_dir / "__init__.py",
        submodule_search_locations=[str(core_dir)],
    )
    if spec is None or spec.loader is None:  # pragma: no cover
        raise ImportError(f"无法加载插件核心目录：{core_dir}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[_CORE_PKG] = module
    spec.loader.exec_module(module)
    return module


_core = _load_core()
HotPulseFetcher = _core.HotPulseFetcher
SubscriptionStore = _core.SubscriptionStore
match_title = _core.match_title
cluster_hits = _core.cluster_hits
split_def = _core.split_def
variants = _core.variants
configure = _core.configure
set_logger = _core.set_logger
ALL_SOURCES = _core.ALL_SOURCES
resolve = _core.resolve
resolve_many = _core.resolve_many

# 命令前缀。带斜杠的必须排在前面，否则 "/热点 微博" 会被 "热点" 提前截断
_PREFIXES = ("/热点", "/热榜", "/hot", "热点", "热榜", "hot")

# 这些词开头的查询走「关键词筛选」，而不是当作平台名
_SEARCH_WORDS = {"搜", "搜索", "找", "查", "筛选"}

_HELP = (
    "🔥 热点查询用法\n"
    "· /热点            默认几个平台各 5 条\n"
    "· /热点 微博       只看单个平台\n"
    "· /热点 微博 10    指定条数（1-20）\n"
    "· /热点 微博 百度  一次查多个平台\n"
    "· /热点 搜 AI      从全平台里筛出含关键词的\n"
    "· /热点源          查看所有可用平台\n"
    "· /热点监控 AI     订阅关键词，上榜即推送"
)

# 这些不是摘要，只是平台的标记，显示出来反而误导
_NOISE_SUMMARY = {"新事件上榜", "热门", "热榜", "置顶", "刚刚", "热点"}


def _hot_text(item) -> str | None:
    """把热度统一成「万/亿」，保留原来的单位后缀（播放等）。"""
    value = getattr(item, "hot", None)
    raw = getattr(item, "hot_text", None)
    if value is None:
        return raw
    unit = ""
    for candidate in ("播放", "热度", "阅读"):
        if raw and candidate in raw:
            unit = candidate
            break
    if value >= 1e8:
        return f"{value / 1e8:.1f}亿{unit}"
    if value >= 1e4:
        return f"{value / 1e4:.1f}万{unit}"
    return f"{int(value)}{unit}"


@register("hotpulse", "anzhirong", "多平台热点聚合查询", "1.2.0")
class HotPulsePlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        # AstrBot 在有 _conf_schema.json 时会传入配置对象；这里做兼容处理，
        # 避免因配置对象类型差异直接把插件搞崩
        self._raw_config = config
        self.fetcher = HotPulseFetcher()
        self._store: SubscriptionStore | None = None
        self._monitor_task: asyncio.Task | None = None
        self._kick_task: asyncio.Task | None = None

    # ---------- 生命周期 ----------

    async def initialize(self):
        """实例化后自动调用：注入配置、建立连接池、启动监控循环。"""
        # 把内核的日志接到 AstrBot 上，这样报错能在 WebUI 日志页里看到
        set_logger(logger)

        configure(
            cache_ttl=self._as_int(self._cfg("cache_ttl", 300), 300),
            timeout=self._as_float(self._cfg("timeout", 8), 8.0),
            weibo_cookie=str(self._cfg("weibo_cookie", "") or ""),
            zhihu_cookie=str(self._cfg("zhihu_cookie", "") or ""),
        )

        store_path = Path(__file__).resolve().parent / "data" / "subscriptions.json"
        self._store = SubscriptionStore.load(store_path)

        # 配置里填了初始关键词、而本地还没有订阅时，导入一次（之后以命令为准）
        seed = str(self._cfg("monitor_keywords", "") or "")
        if not self._store.keywords and seed:
            added = self._store.add_keywords(_split_words(seed))
            if added:
                self._store.save()
                logger.info("hotpulse 从配置导入关键词 %d 个", len(added))

        try:
            await self.fetcher.start()
        except Exception as exc:
            logger.error(f"hotpulse 初始化连接池失败：{exc}")

        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info("hotpulse 插件已加载（订阅关键词 %d 个）", len(self._store.keywords))

    async def terminate(self):
        """插件卸载/停用时停掉后台任务并释放连接池。"""
        for task in (self._monitor_task, self._kick_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:  # pragma: no cover
                    logger.warning(f"hotpulse 停止后台任务时出错：{exc}")
        await self.fetcher.close()

    # ---------- 指令 ----------

    @filter.command("热点", alias={"热榜", "热点新闻"})
    async def hot(self, event: AstrMessageEvent):
        """查询各平台实时热点。用法：/热点 [平台] [条数]"""
        raw = self._strip_prefix(event.message_str)

        # 监控是 /热点 的子命令：这样不会和 /热点 抢前缀，也不会被提前截断
        if raw.startswith("监控"):
            async for reply in self._monitor_command(event, raw):
                yield reply
            return

        tokens = _split_words(raw)

        # 「搜」走关键词筛选：全平台抓一遍，只在本地过滤，不额外请求上游
        if tokens and tokens[0] in _SEARCH_WORDS:
            keywords = tokens[1:]
            if not keywords:
                yield event.plain_result(
                    "用法：/热点 搜 关键词\n"
                    "例：/热点 搜 AI\n"
                    "    /热点 搜 AI=AI,大模型,GPT,DeepSeek（自己定义同义词组）"
                )
                return
            async for reply in self._search(event, keywords):
                yield reply
            return

        limit = self._default_limit()
        source_tokens: list[str] = []
        for token in tokens:
            if token.isdigit():
                limit = max(1, min(int(token), 20))
            else:
                source_tokens.append(token)

        unknown: list[str] = []
        if source_tokens:
            sources = resolve_many(source_tokens)
            unknown = [t for t in source_tokens if resolve(t) is None]
            if not sources:
                yield event.plain_result(f"没认出来「{'、'.join(unknown)}」是哪个平台。\n\n{_HELP}")
                return
        else:
            sources = self._default_sources()

        try:
            results = await self.fetcher.fetch_many(sources, limit)
        except Exception as exc:
            logger.error(f"hotpulse 抓取异常：{exc}")
            yield event.plain_result("⚠️ 抓取出错了，稍后再试一次。")
            return

        yield event.plain_result(self._render(results, unknown))

    @filter.command("热点源", alias={"hotpulse"})
    async def list_sources(self, event: AstrMessageEvent):
        """列出所有可用的热点平台。"""
        lines = ["📡 可用平台（可直接写中文）", ""]
        for source in ALL_SOURCES:
            lines.append(f"· {source.name}（{source.key}）")
        lines.append("")
        lines.append("例：/热点 微博 8")
        yield event.plain_result("\n".join(lines))

    # ---------- 关键词筛选 ----------

    async def _search(self, event: AstrMessageEvent, keywords: list[str]):
        """抓全平台后在本地过滤、合并同一事件，按跨平台数排序。"""
        try:
            results = await self.fetcher.fetch_many(list(ALL_SOURCES), 50)
        except Exception as exc:
            logger.error(f"hotpulse 搜索抓取异常：{exc}")
            yield event.plain_result("⚠️ 抓取出错了，稍后再试一次。")
            return

        pairs = []
        for result in results:
            if not result.ok:
                continue
            for item in result.items:
                if match_title(item.title, keywords):
                    pairs.append((result.source_name, item))

        if not pairs:
            failed = [r.source_name for r in results if not r.ok]
            hint = f"\n（本次不可用：{'、'.join(failed)}）" if failed else ""
            yield event.plain_result(
                f"🔍 搜「{'、'.join(keywords)}」没有结果。\n"
                f"榜单只覆盖当前热榜，冷门词可能确实不在榜上。{hint}"
            )
            return

        clusters = cluster_hits(pairs)
        yield event.plain_result(self._render_search(keywords, clusters, results))

    @staticmethod
    def _render_search(keywords: list[str], clusters, results) -> str:
        multi = sum(1 for c in clusters if len(c.sources) > 1)
        subtitle = f"{len(clusters)} 组结果"
        if multi:
            subtitle += f"，其中 {multi} 组多个平台同时上榜"
        lines = [
            f"🔍 搜「{'、'.join(keywords)}」· {datetime.now():%m-%d %H:%M}",
            subtitle,
            "",
        ]
        for index, cluster in enumerate(clusters):
            if index >= 15:
                lines.append(f"…另有 {len(clusters) - index} 组，缩小关键词可看更多")
                break
            lines.append(f"{index + 1}. {cluster.title}")
            tags = " · ".join(cluster.sources)
            lines.append(f"   [{tags}]")
        failed = [r.source_name for r in results if not r.ok]
        if failed:
            lines.append("")
            lines.append("⚠️ 本次不可用：" + "、".join(failed))
        lines.append("")
        lines.append("想持续盯这个词：/热点监控 " + "、".join(keywords))
        return "\n".join(lines)

    # ---------- 关键词监控 ----------

    async def _monitor_command(self, event: AstrMessageEvent, raw: str):
        """处理 /热点监控 系列子命令。"""
        store = self._store
        if store is None:
            yield event.plain_result("⚠️ 插件尚未初始化完成，稍后再试。")
            return

        rest = raw[len("监控") :].strip()

        if rest.startswith("列表") or not rest:
            yield event.plain_result(self._render_monitor_status())
            return

        if rest.startswith("清空"):
            count = store.clear_keywords()
            store.save()
            yield event.plain_result(f"已清空 {count} 个关键词，监控暂停。")
            return

        if rest.startswith("删除"):
            words = _split_words(rest[len("删除") :])
            if not words:
                yield event.plain_result("用法：/热点监控删除 关键词（多个用逗号分隔）")
                return
            removed = store.remove_keywords(words)
            store.save()
            if removed:
                yield event.plain_result(
                    f"已删除：{'、'.join(removed)}\n剩余 {len(store.keywords)} 个关键词。"
                )
            else:
                yield event.plain_result(f"没找到这些关键词：{'、'.join(words)}")
            return

        if rest.startswith("关闭"):
            store.paused = True
            store.save()
            yield event.plain_result("⏸ 关键词监控已暂停。发 /热点监控开启 恢复。")
            return

        if rest.startswith("开启"):
            store.paused = False
            store.save()
            yield event.plain_result(f"▶️ 关键词监控已恢复，当前 {len(store.keywords)} 个关键词。")
            return

        # 其余情况一律当作「添加关键词」
        words = _split_words(rest)
        if not words:
            yield event.plain_result(self._render_monitor_status())
            return

        added = store.add_keywords(words)
        store.remember_session(self._event_session(event))
        store.paused = False
        store.save()

        if not added:
            yield event.plain_result(
                f"这些词已经在监控里了：{'、'.join(words)}\n"
                f"当前共 {len(store.keywords)} 个。发 /热点监控列表 查看。"
            )
            return

        interval = self._monitor_interval()
        yield event.plain_result(
            f"✅ 已开始监控：{'、'.join(added)}\n"
            f"共 {len(store.keywords)} 个词，每 {interval // 60} 分钟扫描一次，"
            f"命中新标题就推到这个会话。\n\n"
            f"· /热点监控列表 查看\n"
            f"· /热点监控删除 {'、'.join(added[:2])} 取消"
        )

        # 加完立刻扫一轮，不用等下一个周期；保存引用避免任务被 GC 提前回收
        if self._kick_task is not None and not self._kick_task.done():
            self._kick_task.cancel()
        self._kick_task = asyncio.create_task(self._monitor_once())

    async def _monitor_loop(self):
        """后台循环。首次等待 2 分钟，避免开机时和用户查询抢上游。"""
        interval = self._monitor_interval()
        try:
            await asyncio.sleep(120)
            while True:
                try:
                    await self._monitor_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error(f"hotpulse 监控循环异常：{exc}")
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("hotpulse 监控循环已停止")
            raise

    async def _monitor_once(self):
        """扫一遍所有源，推本次新命中的标题。"""
        store = self._store
        if store is None or store.paused:
            return
        if not store.keywords or not store.sessions:
            return
        if MessageChain is None:
            logger.warning("hotpulse: 当前 AstrBot 版本不支持主动发消息，监控不可用")
            return
        if self._in_quiet_hours():
            return

        results = await self.fetcher.fetch_many(list(ALL_SOURCES), 50)

        hits: list[tuple[str, object]] = []
        for result in results:
            if not result.ok:
                continue
            for item in result.items:
                if store.is_seen(item.title):
                    continue
                if match_title(item.title, store.keywords):
                    hits.append((result.source_name, item))

        if not hits:
            return

        text = _render_hits(hits)
        delivered = 0
        for session in list(store.sessions):
            try:
                ok = await self.context.send_message(session, MessageChain().message(text))
                delivered += 1 if ok else 0
            except Exception as exc:
                logger.warning(f"hotpulse 推送到 {session} 失败：{exc}")

        # 无论是否送达都记为已见，避免推不出去时每轮重复尝试刷日志
        store.mark_seen([item.title for _, item in hits])
        store.save()
        logger.info(f"hotpulse 命中 {len(hits)} 条，送达 {delivered} 个会话")

    def _render_monitor_status(self) -> str:
        store = self._store
        assert store is not None
        interval = self._monitor_interval()

        if store.paused:
            state = "已暂停"
        elif store.keywords:
            state = "运行中"
        else:
            state = "未设置关键词"

        lines = ["🎯 关键词监控", ""]
        lines.append(f"状态：{state} · 每 {interval // 60} 分钟扫描一次")
        quiet = self._quiet_range_text()
        if quiet:
            lines.append(f"静默时段：{quiet}")
        if store.keywords:
            lines.append(f"关注（{len(store.keywords)}）：{'、'.join(store.keywords)}")
        else:
            lines.append("关注：（空）")
        lines.append(f"推送会话：{len(store.sessions)} 个")
        lines.append("")
        lines.append("用法：")
        lines.append("· /热点监控 AI           加词（逗号可加多个）")
        lines.append("· /热点监控列表          看当前设置")
        lines.append("· /热点监控删除 AI       删词")
        lines.append("· /热点监控清空          清空全部")
        lines.append("· /热点监控关闭 / 开启   暂停或恢复")
        lines.append("")
        lines.append("提示：直接写 AI 会自动展开成一组同义词（大模型/GPT/DeepSeek…），")
        lines.append("也可以自己定义：/热点监控 AI=AI,大模型,GPT,智能体")
        return "\n".join(lines)

    def _in_quiet_hours(self) -> bool:
        start = self._as_int(self._cfg("quiet_start", 23), 23)
        end = self._as_int(self._cfg("quiet_end", 7), 7)
        if start == end:
            return False
        hour = datetime.now().hour
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def _quiet_range_text(self) -> str:
        start = self._as_int(self._cfg("quiet_start", 23), 23)
        end = self._as_int(self._cfg("quiet_end", 7), 7)
        if start == end:
            return ""
        return f"{start:02d}:00 - {end:02d}:00 不抓不推"

    def _monitor_interval(self) -> int:
        # 低于 5 分钟的间隔会让抓取频率变成"监控"，对上游和自己都没好处
        return max(300, self._as_int(self._cfg("monitor_interval", 1800), 1800))

    # ---------- 内部工具 ----------

    @staticmethod
    def _strip_prefix(message_str: str) -> str:
        raw = (message_str or "").strip()
        for prefix in _PREFIXES:
            if raw.lower().startswith(prefix.lower()):
                return raw[len(prefix) :].strip()
        return raw

    @staticmethod
    def _event_session(event: AstrMessageEvent) -> str:
        for attr in ("unified_msg_origin", "session"):
            value = getattr(event, attr, None)
            if isinstance(value, str) and value:
                return value
        return ""

    @staticmethod
    def _render(results, unknown: list[str]) -> str:
        ok = [r for r in results if r.ok and r.items]
        if not ok:
            detail = "\n".join(f"· {r.source_name}：{r.error}" for r in results)
            return f"⚠️ 这次没抓到数据，稍后再试\n\n{detail or '无可用数据源'}"

        lines = [f"🔥 热点 · {datetime.now():%m-%d %H:%M}", ""]
        single = len(ok) == 1
        for result in ok:
            lines.append(f"【{result.source_name}】")
            for item in result.items:
                lines.append(f"{item.rank}. {item.title}")
                # 多平台时只给标题，避免消息过长；单平台才展开热度/简介/链接
                if not single:
                    continue
                extra = item.extra or {}
                hot = _hot_text(item)
                up = extra.get("up")
                meta = [x for x in (hot, f"UP：{up}" if up else None) if x]
                if meta:
                    lines.append("   " + " · ".join(meta))
                if item.summary and item.summary not in _NOISE_SUMMARY:
                    lines.append("   " + item.summary)
                if item.url:
                    lines.append("   " + item.url)
            lines.append("")

        failed = [r for r in results if not r.ok]
        if failed:
            lines.append("⚠️ 本次不可用：" + "、".join(r.source_name for r in failed))
        if unknown:
            lines.append("❓ 未识别：" + "、".join(unknown))
        return "\n".join(lines).strip()

    def _cfg(self, key: str, default):
        try:
            return self._raw_config.get(key, default)
        except Exception:
            return default

    def _default_sources(self):
        raw = str(self._cfg("default_sources", "weibo,baidu,toutiao") or "")
        return resolve_many(_split_words(raw)) or list(ALL_SOURCES)[:3]

    def _default_limit(self) -> int:
        return max(1, min(self._as_int(self._cfg("default_limit", 5), 5), 20))

    @staticmethod
    def _as_int(value, fallback: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _as_float(value, fallback: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return fallback


def _split_words(raw: str) -> list[str]:
    """按空格、中英文逗号、顿号切词，顺带去掉包裹的引号。"""
    text = (raw or "").strip()
    for sep in ("，", "、", ",", "；", ";"):
        text = text.replace(sep, " ")
    words = []
    for token in text.split():
        token = token.strip().strip("\"'“”‘’")
        if token:
            words.append(token)
    return words


def _render_hits(hits: list[tuple[str, object]]) -> str:
    """一次最多推 10 条，多了会刷屏，剩下的留给下一轮或用户自己查。"""
    lines = [f"🎯 热点命中 · {datetime.now():%m-%d %H:%M}", ""]
    for shown, (source_name, item) in enumerate(hits):
        if shown >= 10:
            lines.append(f"…另有 {len(hits) - shown} 条命中，发 /热点 查看")
            break
        lines.append(f"【{source_name}】{getattr(item, 'title', '')}")
    lines.append("")
    lines.append("调整：/热点监控列表")
    return "\n".join(lines)
