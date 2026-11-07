"""统一数据结构。用 dataclass 而非 pydantic，避免把校验框架塞进插件。"""

from dataclasses import dataclass, field
from datetime import UTC, datetime


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class HotItem:
    rank: int
    title: str
    url: str | None = None
    hot: float | None = None
    hot_text: str | None = None
    summary: str | None = None
    cover: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class SourceResult:
    source: str
    source_name: str
    ok: bool
    items: list[HotItem] = field(default_factory=list)
    error: str | None = None
    elapsed_ms: int = 0
    cached: bool = False
    fetched_at: datetime = field(default_factory=_now)
