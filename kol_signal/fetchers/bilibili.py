from __future__ import annotations

from datetime import datetime, timezone

from browser_session import has_saved_session, open_persistent_context
from fetchers.base import BaseKOLFetcher, FetchResult
from models import KOLAccount, KOLMetrics, KOLPost


class BilibiliFetcher(BaseKOLFetcher):
    platform_name = "bilibili"

    def fetch_recent_posts(self, account: KOLAccount, limit: int = 8, mode: str = "auto") -> FetchResult:
        if mode in ("auto", "browser") and has_saved_session("bilibili"):
            return self._fetch_via_browser(account, limit=limit)
        return FetchResult(
            posts=[],
            warnings=[
                f"{account.display_name}：B站尚未检测到本地登录会话，请先执行登录脚本。"
            ],
        )

    def _fetch_via_browser(self, account: KOLAccount, limit: int = 8) -> FetchResult:
        warnings: list[str] = []
        posts: list[KOLPost] = []
        try:
            with open_persistent_context("bilibili", headless=True) as (_context, page):
                page.goto(
                    f"https://space.bilibili.com/{account.account_id}/dynamic",
                    wait_until="domcontentloaded",
                    timeout=45000,
                )
                page.wait_for_timeout(4000)
                cards = page.locator("a[href*='/opus/'], a[href*='/video/']")
                seen: set[str] = set()
                count = min(cards.count(), limit * 3)
                for idx in range(count):
                    locator = cards.nth(idx)
                    href = locator.get_attribute("href") or ""
                    if not href:
                        continue
                    if href.startswith("//"):
                        href = "https:" + href
                    if href in seen:
                        continue
                    seen.add(href)
                    text = (locator.inner_text() or "").strip()
                    title = text.splitlines()[0][:80] if text else href
                    if not title:
                        continue
                    posts.append(
                        KOLPost(
                            platform="bilibili",
                            account_id=account.account_id,
                            account_name=account.display_name,
                            post_id=str(idx + 1),
                            url=href,
                            title=title,
                            text=text,
                            published_at=datetime.now(timezone.utc).isoformat(),
                            metrics=KOLMetrics(),
                            raw_payload={},
                            extracted_keywords=[],
                            media_urls=[],
                            needs_image_review=account.require_image_review,
                        )
                    )
                    if len(posts) >= limit:
                        break
                if not posts:
                    warnings.append(f"{account.display_name}：B站动态页未解析到帖子，后续可再补更稳的选择器。")
        except Exception as e:
            warnings.append(f"{account.display_name}：B站浏览器抓取失败（{type(e).__name__}）。")
        return FetchResult(posts=posts, warnings=warnings)
