"""数据源注册表 + 中文别名解析。"""

from .baidu import BaiduSource
from .base import Source
from .bilibili import BilibiliSource
from .kr36 import Kr36Source
from .toutiao import ToutiaoSource
from .weibo import WeiboSource
from .zhihu import ZhihuSource

ALL_SOURCES: list[Source] = [
    WeiboSource(),
    BaiduSource(),
    ToutiaoSource(),
    ZhihuSource(),
    BilibiliSource(),
    Kr36Source(),
]

SOURCE_MAP: dict[str, Source] = {s.key: s for s in ALL_SOURCES}

# 用户在聊天里更可能打中文，别名映射比要求记 key 友好得多
ALIASES: dict[str, str] = {
    "微博": "weibo",
    "围脖": "weibo",
    "weibo": "weibo",
    "百度": "baidu",
    "baidu": "baidu",
    "头条": "toutiao",
    "今日头条": "toutiao",
    "toutiao": "toutiao",
    "知乎": "zhihu",
    "zhihu": "zhihu",
    "b站": "bilibili",
    "bilibili": "bilibili",
    "哔哩哔哩": "bilibili",
    "小破站": "bilibili",
    "36氪": "36kr",
    "36kr": "36kr",
    "氪": "36kr",
}


def resolve(key: str) -> Source | None:
    """把用户输入的任意写法解析成数据源，找不到返回 None。"""
    raw = (key or "").strip()
    lowered = raw.lower()
    if lowered in SOURCE_MAP:
        return SOURCE_MAP[lowered]
    mapped = ALIASES.get(lowered) or ALIASES.get(raw)
    if mapped:
        return SOURCE_MAP.get(mapped)
    return None


def resolve_many(keys: list[str]) -> list[Source]:
    seen: set[str] = set()
    result: list[Source] = []
    for key in keys:
        source = resolve(key)
        if source and source.key not in seen:
            seen.add(source.key)
            result.append(source)
    return result


def source_keys() -> list[str]:
    return [s.key for s in ALL_SOURCES]
