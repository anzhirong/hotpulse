"""关键词订阅：持久化、匹配、去重。

刻意不依赖 AstrBot 也不依赖 httpx —— 这里只处理"记住用户想看什么"和
"这条标题是不是已经推过了"两件事，方便单独测试。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

# 匹配规则放在 match.py，搜索和监控共用；这里只是转出，保持旧的导入路径可用
from .match import match_title  # noqa: F401

# 已推送标题的保留策略：最多 3000 条、最长 7 天，超出按时间淘汰，
# 否则字典会随着热搜不断累积，几个月后 json 文件变成几 MB。
SEEN_LIMIT = 3000
SEEN_TTL = 7 * 86400


@dataclass
class SubscriptionStore:
    """订阅数据。sessions 记录要推送到哪些会话。"""

    path: Path
    keywords: list[str] = field(default_factory=list)
    sessions: list[str] = field(default_factory=list)
    seen: dict[str, float] = field(default_factory=dict)
    paused: bool = False

    # ---------- 读写 ----------

    @classmethod
    def load(cls, path: Path) -> SubscriptionStore:
        store = cls(path=Path(path))
        if not store.path.exists():
            return store
        try:
            data = json.loads(store.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # 文件损坏时不要让插件起不来，当作空订阅继续跑
            return store
        store.keywords = [str(k) for k in data.get("keywords") or [] if str(k).strip()]
        store.sessions = [str(s) for s in data.get("sessions") or [] if str(s).strip()]
        store.paused = bool(data.get("paused", False))
        raw_seen = data.get("seen") or {}
        if isinstance(raw_seen, dict):
            store.seen = {
                str(k): float(v) for k, v in raw_seen.items() if isinstance(v, (int, float))
            }
        return store

    def save(self) -> None:
        self.prune()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "keywords": self.keywords,
            "sessions": self.sessions,
            "seen": self.seen,
            "paused": self.paused,
        }
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)  # 原子替换，避免写一半断电留下坏文件
        except OSError:
            pass

    # ---------- 关键词 ----------

    def add_keywords(self, words: list[str]) -> list[str]:
        added = []
        existing = {k.lower() for k in self.keywords}
        for word in words:
            word = word.strip()
            if not word or word.lower() in existing:
                continue
            self.keywords.append(word)
            existing.add(word.lower())
            added.append(word)
        return added

    def remove_keywords(self, words: list[str]) -> list[str]:
        wanted = {w.strip().lower() for w in words if w.strip()}
        removed = [k for k in self.keywords if k.lower() in wanted]
        self.keywords = [k for k in self.keywords if k.lower() not in wanted]
        return removed

    def clear_keywords(self) -> int:
        count = len(self.keywords)
        self.keywords = []
        return count

    # ---------- 会话 ----------

    def remember_session(self, session: str) -> None:
        if session and session not in self.sessions:
            self.sessions.append(session)

    def forget_session(self, session: str) -> None:
        self.sessions = [s for s in self.sessions if s != session]

    # ---------- 去重 ----------

    def is_seen(self, title: str) -> bool:
        return title in self.seen

    def mark_seen(self, titles: list[str]) -> None:
        now = time.time()
        for title in titles:
            self.seen[title] = now

    def prune(self) -> None:
        cutoff = time.time() - SEEN_TTL
        self.seen = {k: v for k, v in self.seen.items() if v >= cutoff}
        if len(self.seen) > SEEN_LIMIT:
            ordered = sorted(self.seen.items(), key=lambda kv: kv[1])[-SEEN_LIMIT:]
            self.seen = dict(ordered)
