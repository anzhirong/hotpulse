"""监控推送的回归测试：重点验证「只推本轮新上榜」。

AstrBot 不在开发机上跑，所以这里用最小的假模块顶替 astrbot.*，
然后按真实路径加载 main.py —— 测的是插件里那份代码，不是复刻一份逻辑。
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import sys
import time
import types
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "astrbot_plugin_hotpulse"


# ---------- 伪造 AstrBot 环境 ----------


class FakeChain:
    def __init__(self) -> None:
        self.text = ""

    def message(self, text: str) -> FakeChain:
        self.text = text
        return self


class FakeStar:
    def __init__(self, context) -> None:
        self.context = context


def _stub_astrbot() -> None:
    if "astrbot" in sys.modules:
        return

    logger = types.SimpleNamespace(
        debug=lambda *a, **k: None,
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logger
    event = types.ModuleType("astrbot.api.event")

    class AstrMessageEvent:
        pass

    event.AstrMessageEvent = AstrMessageEvent
    event.filter = types.SimpleNamespace(
        command=lambda *a, **k: lambda fn: fn,
        llm_tool=lambda *a, **k: lambda fn: fn,
        on_llm_request=lambda *a, **k: lambda fn: fn,
    )
    star = types.ModuleType("astrbot.api.star")

    class Context:  # pragma: no cover - 占位
        pass

    star.Context = Context
    star.Star = FakeStar
    star.register = lambda *a, **k: lambda cls: cls

    core = types.ModuleType("astrbot.core")
    message = types.ModuleType("astrbot.core.message")
    result = types.ModuleType("astrbot.core.message.message_event_result")
    result.MessageChain = FakeChain
    message.message_event_result = result

    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.star": star,
            "astrbot.core": core,
            "astrbot.core.message": message,
            "astrbot.core.message.message_event_result": result,
        }
    )
    astrbot.api = api
    astrbot.core = core


def load_plugin_module():
    """按文件路径加载插件 main.py（AstrBot 也是这么加载的）。"""
    _stub_astrbot()
    if "hp_main" in sys.modules:
        return sys.modules["hp_main"]
    spec = importlib.util.spec_from_file_location("hp_main", PLUGIN_DIR / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["hp_main"] = module
    spec.loader.exec_module(module)
    return module


main = load_plugin_module()
core = sys.modules["astrbot_plugin_hotpulse_core"]
HotItem, SourceResult = core.HotItem, core.SourceResult
SubscriptionStore = core.SubscriptionStore


# ---------- 测试替身 ----------


class FakeFetcher:
    """按 {源: (显示名, 标题列表)} 返回，可以标记某个源失败。"""

    def __init__(self, mapping: dict[str, tuple[str, list[str]]], failing: set[str] | None = None):
        self.mapping = mapping
        self.failing = failing or set()

    async def fetch_many(self, sources, limit):
        out = []
        for key, (name, titles) in self.mapping.items():
            if key in self.failing:
                out.append(SourceResult(source=key, source_name=name, ok=False, error="上游超时"))
                continue
            items = [
                HotItem(rank=i + 1, title=t, url=f"https://example.com/{i}")
                for i, t in enumerate(titles)
            ]
            out.append(SourceResult(source=key, source_name=name, ok=True, items=items))
        return out


class FakeContext:
    def __init__(self, provider=None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.provider = provider

    async def send_message(self, session, chain) -> bool:
        self.sent.append((session, chain.text))
        return True

    def get_using_provider(self):
        return self.provider


class FakeProvider:
    """假的 AstrBot 模型提供方，记录被调了几次。"""

    def __init__(self, text: str = "", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls = 0
        self.prompts: list[str] = []

    async def text_chat(self, prompt=None, system_prompt=None):
        self.calls += 1
        self.prompts.append(prompt or "")
        if self.error is not None:
            raise self.error
        return types.SimpleNamespace(completion_text=self.text)


def make_plugin(
    tmp_path: Path,
    keywords: list[str],
    sessions: list[str] | None = None,
    provider=None,
    config: dict | None = None,
):
    store_path = tmp_path / "subscriptions.json"
    store = SubscriptionStore(
        path=store_path, keywords=list(keywords), sessions=list(sessions or [])
    )
    base = {
        # quiet_start == quiet_end 表示不静默，测试不受跑测试的时刻影响
        "quiet_start": 0,
        "quiet_end": 0,
        "monitor_interval": 1800,
    }
    base.update(config or {})
    plugin = main.HotPulsePlugin(FakeContext(provider), config=base)
    plugin._store = store
    return plugin


def run(coro):
    return asyncio.run(coro)


# ---------- 用例 ----------


def test_cold_start_records_snapshot_without_pushing(tmp_path):
    plugin = make_plugin(tmp_path, ["AI"], ["lark:chat1"])
    plugin.fetcher = FakeFetcher({"weibo": ("微博", ["AI 大模型发布", "某地地震"])})

    run(plugin._monitor_once())

    # 第一次跑没有可比对象，整张榜都不算「新上榜」
    assert plugin.context.sent == []
    assert plugin._store.snapshots["weibo"]["titles"] == ["AI 大模型发布", "某地地震"]


def test_only_new_arrival_pushed(tmp_path):
    plugin = make_plugin(tmp_path, ["AI"], ["lark:chat1"])
    plugin.fetcher = FakeFetcher({"weibo": ("微博", ["AI 大模型发布", "某地地震"])})
    run(plugin._monitor_once())  # 冷启动，只建快照

    # 第二轮：AI 那条还在榜上，新冒出「AI 手机来了」，地震掉榜
    plugin.fetcher = FakeFetcher({"weibo": ("微博", ["AI 大模型发布", "AI 手机来了"])})
    run(plugin._monitor_once())

    assert len(plugin.context.sent) == 1
    text = plugin.context.sent[0][1]
    assert "AI 手机来了" in text
    assert "AI 大模型发布" not in text  # 上一轮就在榜，不该重复推
    assert "新上榜" in text


def test_seen_title_not_pushed_again(tmp_path):
    plugin = make_plugin(tmp_path, ["AI"], ["lark:chat1"])
    plugin.fetcher = FakeFetcher({"weibo": ("微博", ["AI 一号"])})
    run(plugin._monitor_once())

    plugin.fetcher = FakeFetcher({"weibo": ("微博", ["AI 一号", "AI 二号"])})
    run(plugin._monitor_once())
    assert len(plugin.context.sent) == 1

    # 三号挤出二号之前，先把二号标成已推送；模拟「推过了又掉榜再回来」
    plugin._store.mark_seen(["AI 三号"])
    plugin.fetcher = FakeFetcher({"weibo": ("微博", ["AI 一号", "AI 二号", "AI 三号"])})
    run(plugin._monitor_once())
    assert len(plugin.context.sent) == 1  # 没有新增推送


def test_stale_snapshot_does_not_flood(tmp_path):
    plugin = make_plugin(tmp_path, ["AI"], ["lark:chat1"])
    plugin.fetcher = FakeFetcher({"weibo": ("微博", ["AI 一号"])})
    run(plugin._monitor_once())

    # 手动把快照时间推回两小时前（超过 3 个周期的容忍窗口）
    plugin._store.snapshots["weibo"]["at"] = time.time() - 2 * 3600
    plugin.fetcher = FakeFetcher({"weibo": ("微博", ["AI 九号", "AI 十号"])})
    run(plugin._monitor_once())

    assert plugin.context.sent == []  # 断档后只重建快照，不当成新上榜
    assert plugin._store.snapshots["weibo"]["titles"] == ["AI 九号", "AI 十号"]


def test_failed_source_keeps_old_snapshot(tmp_path):
    plugin = make_plugin(tmp_path, ["AI"], ["lark:chat1"])
    plugin.fetcher = FakeFetcher({"weibo": ("微博", ["AI 一号", "AI 二号"])})
    run(plugin._monitor_once())

    plugin.fetcher = FakeFetcher({"weibo": ("微博", ["AI 九号"])}, failing={"weibo"})
    run(plugin._monitor_once())

    assert plugin._store.snapshots["weibo"]["titles"] == ["AI 一号", "AI 二号"]
    assert plugin.context.sent == []


def test_announce_shows_current_list_when_nothing_new(tmp_path):
    # 用户刚加完关键词：还没有新上榜，也要把当前在榜的列出来，否则像没生效
    plugin = make_plugin(tmp_path, ["AI"], ["lark:chat1"])
    plugin.fetcher = FakeFetcher({"weibo": ("微博", ["AI 大模型发布"])})

    run(plugin._monitor_once(announce_session="lark:chat1"))

    assert len(plugin.context.sent) == 1
    session, text = plugin.context.sent[0]
    assert session == "lark:chat1"
    assert "当前在榜" in text
    assert "AI 大模型发布" in text


def test_announce_ignores_quiet_hours(tmp_path):
    plugin = make_plugin(tmp_path, ["AI"], ["lark:chat1"])
    plugin._raw_config = {"quiet_start": 0, "quiet_end": 24}  # 全天静默
    plugin.fetcher = FakeFetcher({"weibo": ("微博", ["AI 一号"])})

    run(plugin._monitor_once())  # 定时轮：静默期内什么都不做
    assert plugin.context.sent == []

    run(plugin._monitor_once(announce_session="lark:chat1"))  # 用户主动触发：照推
    assert len(plugin.context.sent) == 1


def test_push_goes_to_all_sessions_but_announce_only_to_one(tmp_path):
    plugin = make_plugin(tmp_path, ["AI"], ["lark:a", "lark:b"])
    plugin.fetcher = FakeFetcher({"weibo": ("微博", ["AI 一号"])})
    run(plugin._monitor_once())

    plugin.fetcher = FakeFetcher({"weibo": ("微博", ["AI 二号"])})
    run(plugin._monitor_once())
    assert [s for s, _ in plugin.context.sent] == ["lark:a", "lark:b"]

    plugin.context.sent.clear()
    plugin.fetcher = FakeFetcher({"weibo": ("微博", ["AI 三号"])})
    run(plugin._monitor_once(announce_session="lark:a"))
    assert [s for s, _ in plugin.context.sent] == ["lark:a"]


def test_snapshot_persists_across_reload(tmp_path):
    path = tmp_path / "subscriptions.json"
    store = SubscriptionStore(path=path, keywords=["AI"], sessions=["lark:a"])
    store.remember_snapshot("weibo", ["标题一", "标题二"])
    store.mark_seen(["标题一"])
    store.save()

    again = SubscriptionStore.load(path)
    assert again.snapshots["weibo"]["titles"] == ["标题一", "标题二"]
    assert again.diff_titles("weibo", ["标题一", "标题二", "标题三"]) == ["标题三"]
    assert again.diff_titles("weibo", ["标题一"]) == []

    # 文件内容是明文 json，方便出问题时直接看
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["snapshots"]["weibo"]["titles"] == ["标题一", "标题二"]


def test_corrupt_file_falls_back_to_empty(tmp_path):
    path = tmp_path / "subscriptions.json"
    path.write_text("{ 这不是 json", encoding="utf-8")
    store = SubscriptionStore.load(path)
    assert store.keywords == [] and store.snapshots == {}


def test_render_merges_same_event_across_sources(tmp_path):
    plugin = make_plugin(tmp_path, ["AI"], ["lark:a"])
    plugin.fetcher = FakeFetcher(
        {
            "weibo": ("微博", ["某公司发布新一代AI大模型"]),
            "baidu": ("百度热搜", ["某公司发布新一代AI大模型 参数翻倍"]),
        }
    )
    run(plugin._monitor_once(announce_session="lark:a"))

    text = plugin.context.sent[0][1]
    assert "微博 · 百度热搜" in text  # 同一件事合并成一条
    assert text.count("某公司发布新一代AI大模型") == 1


# ---------- 数据目录 ----------


def test_data_dir_falls_back_to_plugin_dir_without_astrbot_api():
    # 测试里的假 astrbot 没有 StarTools，此时应退回插件目录而不是抛异常
    path = main._resolve_data_dir()
    assert path == PLUGIN_DIR / "data"


def test_data_dir_prefers_astrbot_plugin_data(tmp_path, monkeypatch):
    # AstrBot 提供 StarTools 时应落到 data/plugin_data/<插件名>，插件目录之外
    class FakeStarTools:
        @staticmethod
        def get_data_dir(name: str):
            assert name == "astrbot_plugin_hotpulse"
            return tmp_path / "plugin_data" / name

    monkeypatch.setattr(sys.modules["astrbot.api.star"], "StarTools", FakeStarTools, raising=False)
    assert main._resolve_data_dir() == tmp_path / "plugin_data" / "astrbot_plugin_hotpulse"

    # 签名不对（老版本不接受参数）时不能炸，要继续往下找
    class BadStarTools:
        @staticmethod
        def get_data_dir():
            return tmp_path / "wrong"

    monkeypatch.setattr(sys.modules["astrbot.api.star"], "StarTools", BadStarTools, raising=False)
    assert main._resolve_data_dir() == PLUGIN_DIR / "data"


def test_migrate_old_store_keeps_existing_target(tmp_path):
    old = tmp_path / "old.json"
    old.write_text('{"keywords": ["旧"]}', encoding="utf-8")
    target = tmp_path / "new" / "subscriptions.json"
    target.parent.mkdir()
    target.write_text('{"keywords": ["新"]}', encoding="utf-8")

    assert main._migrate_old_store(target, old) is False
    assert "新" in target.read_text(encoding="utf-8")  # 不覆盖新数据


def test_migrate_old_store_copies_when_target_missing(tmp_path):
    old = tmp_path / "old.json"
    old.write_text('{"keywords": ["AI"]}', encoding="utf-8")
    target = tmp_path / "plugin_data" / "subscriptions.json"

    assert main._migrate_old_store(target, old) is True
    assert "AI" in target.read_text(encoding="utf-8")
    assert old.exists()  # 老文件留着，万一新目录后面出问题还能找回来


# ---------- 升级时重新加载 core ----------


def test_stale_core_module_does_not_break_reload():
    """线上真实踩到的坑：AstrBot 换完文件重新 import 插件时，sys.modules 里
    还留着上一版注册的 core 包（没有 SubscriptionStore），旧代码直接复用，
    报 "module 'astrbot_plugin_hotpulse_core' has no attribute SubscriptionStore"。
    """
    sys.modules["astrbot_plugin_hotpulse_core"] = types.ModuleType("astrbot_plugin_hotpulse_core")
    sys.modules["astrbot_plugin_hotpulse_core.fetcher"] = types.ModuleType("stale.fetcher")

    spec = importlib.util.spec_from_file_location("hp_main_reload", PLUGIN_DIR / "main.py")
    reloaded = importlib.util.module_from_spec(spec)
    sys.modules["hp_main_reload"] = reloaded
    spec.loader.exec_module(reloaded)

    fresh = sys.modules["astrbot_plugin_hotpulse_core"]
    for name in ("SubscriptionStore", "HotPulseFetcher", "cluster_hits", "match_title"):
        assert hasattr(fresh, name), name
    assert "astrbot_plugin_hotpulse_core.fetcher" in sys.modules  # 子模块也跟着重建了
    assert fresh.SubscriptionStore is not None


def test_half_built_core_is_removed_from_sys_modules(tmp_path, monkeypatch):
    """core 执行到一半抛异常时，半成品不能留在 sys.modules 里毒化下次加载。"""
    bad_dir = tmp_path / "core"
    bad_dir.mkdir()
    (bad_dir / "__init__.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")

    real_file = main.__file__
    monkeypatch.setattr(main, "__file__", str(tmp_path / "main.py"))
    sys.modules.pop("astrbot_plugin_hotpulse_core", None)
    try:
        with pytest.raises(RuntimeError):
            main._load_core()
        assert "astrbot_plugin_hotpulse_core" not in sys.modules
    finally:
        monkeypatch.setattr(main, "__file__", real_file)
        main._load_core()  # 恢复成真的 core，后面的用例还要用


# ---------- 指令解析 ----------
# 线上踩过的坑：/热点监控 ai 匹配不到 @filter.command("热点")，消息掉进大模型。
# AstrBot 只认「命令名 + 空格」或「整句等于命令名」，所以这里逐个盯住能走通的写法。


class FakeEvent:
    def __init__(self, message_str: str, session: str = "lark:test") -> None:
        self.message_str = message_str
        self.unified_msg_origin = session

    def plain_result(self, text: str) -> str:
        return text


def drive_monitor(plugin, monkeypatch, message_str: str) -> list[str]:
    """照抄 monitor 处理器的那两行：剥前缀 -> 交给 _monitor_command。"""

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(plugin, "_monitor_once", _noop)  # 别真去抓上游

    async def collect():
        raw = plugin._strip_prefix(message_str)
        return [reply async for reply in plugin._monitor_command(FakeEvent(message_str), raw)]

    return run(collect())


@pytest.mark.parametrize("message_str", ["/热点监控 AI", "热点监控 AI"])
def test_monitor_add_keyword_forms(tmp_path, monkeypatch, message_str):
    plugin = make_plugin(tmp_path, ["旧词"], ["lark:test"])
    replies = drive_monitor(plugin, monkeypatch, message_str)
    assert replies and "已开始监控" in replies[-1]
    assert "AI" in plugin._store.keywords


def test_all_documented_forms_pass_astrbot_command_matching():
    """复刻 CommandFilter 的判定：整句相等，或者「命令名 + 空格」开头。

    上面的用例测的是「进到处理器之后」的解析；这条测「能不能进到处理器」，
    漏一个写法就是用户发出去没人理、被大模型接走。
    """
    monitor_names = {"热点监控"} | main._MONITOR_ALIASES
    cases = [
        "热点监控 AI",  # 加词
        "热点监控列表",
        "热点监控删除 AI",
        "热点监控清空",
        "热点监控关闭",
        "热点监控开启",
    ]
    for msg in cases:
        assert any(msg == n or msg.startswith(n + " ") for n in monitor_names), msg

    # 查询类不能被监控别名抢走
    for msg in ["热点", "热点 微博 10", "热点 搜 AI", "热点源"]:
        assert not any(msg == n or msg.startswith(n + " ") for n in monitor_names), msg


@pytest.mark.parametrize(
    ("message_str", "expect"),
    [
        ("/热点监控列表", "关键词监控"),
        ("热点监控列表", "关键词监控"),
        ("/热点监控删除 旧词", "已删除"),
        ("/热点监控清空", "已清空"),
        ("/热点监控关闭", "已暂停"),
        ("/热点监控开启", "已恢复"),
    ],
)
def test_monitor_subcommand_forms(tmp_path, monkeypatch, message_str, expect):
    plugin = make_plugin(tmp_path, ["旧词"], ["lark:test"])
    replies = drive_monitor(plugin, monkeypatch, message_str)
    assert replies and expect in replies[-1]


def test_monitor_still_works_as_slash_hot_subcommand(tmp_path, monkeypatch):
    # 老写法「/热点 监控 AI」保留可用
    plugin = make_plugin(tmp_path, ["旧词"], ["lark:test"])
    assert plugin._strip_prefix("/热点 监控 AI") == "监控 AI"

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(plugin, "_monitor_once", _noop)

    async def collect():
        return [r async for r in plugin._monitor_command(FakeEvent("x"), "监控 AI")]

    assert "已开始监控" in run(collect())[-1]
    assert "AI" in plugin._store.keywords


def test_monitor_aliases_all_survive_prefix_strip():
    """别名去掉「热点」前缀后必须仍以「监控」开头，否则 rest 会错位。"""
    plugin = make_plugin(Path(".") / "unused", [], [])
    for alias in main._MONITOR_ALIASES:
        raw = plugin._strip_prefix("/" + alias)
        assert raw.startswith("监控"), f"{alias} -> {raw}"


# ---------- 排序 ----------


def test_cluster_sort_uses_rank_not_raw_hot():
    """36氪 的 hot 是阅读数（几千），B站是播放量（几百万），直接比 hot 排序
    会让 36氪 的榜首永远垫底。名次才是跨平台可比的。"""
    clusters = core.cluster_hits(
        [
            ("36氪热榜", HotItem(rank=1, title="丙 某个行业观察", hot=9000)),
            ("B站热门", HotItem(rank=9, title="丁 某个视频标题", hot=4_000_000)),
        ]
    )
    assert [c.title for c in clusters] == ["丙 某个行业观察", "丁 某个视频标题"]


def test_cluster_sort_cross_source_still_wins():
    """跨平台条数优先于名次：三个平台都上的事，比单平台第 1 名更值得排在前面。"""
    clusters = core.cluster_hits(
        [
            ("微博热搜", HotItem(rank=1, title="某公司发布新一代AI大模型")),
            ("百度热搜", HotItem(rank=6, title="某公司发布新一代AI大模型 参数翻倍")),
            ("B站热门", HotItem(rank=1, title="完全无关的另一个视频")),
        ]
    )
    assert len(clusters[0].sources) == 2
    assert clusters[0].rank == 1


# ---------- 推送正文 ----------


def test_push_keeps_links_and_platform_summary():
    """热榜接口没有正文，链接是唯一的入口；平台自带摘要（B站简介）要放出来，
    太短的不算摘要（头条 LabelDesc 只有 5 个字，是标签）。"""
    titles = [
        "苹果发布新芯片",
        "某地发布暴雨预警",
        "国产电影票房破纪录",
        "高校开学时间定了",
        "新能源车补贴调整",
        "演唱会门票秒空",
        "城市地铁新线路开通",
        "外卖平台配送费涨价",
    ]
    items = [
        HotItem(rank=i + 1, title=t, url=f"https://example.com/{i}") for i, t in enumerate(titles)
    ]
    items[0].summary = (
        "这是一段足够长的视频简介，用来验证摘要会不会被截断到六十个字以内，"
        "后面还有很多内容需要被切掉，否则整条消息会被摘要撑得很长很长"
    )
    items[1].summary = "五个字"
    clusters = core.cluster_hits([("微博热搜", item) for item in items])
    assert len(clusters) == 8  # 标题互不相同，不该被合并

    text = main._render_new(clusters)
    assert "https://example.com/0" in text
    assert "https://example.com/7" in text  # 超过 5 组也要给链接
    assert "…" in text  # 长摘要被截断
    assert "五个字" not in text  # 太短的不当摘要

    # 预览与正式推送只有抬头不同
    assert "当前在榜" in main._render_new(clusters, preview=True)
    assert "新上榜" in text


# ---------- AI 一句话解读 ----------


def test_parse_briefs_aligns_by_number():
    text = """1. 讲的是某地暴雨预警，出行要看天气
2、模型自己用了顿号
3) 这条太短
7. 编号越界，应该被丢掉
4. 解读：某某公司发布了新芯片，主打能效
"""
    briefs = main._parse_briefs(text, 4)
    assert briefs[0].startswith("讲的是某地暴雨")
    assert briefs[1].startswith("模型自己用了顿号")
    assert briefs[2] == ""  # 太短不当解读
    assert briefs[3].startswith("某某公司发布了新芯片")  # 前缀被去掉


def test_parse_briefs_truncates_long_line():
    briefs = main._parse_briefs("1. " + "很" * 200, 1)
    assert len(briefs[0]) == 31  # 30 字 + 省略号
    assert briefs[0].endswith("…")


def test_ai_brief_appears_in_push(tmp_path):
    provider = FakeProvider("1. 说有骗子用 AI 批量做短视频引流\n2. 另一条新闻的简单说明")
    plugin = make_plugin(tmp_path, ["AI"], ["lark:a"], provider=provider)
    plugin.fetcher = FakeFetcher({"weibo": ("微博", ["AI 视频引流", "AI 另一条消息"])})

    run(plugin._monitor_once(announce_session="lark:a"))

    text = plugin.context.sent[0][1]
    assert "💭 说有骗子用 AI 批量做短视频引流" in text
    assert "AI 根据标题推测" in text  # 必须声明不是原文
    assert provider.calls == 1  # 10 条标题只调一次模型


def test_ai_brief_failure_does_not_block_push(tmp_path):
    provider = FakeProvider(error=RuntimeError("401 模型未授权"))
    plugin = make_plugin(tmp_path, ["AI"], ["lark:a"], provider=provider)
    plugin.fetcher = FakeFetcher({"weibo": ("微博", ["AI 视频引流"])})

    run(plugin._monitor_once(announce_session="lark:a"))

    assert plugin.context.sent  # 模型挂了，推送照发
    assert "💭" not in plugin.context.sent[0][1]


def test_ai_brief_can_be_disabled(tmp_path):
    provider = FakeProvider("1. 不该被用到的解读")
    plugin = make_plugin(
        tmp_path, ["AI"], ["lark:a"], provider=provider, config={"ai_brief": False}
    )
    plugin.fetcher = FakeFetcher({"weibo": ("微博", ["AI 视频引流"])})

    run(plugin._monitor_once(announce_session="lark:a"))

    assert provider.calls == 0
    assert "💭" not in plugin.context.sent[0][1]


def test_platform_summary_wins_over_ai_brief():
    """B站简介是真原文，有它就别再叠一行 AI 推测。"""
    clusters = core.cluster_hits(
        [
            (
                "B站热门",
                HotItem(
                    rank=1,
                    title="AI 视频引流",
                    summary="这是视频作者自己写的简介，属于原文内容不是推测",
                ),
            )
        ]
    )
    text = main._render_new(clusters, briefs=["模型对这条的推测"])
    assert "这是视频作者自己写的简介" in text
    assert "💭" not in text


# ---------- 自然语言工具 ----------


def test_llm_tool_docstring_is_parsable():
    """AstrBot 从 docstring 的 Args: 段生成参数 schema，格式错了参数会被静默丢弃。"""
    doc = inspect.getdoc(main.HotPulsePlugin.hot_search_tool) or ""
    assert "Args:" in doc
    assert "keyword(string):" in doc


def test_llm_tool_returns_filtered_titles(tmp_path):
    plugin = make_plugin(tmp_path, [], [])
    plugin.fetcher = FakeFetcher(
        {
            "weibo": ("微博", ["AI 视频引流", "某地发布暴雨预警"]),
            "baidu": ("百度热搜", ["AI 手机发布"]),
        }
    )

    out = run(plugin.hot_search_tool(FakeEvent("我想看 AI"), "AI"))

    assert "AI 视频引流" in out
    assert "AI 手机发布" in out
    assert "某地发布暴雨预警" not in out
    assert "https://example.com/0" in out  # 链接必须给，热榜没有正文
    assert "微博" in out  # 平台不能丢
    assert "不要改写" in out  # 提示模型原样转述，别自己发挥


def test_llm_tool_reports_empty_result(tmp_path):
    plugin = make_plugin(tmp_path, [], [])
    plugin.fetcher = FakeFetcher({"weibo": ("微博", ["某地发布暴雨预警"])})

    out = run(plugin.hot_search_tool(FakeEvent("我想看火箭"), "火箭发射"))

    assert "没有" in out


def test_empty_result_shows_synonym_expansion(tmp_path):
    """说「芯片」没结果时，要告诉用户实际搜的是哪一组词，否则像没干活。"""
    plugin = make_plugin(tmp_path, [], [])
    plugin.fetcher = FakeFetcher({"weibo": ("微博", ["某地发布暴雨预警"])})

    out = run(plugin.hot_search_tool(FakeEvent("我想看芯片"), "芯片"))

    assert "没有" in out
    assert "同义词组" in out
    assert "半导体" in out


def test_llm_request_hint_mentions_tool():
    """弱模型会把「我想看ai」当闲聊反问，所以请求里要明确告诉它有工具可用。"""
    plugin = make_plugin(Path(".") / "unused", [], [])

    class Req:
        system_prompt = "你是助手。"

    req = Req()
    run(plugin.hint_hot_search_tool(FakeEvent("我想看ai"), req))
    assert "hot_search" in req.system_prompt
    assert "不要反问" in req.system_prompt
    assert req.system_prompt.startswith("你是助手。")  # 原有提示词不能被覆盖


def test_llm_request_hint_survives_broken_req():
    """框架结构变了（属性名不同）也不能把整次请求搞挂。"""
    plugin = make_plugin(Path(".") / "unused", [], [])

    class Weird:
        pass

    run(plugin.hint_hot_search_tool(FakeEvent("x"), Weird()))  # 不抛异常即可
