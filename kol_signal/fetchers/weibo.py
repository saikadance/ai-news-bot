from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime, timezone

import requests

from browser_session import has_saved_session, open_persistent_context
from fetchers.base import BaseKOLFetcher, FetchResult
from models import KOLAccount, KOLMetrics, KOLPost


WEIBO_API = "https://m.weibo.cn/api/container/getIndex"
WEIBO_DETAIL_API = "https://m.weibo.cn/statuses/extend"


def _headers(account: KOLAccount) -> dict[str, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
            "Mobile/15E148 Safari/604.1"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": account.homepage or "https://m.weibo.cn/",
        "X-Requested-With": "XMLHttpRequest",
    }
    cookie = os.getenv("WEIBO_COOKIE", "").strip()
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _strip_html_text(text: str) -> str:
    text = re.sub(r"(?is)<br\s*/?>", "\n", text or "")
    text = re.sub(r"(?is)</p\s*>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", "", text)
    text = html.unescape(text)
    text = text.replace("\u200b", "")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _collect_media_urls(mblog: dict) -> list[str]:
    urls: list[str] = []
    for item in mblog.get("pics") or []:
        if not isinstance(item, dict):
            continue
        for key in ("large", "mw2000", "mw690", "bmiddle"):
            candidate = item.get(key)
            if isinstance(candidate, dict):
                url = str(candidate.get("url", "")).strip()
                if url and url not in urls:
                    urls.append(url)
                    break
    page_info = mblog.get("page_info") or {}
    if isinstance(page_info, dict):
        page_pic = page_info.get("page_pic") or {}
        if isinstance(page_pic, dict):
            url = str(page_pic.get("url", "")).strip()
            if url and url not in urls:
                urls.append(url)
    return urls


def _extract_keywords(text: str, account: KOLAccount) -> list[str]:
    keywords: list[str] = []
    plain = text.lower()
    for keyword in account.focus_keywords:
        if keyword.lower() in plain and keyword not in keywords:
            keywords.append(keyword)
    return keywords


def _extract_hashtags(text: str) -> list[str]:
    found: list[str] = []
    for item in re.findall(r"#([^#\n]{1,40})#", text or ""):
        tag = item.strip()
        if tag and tag not in found:
            found.append(tag)
    return found


def _match_focus_filters(text: str, account: KOLAccount) -> tuple[list[str], list[str]]:
    if not account.focus_keywords:
        return [], []

    plain = (text or "").lower()
    matched_keywords: list[str] = []
    for keyword in account.focus_keywords:
        norm = keyword.strip()
        if norm and norm.lower() in plain and norm not in matched_keywords:
            matched_keywords.append(norm)

    matched_hashtags: list[str] = []
    hashtags = _extract_hashtags(text)
    for tag in hashtags:
        tag_lower = tag.lower()
        for keyword in account.focus_keywords:
            norm = keyword.strip()
            if norm and norm.lower() in tag_lower and tag not in matched_hashtags:
                matched_hashtags.append(tag)
                break
    return matched_keywords, matched_hashtags


def _detail_text_via_requests(session: requests.Session, account: KOLAccount, post_id: str) -> str:
    try:
        resp = session.get(
            WEIBO_DETAIL_API,
            params={"id": post_id},
            headers=_headers(account),
            timeout=15,
        )
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get("data") or {}
        long_text = str(data.get("longTextContent", "") or data.get("text", "") or "")
        return _strip_html_text(long_text)
    except Exception:
        return ""


def _build_post(account: KOLAccount, mblog: dict, detail_fetcher) -> KOLPost | None:
    if not isinstance(mblog, dict):
        return None
    post_id = str(mblog.get("id") or mblog.get("idstr") or "").strip()
    if not post_id:
        return None

    raw_text = str(mblog.get("raw_text", "") or "").strip()
    text = raw_text or _strip_html_text(str(mblog.get("text", "") or ""))
    if mblog.get("isLongText") and len(text) < 80:
        detail = detail_fetcher(post_id)
        if detail:
            text = detail

    title = text.splitlines()[0][:80] if text else f"微博 {post_id}"
    media_urls = _collect_media_urls(mblog)
    metrics = KOLMetrics(
        views=int(mblog.get("reads_count") or 0),
        likes=int(mblog.get("attitudes_count") or 0),
        comments=int(mblog.get("comments_count") or 0),
        shares=int(mblog.get("reposts_count") or 0),
        favorites=0,
    )
    normalized_text = text.strip()
    matched_keywords, matched_hashtags = _match_focus_filters(normalized_text, account)
    return KOLPost(
        platform="weibo",
        account_id=account.account_id,
        account_name=account.display_name,
        post_id=post_id,
        url=f"https://m.weibo.cn/detail/{post_id}",
        title=title,
        text=normalized_text,
        published_at=str(mblog.get("created_at", "") or datetime.now(timezone.utc).isoformat()),
        metrics=metrics,
        raw_payload=mblog,
        extracted_keywords=_extract_keywords(normalized_text, account),
        matched_focus_keywords=matched_keywords,
        matched_focus_hashtags=matched_hashtags,
        media_urls=media_urls,
        needs_image_review=bool(media_urls) and account.require_image_review,
    )


class WeiboFetcher(BaseKOLFetcher):
    platform_name = "weibo"

    def fetch_recent_posts(self, account: KOLAccount, limit: int = 8, mode: str = "auto") -> FetchResult:
        if mode in ("auto", "browser") and has_saved_session("weibo"):
            browser_result = self._fetch_via_browser(account, limit=limit)
            if browser_result.posts or mode == "browser":
                return browser_result
        if mode == "browser":
            return FetchResult(
                posts=[],
                warnings=[f"{account.display_name}：未找到微博本地浏览器会话，请先执行登录脚本。"],
            )
        return self._fetch_via_requests(account, limit=limit)

    def _fetch_via_requests(self, account: KOLAccount, limit: int = 8) -> FetchResult:
        session = requests.Session()
        warnings: list[str] = []
        posts: list[KOLPost] = []
        page = 1

        while len(posts) < limit and page <= 3:
            try:
                resp = session.get(
                    WEIBO_API,
                    params={
                        "type": "uid",
                        "value": account.account_id,
                        "containerid": f"107603{account.account_id}",
                        "page": page,
                    },
                    headers=_headers(account),
                    timeout=18,
                )
                if resp.status_code in (418, 432):
                    warnings.append(
                        f"{account.display_name}：微博接口触发风控（HTTP {resp.status_code}），建议补浏览器登录或 Cookie。"
                    )
                    break
                resp.raise_for_status()
                payload = resp.json()
            except Exception as e:
                warnings.append(f"{account.display_name}：微博抓取失败（{type(e).__name__}）。")
                break

            cards = ((payload.get("data") or {}).get("cards") or [])
            if not cards:
                warnings.append(
                    f"{account.display_name}：微博返回空卡片，可能需要浏览器登录或 Cookie。"
                )
                break

            for card in cards:
                mblog = card.get("mblog") if isinstance(card, dict) else None
                if not mblog:
                    continue
                post = _build_post(
                    account,
                    mblog,
                    detail_fetcher=lambda post_id: _detail_text_via_requests(session, account, post_id),
                )
                if not post:
                    continue
                if account.focus_keywords and not (
                    post.matched_focus_keywords or post.matched_focus_hashtags
                ):
                    continue
                posts.append(post)
                if len(posts) >= limit:
                    break
            page += 1

        if not posts and "Cookie" not in _headers(account):
            warnings.append(
                f"{account.display_name}：当前未配置 WEIBO_COOKIE，如公共接口受限可直接使用浏览器登录模式。"
            )
        if not posts and account.focus_keywords:
            warnings.append(
                f"{account.display_name}：已启用关键词/话题筛选，本轮没有命中 focus_keywords 的公开微博。"
            )

        return FetchResult(posts=posts[:limit], warnings=warnings)

    def _fetch_via_browser(self, account: KOLAccount, limit: int = 8) -> FetchResult:
        warnings: list[str] = []
        posts: list[KOLPost] = []
        try:
            with open_persistent_context("weibo", headless=True) as (_context, page):
                page_num = 1
                while len(posts) < limit and page_num <= 3:
                    api_url = (
                        f"{WEIBO_API}?type=uid&value={account.account_id}"
                        f"&containerid=107603{account.account_id}&page={page_num}"
                    )
                    page.goto(api_url, wait_until="domcontentloaded", timeout=30000)
                    raw = page.locator("body").inner_text()
                    payload = json.loads(raw)
                    cards = ((payload.get("data") or {}).get("cards") or [])
                    if not cards:
                        warnings.append(f"{account.display_name}：微博浏览器会话返回空卡片。")
                        break
                    for card in cards:
                        mblog = card.get("mblog") if isinstance(card, dict) else None
                        if not mblog:
                            continue
                        post = _build_post(
                            account,
                            mblog,
                            detail_fetcher=lambda post_id: self._detail_text_via_browser(page, post_id),
                        )
                        if not post:
                            continue
                        if account.focus_keywords and not (
                            post.matched_focus_keywords or post.matched_focus_hashtags
                        ):
                            continue
                        posts.append(post)
                        if len(posts) >= limit:
                            break
                    page_num += 1
        except Exception as e:
            warnings.append(f"{account.display_name}：微博浏览器抓取失败（{type(e).__name__}）。")

        if not posts and account.focus_keywords:
            warnings.append(
                f"{account.display_name}：已启用关键词/话题筛选，本轮没有命中 focus_keywords 的公开微博。"
            )

        return FetchResult(posts=posts[:limit], warnings=warnings)

    def _detail_text_via_browser(self, page, post_id: str) -> str:
        try:
            page.goto(f"{WEIBO_DETAIL_API}?id={post_id}", wait_until="domcontentloaded", timeout=30000)
            raw = page.locator("body").inner_text()
            payload = json.loads(raw)
            data = payload.get("data") or {}
            return _strip_html_text(str(data.get("longTextContent", "") or data.get("text", "") or ""))
        except Exception:
            return ""
