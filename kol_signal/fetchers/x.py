from __future__ import annotations

from fetchers.base import BaseKOLFetcher, FetchResult
from models import KOLAccount


class XFetcher(BaseKOLFetcher):
    platform_name = "x"

    def fetch_recent_posts(self, account: KOLAccount, limit: int = 8, mode: str = "auto") -> FetchResult:
        return FetchResult(
            posts=[],
            warnings=[
                f"{account.display_name}：X 适配器尚未接入真实抓取。"
            ],
        )
