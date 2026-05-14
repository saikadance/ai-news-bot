from __future__ import annotations

from dataclasses import dataclass, field

from models import KOLAccount, KOLPost


@dataclass
class FetchResult:
    posts: list[KOLPost] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class BaseKOLFetcher:
    platform_name = "unknown"

    def fetch_recent_posts(self, account: KOLAccount, limit: int = 8, mode: str = "auto") -> FetchResult:
        raise NotImplementedError
