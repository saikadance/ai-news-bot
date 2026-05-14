from __future__ import annotations

from fetchers.base import BaseKOLFetcher
from fetchers.bilibili import BilibiliFetcher
from fetchers.weibo import WeiboFetcher
from fetchers.x import XFetcher


def get_fetcher(platform: str) -> BaseKOLFetcher | None:
    normalized = (platform or "").strip().lower()
    mapping = {
        "weibo": WeiboFetcher(),
        "x": XFetcher(),
        "twitter": XFetcher(),
        "bilibili": BilibiliFetcher(),
        "b站": BilibiliFetcher(),
    }
    return mapping.get(normalized)
