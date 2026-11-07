"""监控推送的回归测试：重点验证「只推本轮新上榜」。

AstrBot 不在开发机上跑，所以这里用最小的假模块顶替 astrbot.*，
然后按真实路径加载 main.py —— 测的是插件里那份代码，不是复刻一份逻辑。
"""

from __future__ import annotations

import asyncio
import importlib.util
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
    event.filter = types.SimpleNamespace(command=lambda *a, **k: lambda fn: fn)
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
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, session, chain) -> bool:
        self.sent.append((session, chain.text))
        return True


def make_plugin(tmp_path: Path, keywords: list[str], sessions: list[str] | None = None):
    store_path = tmp_path / "subscriptions.json"
    store = SubscriptionStore(
        path=store_path, keywords=list(keywords), sessions=list(sessions or [])
    )
    plugin = main.HotPulsePlugin(
        FakeContext(),
        # quiet_start == quiet_end 表示不静默，测试不受跑测试的时刻影响
        config={"quiet_start": 0, "quiet_end": 0, "monitor_interval": 1800},
    )
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
