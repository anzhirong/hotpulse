"""关键词匹配与标题聚类。

`/热点 搜` 和后台监控共用这一套规则。原因很实际：同一句话如果搜索能命中、
推送却命不中，用户没法理解，也没法自己调。

两条设计取舍：

1. **同义词用「组」表达，而不是让用户手写一堆词**
   AI 这类词在标题里经常换个说法出现（"大模型""DeepSeek""智能体"），
   只写字面量必然漏。用户可以写 `AI` 用内置组，也可以写
   `AI=AI,大模型,GPT` 自己定义组。

2. **跨平台合并用字符 bigram，不引入分词和向量库**
   "同一件事在几个平台同时上榜"本质是字面高度重合。jieba + TF-IDF +
   KMeans 能算得更准，但要几十 MB 内存，对 2C2G 是反性价比。
   字符 bigram 做包含度计算，几十条标题是微秒级，零依赖。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 内置同义词组。键写小写，用户输入不分大小写都能命中。
BUILTIN_GROUPS: dict[str, list[str]] = {
    "ai": [
        "AI",
        "大模型",
        "GPT",
        "LLM",
        "DeepSeek",
        "OpenAI",
        "Claude",
        "Gemini",
        "AIGC",
        "智能体",
        "算力",
        "英伟达",
        "多模态",
        "Agent",
    ],
    "芯片": ["芯片", "半导体", "晶圆", "光刻", "台积电", "中芯", "英伟达", "GPU"],
    "手机": ["手机", "iPhone", "华为", "小米", "荣耀", "vivo", "OPPO", "折叠屏"],
    "汽车": ["汽车", "新能源车", "电动车", "比亚迪", "特斯拉", "理想", "蔚来", "小鹏"],
}

# 组内词之间允许用这些符号分隔
_SPLIT_RE = re.compile(r"[,，、/|]+")
# 参与相似度比较的字符：只保留中英文数字，去掉标点、空格、话题符号
_NOISE_RE = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)


def split_def(keyword: str) -> tuple[str, list[str]]:
    """把 `AI=AI,大模型,GPT` 拆成 (显示名, 同义词列表)。"""
    text = (keyword or "").strip()
    if "=" not in text:
        return text, []
    name, _, rest = text.partition("=")
    name = name.strip()
    words = [w.strip() for w in _SPLIT_RE.split(rest) if w.strip()]
    if not name or not words:
        return text, []
    return name, words


def variants(keyword: str) -> list[str]:
    """一个关键词实际参与匹配的所有写法。"""
    name, custom = split_def(keyword)
    if custom:
        return custom
    return BUILTIN_GROUPS.get(name.lower(), [name]) if name else []


def match_title(title: str, keywords: list[str]) -> list[str]:
    """返回这条标题命中的关键词（用用户输入的写法表示，便于展示）。"""
    if not title or not keywords:
        return []
    low = title.lower()
    hits = []
    for keyword in keywords:
        name, _ = split_def(keyword)
        if not name:
            continue
        if any(word.lower() in low for word in variants(keyword) if word):
            hits.append(name)
    return hits


# ---------- 跨平台聚类 ----------


def _bigrams(text: str) -> set[str]:
    cleaned = _NOISE_RE.sub("", (text or "").lower())
    if len(cleaned) < 2:
        return {cleaned} if cleaned else set()
    return {cleaned[i : i + 2] for i in range(len(cleaned) - 1)}


def similar(a: str, b: str, threshold: float = 0.55) -> bool:
    """两条标题是否在说同一件事。

    用「包含度」而不是 Jaccard：微博是短话题（"某地地震"），头条是长标题
    （"某地发生 4.8 级地震 暂无人员伤亡"），交集相对短的那条才是真实信号。
    短标题（少于 4 字）不参与合并，否则"高考""官宣"会把整页都并成一条。
    """
    left, right = _bigrams(a), _bigrams(b)
    if not left or not right:
        return False
    shorter = min(len(left), len(right))
    if shorter < 3:
        return left == right
    return len(left & right) / shorter >= threshold


@dataclass
class Cluster:
    """同一件事在若干平台的呈现。title 取最长的那条，信息通常最完整。"""

    title: str
    sources: list[str] = field(default_factory=list)
    items: list = field(default_factory=list)

    @property
    def hot(self) -> float:
        values = [getattr(i, "hot", None) for i in self.items]
        return max((v for v in values if isinstance(v, (int, float))), default=0.0)

    @property
    def rank(self) -> int:
        """这条事件在各自榜单里拿到的最好名次。

        排序不能直接比 hot：36氪 是它自己的评分（几千），微博是热度值、B站是
        播放量（几百万），拿三种单位比大小，36氪 永远垫底。名次才是可比的。
        """
        values = [getattr(i, "rank", None) for i in self.items]
        return min((v for v in values if isinstance(v, int) and v > 0), default=999)


def cluster_hits(pairs: list[tuple[str, object]]) -> list[Cluster]:
    """把 (平台名, 条目) 合并成簇，按「跨平台数 → 榜内名次 → 热度」排序。

    顺序很关键：跨 3 个平台都上榜的，才是真的热；同平台名次越靠前越热；
    热度只作为最后的兜底，因为它的单位每个平台都不一样。
    """
    clusters: list[Cluster] = []
    for source_name, item in pairs:
        title = getattr(item, "title", "") or ""
        if not title:
            continue
        for cluster in clusters:
            if similar(title, cluster.title):
                cluster.items.append(item)
                if source_name not in cluster.sources:
                    cluster.sources.append(source_name)
                if len(title) > len(cluster.title):
                    cluster.title = title
                break
        else:
            clusters.append(Cluster(title=title, sources=[source_name], items=[item]))

    clusters.sort(key=lambda c: (-len(c.sources), c.rank, -c.hot))
    return clusters
