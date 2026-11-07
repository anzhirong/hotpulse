"""hotpulse · AstrBot 插件

在飞书 / QQ / Telegram 等平台里直接查询各平台实时热榜，并支持关键词监控：
命中关注词的标题会主动推送到订阅过的会话。抓取逻辑完全内嵌，不需要额外起服务。
"""

import asyncio
import importlib.util
import re
import shutil
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
    """加载随插件分发的核心包。

    这里**不能**只在 sys.modules 里查一下就复用。AstrBot 升级插件是「先把新文件
    换上去，再重新 import 插件」的，进程里还留着上一版注册的
    astrbot_plugin_hotpulse_core；直接复用就会拿到旧对象，然后报
    「module ... has no attribute SubscriptionStore」（v1.0.0 的核心包里没有它）。

    所以每次加载先把整包从 sys.modules 里清掉，再从磁盘重新执行一遍。
    核心包只有几十 KB，这点开销远小于一次排查。
    """
    for name in [n for n in sys.modules if n == _CORE_PKG or n.startswith(_CORE_PKG + ".")]:
        del sys.modules[name]

    core_dir = Path(__file__).resolve().parent / "core"
    spec = importlib.util.spec_from_file_location(
        _CORE_PKG,
        core_dir / "__init__.py",
        submodule_search_locations=[str(core_dir)],
    )
    if spec is None or spec.loader is None:  # pragma: no cover
        raise ImportError(f"无法加载插件核心目录：{core_dir}")

    module = importlib.util.module_from_spec(spec)
    # 相对导入要求父包已经在 sys.modules 里，所以必须先注册再执行
    sys.modules[_CORE_PKG] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        # 执行到一半失败时把半成品撤掉，否则会毒化下一次加载
        sys.modules.pop(_CORE_PKG, None)
        raise
    return module


_core = _load_core()
HotPulseFetcher = _core.HotPulseFetcher
SubscriptionStore = _core.SubscriptionStore
match_title = _core.match_title
cluster_hits = _core.cluster_hits
cloud_compare = _core.compare
CLOUD_SPECS = _core.SPECS
DEFAULT_SPEC = _core.DEFAULT_SPEC
split_def = _core.split_def
variants = _core.variants
configure = _core.configure
set_logger = _core.set_logger
ALL_SOURCES = _core.ALL_SOURCES
resolve = _core.resolve
resolve_many = _core.resolve_many

# 命令前缀。带斜杠的必须排在前面，否则 "/热点 微博" 会被 "热点" 提前截断
_PREFIXES = ("/热点", "/热榜", "/hot", "热点", "热榜", "hot")

# 「热点监控」必须单独注册成一条指令，不能只当 /热点 的子命令：
# AstrBot 的 CommandFilter 判断的是 message_str.startswith("热点 ") 或完全相等，
# 「/热点监控 ai」里「热点」后面紧跟的是「监」而不是空格，匹配不上，消息会掉进
# 大模型（用户看到的就是「什么玩意」）。
# 别名覆盖不带空格的几种写法，否则 /热点监控列表 这类也会漏。
# 注意：别名去掉「热点」前缀后必须仍然以「监控」开头，否则下面的解析会错位。
_MONITOR_ALIASES = {
    "热榜监控",
    "热点监控列表",
    "热点监控删除",
    "热点监控清空",
    "热点监控关闭",
    "热点监控开启",
}

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
    "· /热点监控 AI     订阅关键词，新上榜才推送\n"
    "· /热点监控列表    看监控状态\n"
    "· /云价 2核4G      阿里云/腾讯云同规格比价"
)

# 这些不是摘要，只是平台的标记，显示出来反而误导
_NOISE_SUMMARY = {"新事件上榜", "热门", "热榜", "置顶", "刚刚", "热点"}

# AI 解读：只根据标题转述，不允许补事实。热榜接口没有正文，能拿到的只有标题，
# 所以这里定位是「把标题说人话」，不是「总结新闻」。每条会带 💭 前缀并在末尾声明。
_BRIEF_SYSTEM = (
    "给每条热点标题补一句中文说明：这条在说什么。"
    "只看标题，不要编数字、人名、时间、结论。"
    "每条 30 字以内，按原编号一行一条，格式「1. 说明」。"
)

# 「1. 说明」「1、说明」「1)说明」都收
_BRIEF_LINE = re.compile(r"^\s*(\d{1,2})\s*[.、)．]\s*(.+?)\s*$")

# 定时总结里的整体概括。同样只许依据标题，不许补事实。
_DIGEST_SYSTEM = (
    "下面是用户关注主题今天在热榜上的标题。用 2 到 3 句中文概括这批人都在聊什么，"
    "可以归类但不能编造标题里没有的事实、数字、结论。总长 80 字以内，直接给结论，不要客套。"
)


def _parse_briefs(text: str, count: int) -> list[str]:
    """把模型输出按编号对回标题。对不上、太短、太长的都丢掉，宁可空着。"""
    briefs = [""] * count
    for line in (text or "").splitlines():
        matched = _BRIEF_LINE.match(line)
        if not matched:
            continue
        index = int(matched.group(1)) - 1
        if not 0 <= index < count:
            continue
        body = matched.group(2).strip()
        # 模型有时会自己加「解读：」前缀
        for prefix in ("解读：", "解读:", "说明：", "说明:"):
            if body.startswith(prefix):
                body = body[len(prefix) :].strip()
        if len(body) < 6:
            # 太短的没信息量（「同上」「略」这种），宁可空着
            continue
        briefs[index] = body if len(body) <= 30 else body[:30] + "…"
    return briefs


def _fmt_hot(value) -> str:
    """热度数值统一成「万/亿」，没有数值就返回空串。"""
    if not isinstance(value, (int, float)) or value <= 0:
        return ""
    if value >= 1e8:
        return f"{value / 1e8:.1f}亿"
    if value >= 1e4:
        return f"{value / 1e4:.1f}万"
    return f"{int(value)}"


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
    return f"{_fmt_hot(value)}{unit}"


# ---- 数据目录 ----
# 关键词、去重记录、榜单快照都必须存在插件目录之外：从 WebUI「从文件安装」
# 会整目录替换插件文件夹，放在里面等于每次升级都被清空。
# AstrBot 的约定位置是 data/plugin_data/<插件名>/，不同版本取的 API 不一样，
# 逐个尝试，全都不行才退回插件目录（老版本没有这些 API 时的保底）。
_PLUGIN_NAME = "astrbot_plugin_hotpulse"


def _data_dir_from_api() -> Path:
    from astrbot.api.star import StarTools

    return Path(StarTools.get_data_dir(_PLUGIN_NAME))


def _data_dir_from_core() -> Path:
    from astrbot.core.star.star_tools import StarTools

    return Path(StarTools.get_data_dir(_PLUGIN_NAME))


def _data_dir_from_path() -> Path:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path

    return Path(get_astrbot_data_path()) / "plugin_data" / _PLUGIN_NAME


def _resolve_data_dir() -> Path:
    for getter in (_data_dir_from_api, _data_dir_from_core, _data_dir_from_path):
        try:
            path = getter()
        except Exception:  # 老版本没这个 API，或者签名不一样
            continue
        if path:
            return path
    return Path(__file__).resolve().parent / "data"


def _migrate_old_store(target: Path, old: Path | None = None) -> bool:
    """v1.2.0 及以前把订阅文件放在插件目录里，第一次用新目录时搬过去。

    返回是否真的搬了，方便日志和测试判断。
    """
    old = old or Path(__file__).resolve().parent / "data" / "subscriptions.json"
    if target.exists() or not old.exists() or old.resolve() == target.resolve():
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(old, target)
    except OSError as exc:
        logger.warning(f"hotpulse 迁移订阅数据失败，改用插件目录：{exc}")
        return False
    logger.info("hotpulse 已把订阅数据迁移到 %s", target.parent)
    return True


@register("hotpulse", "anzhirong", "多平台热点聚合查询", "1.7.0")
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
        self._digest_task: asyncio.Task | None = None

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
            aliyun_ak=str(self._cfg("aliyun_ak", "") or "").strip(),
            aliyun_sk=str(self._cfg("aliyun_sk", "") or "").strip(),
            tencent_secret_id=str(self._cfg("tencent_secret_id", "") or "").strip(),
            tencent_secret_key=str(self._cfg("tencent_secret_key", "") or "").strip(),
        )

        store_path = _resolve_data_dir() / "subscriptions.json"
        _migrate_old_store(store_path)
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
        self._digest_task = asyncio.create_task(self._digest_loop())
        logger.info("hotpulse 插件已加载（订阅关键词 %d 个）", len(self._store.keywords))

    async def terminate(self):
        """插件卸载/停用时停掉后台任务并释放连接池。"""
        for task in (self._monitor_task, self._digest_task, self._kick_task):
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

        # 兼容「/热点 监控 AI」这种带空格的老写法（下面 monitor 那条是主入口）
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

    @filter.command("热点监控", alias=_MONITOR_ALIASES)
    async def monitor(self, event: AstrMessageEvent):
        """订阅关键词，命中词新上榜就推送。用法：/热点监控 [关键词|列表|删除|清空|关闭|开启]"""
        async for reply in self._monitor_command(event, self._strip_prefix(event.message_str)):
            yield reply

    @filter.command("云价", alias={"云比价", "云服务器"})
    async def cloud_price(self, event: AstrMessageEvent):
        """查阿里云 / 腾讯云同规格服务器的官方价并对比。用法：/云价 [2核4G|4核8G]"""
        tokens = _split_words(self._strip_prefix(event.message_str))
        spec = next((t for t in tokens if t in CLOUD_SPECS), DEFAULT_SPEC)
        results, skipped = await cloud_compare(spec)
        yield event.plain_result(_render_cloud(results, spec, skipped))

    # 自然语言入口：用户说「我想看 AI 相关热点」时，由模型自己决定调这个工具，
    # 不需要记 /热点 搜 这种指令。参数 schema 是从下面的 docstring 解析出来的，
    # Args: 段格式写错会静默丢掉参数，所以 tests 里专门盯了这段。
    if hasattr(filter, "llm_tool"):  # 老版本没有这个装饰器，有就注册，没有就只保留指令

        @filter.llm_tool(name="hot_search")
        async def hot_search_tool(self, event: AstrMessageEvent, keyword: str) -> str:
            """查全网热榜（微博、百度、头条、B站、36氪）的实时条目，按主题筛选。

            只要用户表达了想看某类内容，就直接调用本工具，不要反问他具体想看什么：
            「我想看AI」「有没有芯片的消息」「最近有什么新能源的」「ai」都算。
            用户说得宽泛时按字面调用即可，结果为空也没关系，工具会告诉你搜过哪些同义词。

            Args:
                keyword(string): 主题关键词，如 AI、芯片、汽车；用户没说清就用原话里最接近的词
            """
            keywords = _split_words(keyword) or [keyword]
            try:
                results = await self.fetcher.fetch_many(list(ALL_SOURCES), 50)
            except Exception as exc:
                logger.error(f"hotpulse 工具抓取异常：{exc}")
                return "抓取失败，稍后再试。"

            pairs = [
                (result.source_name, item)
                for result in results
                if result.ok
                for item in result.items
                if match_title(item.title, keywords)
            ]
            if not pairs:
                failed = [r.source_name for r in results if not r.ok]
                hint = f"（不可用：{'、'.join(failed)}）" if failed else ""
                return (
                    f"当前全网热榜里没有和「{keyword}」相关的条目"
                    f"{_variants_hint(keywords)}{hint}。换个说法再试。"
                )

            # 每条都带平台和链接：热榜没有正文，链接是用户唯一能看下去的入口。
            # 之前只给「标题（平台）」，模型会顺手把括号删掉重排成纯列表，
            # 用户回「1」想看某条时，回复里没有任何可点的东西。
            clusters = cluster_hits(pairs)[:8]
            lines = [f"「{keyword}」当前在榜 {len(clusters)} 组："]
            for index, cluster in enumerate(clusters, 1):
                lines.append(f"{index}. {cluster.title}")
                meta = " · ".join(cluster.sources)
                url = next(
                    (getattr(i, "url", None) for i in cluster.items if getattr(i, "url", None)),
                    None,
                )
                lines.append(f"   {meta}")
                if url:
                    lines.append(f"   {url}")
            lines.append(
                "以上是系统实时抓取的原文，请原样发给用户：保留编号、平台和链接，"
                "不要改写标题、不要删链接、不要补充标题里没有的信息。"
                "用户之后说「第 N 条」「1」时，直接把对应那条的标题和链接给他，"
                "不要猜内容——系统拿不到正文。"
            )
            return "\n".join(lines)

    # 光靠工具描述不够：弱一点的模型会把「我想看ai」当闲聊反问「想看哪块」。
    # 这里在每次请求的 system prompt 末尾加一句硬提示，比改描述管用。
    if hasattr(filter, "on_llm_request"):

        @filter.on_llm_request()
        async def hint_hot_search_tool(self, event: AstrMessageEvent, req):
            hint = (
                "\n（系统：你有 hot_search 工具，可查微博/百度/头条/B站/36氪的实时热榜。"
                "用户说「我想看X」「有没有X的消息」「X 有什么热点」时必须直接调用它，"
                "不要反问他想看什么，也不要凭记忆编新闻。）"
            )
            try:
                req.system_prompt = (req.system_prompt or "") + hint
            except Exception as exc:  # 框架结构变了也不能把请求搞挂
                logger.debug(f"hotpulse 注入工具提示失败：{exc}")

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
                f"🔍 搜「{'、'.join(keywords)}」没有结果。{_variants_hint(keywords)}\n"
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

        # raw 可能是「监控 ai」（从 /热点监控 进来的），也可能是 /热点 那条
        # 路由转过来的同样形式；统一把开头的「监控」摘掉，剩下的才是子命令/关键词
        rest = raw[len("监控") :].strip() if raw.startswith("监控") else raw.strip()

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
        self._kick_task = asyncio.create_task(
            self._monitor_once(announce_session=self._event_session(event) or None)
        )

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

    async def _monitor_once(self, announce_session: str | None = None):
        """扫一轮，只推「这一轮刚上榜」的命中标题。

        announce_session 有值是「用户刚加完关键词」的场景：只推给他本人、
        跳过静默时段，并且在没有新上榜时先把他当前能看到的命中列出来，
        免得他以为插件没动静。
        """
        store = self._store
        if store is None or store.paused:
            return
        if not store.keywords:
            return

        targets = [announce_session] if announce_session else list(store.sessions)
        if not targets:
            return
        if not announce_session and self._in_quiet_hours():
            return

        # 断档多久就不再做「新上榜」判断：三个扫描周期，至少 3 小时
        max_age = self._monitor_interval() * 3 + 300
        results = await self.fetcher.fetch_many(list(ALL_SOURCES), 50)

        fresh: list[tuple[str, object]] = []
        current: list[tuple[str, object]] = []
        cold: list[str] = []
        for result in results:
            if not result.ok or not result.items:
                # 抓取失败的源保留旧快照：等它恢复时，还挂在榜上的不会被当成新的
                continue
            titles = [item.title for item in result.items if item.title]
            new_titles = store.diff_titles(result.source, titles, max_age=max_age)
            # 必须先比对、再落快照，顺序反了本轮就会被自己覆盖成「没有新上榜」
            store.remember_snapshot(result.source, titles)

            matched = [item for item in result.items if match_title(item.title, store.keywords)]
            if new_titles is None:
                cold.append(result.source_name)
                current.extend((result.source_name, item) for item in matched)
                continue
            wanted = set(new_titles)
            fresh.extend(
                (result.source_name, item)
                for item in matched
                if item.title in wanted and not store.is_seen(item.title)
            )

        # 快照先落盘：即使这一轮没命中，重启后也能接着比对而不是重新冷启动
        store.save()
        if cold:
            logger.info("hotpulse 建立榜单快照（%s），本轮不推送", "、".join(cold))

        if MessageChain is None:
            logger.warning("hotpulse: 当前 AstrBot 版本不支持主动发消息，监控不可用")
            return

        preview = announce_session is not None and not fresh
        hits = current if preview else fresh
        if not hits:
            return

        clusters = cluster_hits(hits)
        briefs = await self._ai_brief(clusters)
        text = _render_new(clusters, preview=preview, briefs=briefs)
        delivered = 0
        for session in targets:
            try:
                ok = await self.context.send_message(session, MessageChain().message(text))
                delivered += 1 if ok else 0
            except Exception as exc:
                logger.warning(f"hotpulse 推送到 {session} 失败：{exc}")

        # 无论是否送达都记为已见，避免推不出去时每轮重复尝试刷日志
        store.mark_seen([item.title for _, item in hits])
        store.save()
        logger.info(
            "hotpulse %s %d 条（%d 组），送达 %d 个会话",
            "当前在榜" if preview else "新上榜",
            len(hits),
            len(clusters),
            delivered,
        )

    async def _model_text(self, prompt: str, system_prompt: str) -> str:
        """调一次 AstrBot 当前使用的模型，失败一律返回空串。

        解读、日报都走这里，省得每处都写一遍 provider 获取和超时兜底。
        """
        try:
            provider = self.context.get_using_provider()
        except Exception as exc:
            logger.warning(f"hotpulse 获取模型失败：{exc}")
            return ""
        if provider is None:
            logger.info("hotpulse 未配置默认模型，跳过 AI 生成")
            return ""

        try:
            response = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, system_prompt=system_prompt),
                timeout=30,
            )
        except TypeError:
            # 老版本的 text_chat 不接 system_prompt
            try:
                response = await asyncio.wait_for(provider.text_chat(prompt), timeout=30)
            except Exception as exc:
                logger.warning(f"hotpulse AI 生成失败：{exc}")
                return ""
        except Exception as exc:
            logger.warning(f"hotpulse AI 生成失败：{exc}")
            return ""
        return getattr(response, "completion_text", "") or ""

    async def _ai_brief(self, clusters) -> list[str]:
        """用 AstrBot 当前使用的模型，给每条热榜补一句「这是什么」。

        前提说清楚：热榜接口只有标题，正文抓不到（36氪 详情页被火山引擎安全检测
        挡、头条要求执行 JS），所以这里做的是「把标题说人话」，并在提示词里
        禁止模型补数字、人名、结论。每条都会带 💭 前缀，消息末尾有声明。

        一次推送只调一次模型：10 条标题一起喂，2C2G 也扛得住。
        任何环节失败都返回空列表，推送照发，不能因为模型挂了就没消息。
        """
        if not self._cfg_bool("ai_brief", True):
            return []

        titles = [cluster.title for cluster in clusters[:10]]
        if not titles:
            return []

        numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(titles, 1))
        text = await self._model_text(numbered, _BRIEF_SYSTEM)
        briefs = _parse_briefs(text, len(titles))
        logger.info("hotpulse AI 解读 %d/%d 条", sum(1 for b in briefs if b), len(titles))
        return briefs

    # ---------- 定时总结 ----------

    def _digest_times(self) -> list[str]:
        """配置里的总结推送时间，形如 ["09:00", "21:00"]。"""
        return self._times_of("digest_time")

    async def _digest_loop(self):
        """每 30 秒看一眼是不是到了推送时间。

        不用 sleep 到下一个整点：容器时区、系统卡顿都会让长睡眠算错，
        短轮询加「这个时间点已发过」的记录，简单且不会漏。
        """
        try:
            while True:
                try:
                    await self._maybe_digest()
                    await self._maybe_cloud()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error(f"hotpulse 定时任务异常：{exc}")
                await asyncio.sleep(30)
        except asyncio.CancelledError:
            logger.info("hotpulse 定时循环已停止")
            raise

    def _times_of(self, key: str) -> list[str]:
        """读一个 HH:MM 列表配置。格式不对的丢掉并留日志，不让它拖垮整个定时任务。"""
        raw = str(self._cfg(key, "") or "")
        times: list[str] = []
        for token in re.split(r"[,，、\s]+", raw):
            token = token.strip()
            if not token:
                continue
            matched = re.fullmatch(r"(\d{1,2}):(\d{2})", token)
            if not matched:
                logger.warning("hotpulse %s 里的「%s」不是 HH:MM，已忽略", key, token)
                continue
            hour, minute = int(matched.group(1)), int(matched.group(2))
            if hour > 23 or minute > 59:
                logger.warning("hotpulse %s 里的「%s」超出范围，已忽略", key, token)
                continue
            slot = f"{hour:02d}:{minute:02d}"
            if slot not in times:
                times.append(slot)
        return times

    async def _maybe_cloud(self):
        """到点推一次云服务器比价。没有会话或没填密钥就不折腾。"""
        store = self._store
        if store is None or not store.sessions:
            return
        if not self._cfg_bool("cloud_enabled", False):
            return

        times = self._times_of("cloud_time")
        if not times:
            return
        now = datetime.now()
        slot = f"{now:%Y-%m-%d} {now:%H:%M}"
        key = f"cloud {slot}"
        if now.strftime("%H:%M") not in times or key in store.digest_sent:
            return

        spec = str(self._cfg("cloud_spec", DEFAULT_SPEC) or DEFAULT_SPEC)
        if spec not in CLOUD_SPECS:
            spec = DEFAULT_SPEC

        store.digest_sent.append(key)
        store.save()

        results, skipped = await cloud_compare(spec)
        if not results:
            logger.info("hotpulse %s 云比价：没填任何密钥，跳过", slot)
            return

        text = _render_cloud(results, spec, skipped)
        delivered = 0
        for session in list(store.sessions):
            try:
                ok = await self.context.send_message(session, MessageChain().message(text))
                delivered += 1 if ok else 0
            except Exception as exc:
                logger.warning(f"hotpulse 云比价推送到 {session} 失败：{exc}")
        logger.info("hotpulse %s 云比价（%s）送达 %d 个会话", slot, spec, delivered)

    async def _maybe_digest(self):
        store = self._store
        if store is None or store.paused or not store.keywords or not store.sessions:
            return
        if MessageChain is None:
            return

        times = self._digest_times()
        if not times:
            return

        now = datetime.now()
        slot = f"{now:%Y-%m-%d} {now:%H:%M}"
        if now.strftime("%H:%M") not in times or slot in store.digest_sent:
            return

        # 先记下来再抓取：抓取要几十秒，这期间循环可能再进来一次，会导致重复推
        store.digest_sent.append(slot)
        store.save()

        results = await self.fetcher.fetch_many(list(ALL_SOURCES), 50)
        pairs = [
            (result.source_name, item)
            for result in results
            if result.ok
            for item in result.items
            if match_title(item.title, store.keywords)
        ]
        if not pairs:
            logger.info("hotpulse %s 总结：当前没有命中项，跳过", slot)
            return

        limit = max(1, min(self._as_int(self._cfg("digest_limit", 8), 8), 15))
        clusters = cluster_hits(pairs)
        summary = ""
        if self._cfg_bool("digest_ai", True):
            titles = "\n".join(f"{i}. {c.title}" for i, c in enumerate(clusters[:15], 1))
            summary = _clean_summary(await self._model_text(titles, _DIGEST_SYSTEM))

        text = _render_digest(clusters, limit, summary, "、".join(self._digest_times()))
        delivered = 0
        for session in list(store.sessions):
            try:
                ok = await self.context.send_message(session, MessageChain().message(text))
                delivered += 1 if ok else 0
            except Exception as exc:
                logger.warning(f"hotpulse 总结推送到 {session} 失败：{exc}")
        logger.info("hotpulse %s 定时总结 %d 组，送达 %d 个会话", slot, len(clusters), delivered)

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
        # 有快照就说明扫过，把时间摆出来，用户不用翻日志也能确认在跑
        latest = max((float(s.get("at") or 0) for s in store.snapshots.values()), default=0.0)
        if latest:
            scanned = datetime.fromtimestamp(latest)
            lines.append(
                f"上次扫描：{scanned:%m-%d %H:%M}（已记录 {len(store.snapshots)} 个平台的榜单）"
            )
        quiet = self._quiet_range_text()
        if quiet:
            lines.append(f"静默时段：{quiet}")
        times = self._digest_times()
        if times:
            lines.append(f"定时总结：每天 {'、'.join(times)}")
            if store.digest_sent:
                lines.append(f"上次总结：{store.digest_sent[-1]}")
        else:
            lines.append("定时总结：未开启（配置 digest_time 留空）")
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
        lines.append("推送规则：只推本轮新上榜的；已经在榜上的不会重复提醒。")
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

    def _cfg_bool(self, key: str, default: bool) -> bool:
        # WebUI 里勾的是 bool，但从 yaml / 环境导入时可能是字符串
        value = self._cfg(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)

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


def _short(text, limit: int = 60) -> str | None:
    """把平台摘要压成一行：去换行、去装饰符号、太长截断。

    短于 8 字的不算摘要（头条的 LabelDesc 只有 5 个字，是标签不是内容）。
    """
    if not text:
        return None
    clean = " ".join(str(text).split()).strip("-—· ")
    if len(clean) < 8 or clean in _NOISE_SUMMARY:
        return None
    return clean if len(clean) <= limit else clean[:limit] + "…"


def _variants_hint(keywords: list[str]) -> str:
    """没有结果时把实际搜过的同义词列出来，否则用户以为插件没认真找。"""
    words: list[str] = []
    for keyword in keywords:
        for word in variants(keyword):
            if word and word not in words:
                words.append(word)
    if len(words) <= len(keywords):
        return ""
    return "（已按同义词组展开：" + "、".join(words[:8]) + "）"


def _clean_summary(text: str, limit: int = 120) -> str:
    """模型写的整体概括：压成一段，去掉常见客套开头，太短当没写。"""
    clean = " ".join((text or "").split())
    # 「以下是总结：」这种要连剥两层，所以循环而不是只剥一次
    changed = True
    while changed and clean:
        changed = False
        for prefix in ("以下是", "总结：", "总结:", "概括：", "概括:", "日报：", "日报:"):
            if clean.startswith(prefix):
                clean = clean[len(prefix) :].lstrip("：:，, ")
                changed = True
                break
    if len(clean) < 8:
        return ""
    return clean if len(clean) <= limit else clean[:limit] + "…"


def _render_digest(clusters, limit: int, summary: str = "", times: str = "") -> str:
    """定时总结：先给一段整体概括，再列条目。和即时推送的区别是有「今天都在聊什么」。"""
    lines = [f"📊 热点总结 · {datetime.now():%m-%d %H:%M}", ""]
    if summary:
        lines.append(f"💭 {summary}")
        lines.append("")

    for index, cluster in enumerate(clusters[:limit], 1):
        lines.append(f"{index}. {cluster.title}")
        lead = min(cluster.items, key=lambda i: getattr(i, "rank", 99) or 99)
        meta = " · ".join(x for x in (" · ".join(cluster.sources), _hot_text(lead)) if x)
        if meta:
            lines.append(f"   {meta}")
        url = next(
            (getattr(i, "url", None) for i in cluster.items if getattr(i, "url", None)), None
        )
        if url:
            lines.append(f"   {url}")

    if len(clusters) > limit:
        lines.append(f"…另有 {len(clusters) - limit} 组，发 /热点 搜 关键词 看全榜")

    lines.append("")
    when = f"每天 {times} 推一次" if times else "定时推送"
    lines.append(f"· {when}，可在插件配置的 digest_time 调整")
    if summary:
        lines.append("· 💭 = AI 根据标题概括，不是原文")
    return "\n".join(lines)


def _render_cloud(results, spec: str, skipped: str = "") -> str:
    """比价结果。核心是最后那行差额，前面都是为它做铺垫。"""
    ok = [r for r in results if r.ok and r.quote]
    lines = [f"💰 云服务器比价 · {spec} 包年", ""]

    if not ok:
        lines.append("这次两家都没查成功：")
        for result in results:
            lines.append(f"· {result.vendor}：{result.error}")
        if skipped:
            lines.append(f"· 未参与：{skipped}")
        lines.append("")
        lines.append("常见原因：密钥没填 / 权限不足 / 该地域不卖这个型号。")
        return "\n".join(lines)

    for result in ok:
        quote = result.quote
        line = f"{quote.vendor}  {quote.price:,.2f} 元/{quote.period}"
        if quote.discount:
            line += f"（原价 {quote.list_price:,.2f}，省 {quote.discount:.0%}）"
        lines.append(line)

    for result in results:
        if not result.ok:
            lines.append(f"{result.vendor}  查不到：{result.error}")

    if len(ok) == 2:
        left, right = ok[0].quote, ok[1].quote
        cheap, dear = (left, right) if left.price <= right.price else (right, left)
        diff = dear.price - cheap.price
        pct = diff / dear.price * 100 if dear.price else 0
        lines.append("")
        if diff < 0.01:
            lines.append("→ 两家一个价（差不到 1 分）")
        else:
            lines.append(f"→ {cheap.vendor}便宜 {diff:,.2f} 元（{pct:.1f}%）")

    if skipped:
        lines.append("")
        lines.append(f"未参与：{skipped}")
    lines.append("")
    lines.append("口径：官方目录价 + 官方折扣，不含限时活动价、代金券、学生优惠。")
    lines.append("活动价以官网活动页和下单页为准，这里只做同规格横向对比。")
    specs = "、".join(CLOUD_SPECS)
    lines.append(f"· 换规格：/云价 {specs}")
    return "\n".join(lines)


def _render_new(clusters, preview: bool = False, briefs: list[str] | None = None) -> str:
    """监控推送的正文：同一件事跨平台合并成一条，最多 10 条。

    preview 是「刚加完关键词、还没有新上榜」时的一次性预览，用词要说清楚
    这些是榜上已有的，否则用户会误以为是刚冒出来的新闻。
    """
    title = "📋 当前在榜" if preview else "🎯 新上榜"
    lines = [f"{title} · {datetime.now():%m-%d %H:%M}", ""]
    briefs = briefs or []
    used_brief = False

    for index, cluster in enumerate(clusters):
        if index >= 10:
            lines.append(f"…另有 {len(clusters) - index} 条")
            break
        lines.append(f"{index + 1}. {cluster.title}")
        # 热度取榜内名次最好的那条，顺便带出单位（播放/阅读/热度），
        # 否则「8808」这种数字看不出是什么
        lead = min(cluster.items, key=lambda i: getattr(i, "rank", 99) or 99)
        meta = " · ".join(x for x in (" · ".join(cluster.sources), _hot_text(lead)) if x)
        if meta:
            lines.append(f"   {meta}")
        # 平台自带摘要（目前只有 B站视频简介有真内容）优先，那是原文；
        # 没有才用 AI 解读，两行不重复占位
        summary = _short(getattr(lead, "summary", None))
        if summary:
            lines.append(f"   {summary}")
        elif index < len(briefs) and briefs[index]:
            lines.append(f"   💭 {briefs[index]}")
            used_brief = True
        # 链接一律给：热榜接口没有正文，用户只能点进去看，省掉这条消息就真的空了
        url = next(
            (getattr(i, "url", None) for i in cluster.items if getattr(i, "url", None)), None
        )
        if url:
            lines.append(f"   {url}")

    lines.append("")
    if preview:
        lines.append("以上是现在榜上已有的，之后只推新冒出来的。")
    lines.append("· /热点 搜 关键词 看全榜 · /热点监控列表 改关注词")
    if used_brief:
        # 必须写明白：这是模型看着标题说的话，不是新闻原文
        lines.append("· 💭 = AI 根据标题推测，不是原文，细节点链接核对")
    return "\n".join(lines)
