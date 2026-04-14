from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import requests

logger = logging.getLogger(__name__)

_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "analysis_cache.json")
KEEP_DAYS = 3
_GIST_API = "https://api.github.com/gists/{gist_id}"
_FAV_FILENAME = "favorites.json"


def load() -> dict[str, dict]:
    if not os.path.exists(_CACHE_FILE):
        return {}
    try:
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        logger.info("分析缓存已加载：%d 条记录", len(data))
        return data
    except Exception as e:
        logger.warning("分析缓存加载失败，将重建：%s", e)
        return {}


def save_batch(news_items: list, index_map: dict[int, object], existing_cache: dict[str, dict]) -> dict[str, dict]:
    updated = dict(existing_cache)
    now_iso = datetime.now(timezone.utc).isoformat()

    for i, analysis in index_map.items():
        if i >= len(news_items):
            continue
        item = news_items[i]
        key = item.permalink or ""
        if not key:
            continue
        try:
            record = asdict(analysis)
        except Exception:
            record = {
                "judgment": getattr(analysis, "judgment", ""),
                "score": getattr(analysis, "score", 0),
                "reason": getattr(analysis, "reason", ""),
                "angles": getattr(analysis, "angles", []),
                "error": getattr(analysis, "error", ""),
            }
        record["_title"] = item.text.split("\n")[0][:200]
        record["_source"] = item.source or ""
        record["_timestamp"] = item.timestamp or ""
        record["_cached_at"] = now_iso
        updated[key] = record

    favorite_urls = _get_favorite_links()
    if KEEP_DAYS > 0:
        cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat()
        pruned = {
            url: rec
            for url, rec in updated.items()
            if url in favorite_urls or rec.get("_cached_at", "1970-01-01") >= cutoff_iso
        }
        removed = len(updated) - len(pruned)
        if removed:
            logger.info("清理过期分析缓存 %d 条（超过 %d 天）", removed, KEEP_DAYS)
    else:
        pruned = updated

    try:
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(pruned, f, ensure_ascii=False, indent=2)
        logger.info("分析缓存已保存：共 %d 条记录", len(pruned))
    except Exception as e:
        logger.warning("分析缓存保存失败：%s", e)

    return pruned


def get_cached_news_items(cache: dict[str, dict]) -> list:
    from news_fetcher import NewsItem

    items = []
    for url, rec in cache.items():
        title = rec.get("_title", "")
        if not title:
            continue
        items.append(
            NewsItem(
                text=title,
                timestamp=rec.get("_timestamp", "0"),
                permalink=url,
                source=rec.get("_source", ""),
            )
        )
    items.sort(key=lambda x: x.timestamp)
    return items


def load_favorite_news_items() -> list:
    from news_fetcher import NewsItem

    favorite_items = []
    for item in _get_favorites_payload().get("items", []):
        link = (item.get("link") or "").strip()
        title = (item.get("title") or "").strip()
        source = (item.get("source") or "").strip()
        added_at = (item.get("added_at") or "").strip()
        if not link or not title:
            continue

        timestamp = "0"
        if added_at:
            try:
                timestamp = str(datetime.fromisoformat(added_at.replace("Z", "+00:00")).timestamp())
            except Exception:
                pass

        favorite_items.append(
            NewsItem(
                text=title,
                timestamp=timestamp,
                permalink=link,
                source=source,
            )
        )

    favorite_items.sort(key=lambda x: x.timestamp)
    return favorite_items


def filter_uncached(news_items: list, cache: dict[str, dict]) -> set[int]:
    uncached = set()
    for i, item in enumerate(news_items):
        key = item.permalink or ""
        if not key or key not in cache:
            uncached.add(i)
            continue
        if not cache[key].get("_cached_at"):
            uncached.add(i)
    skipped = len(news_items) - len(uncached)
    if skipped:
        logger.info("分析缓存命中 %d 条（跳过）；需新分析 %d 条", skipped, len(uncached))
    return uncached


def to_index_map(news_items: list, cache: dict[str, dict], fresh: dict[int, object]) -> dict[int, object]:
    from llm_analyzer import ArticleAnalysis

    result: dict[int, ArticleAnalysis] = {}
    for i, item in enumerate(news_items):
        key = item.permalink or ""
        if key and key in cache:
            d = cache[key]
            result[i] = ArticleAnalysis(
                judgment=d.get("judgment", ""),
                score=d.get("score", 0),
                reason=d.get("reason", ""),
                angles=d.get("angles", []),
                error=d.get("error", ""),
            )
    result.update(fresh)
    return result


def merge_items(cached_items: list, fresh_items: list) -> list:
    seen: set[str] = set()
    merged: list = []

    for item in fresh_items:
        key = item.permalink or ""
        if key:
            seen.add(key)
        merged.append(item)

    for item in cached_items:
        key = item.permalink or ""
        if key and key in seen:
            continue
        seen.add(key)
        merged.append(item)

    merged.sort(key=lambda x: x.timestamp)
    return merged


def clear() -> None:
    if os.path.exists(_CACHE_FILE):
        os.remove(_CACHE_FILE)
        logger.info("分析缓存已清除")


def _get_favorite_links() -> set[str]:
    return {
        (item.get("link") or "").strip()
        for item in _get_favorites_payload().get("items", [])
        if (item.get("link") or "").strip()
    }


def _get_favorites_payload() -> dict:
    gist_id = os.environ.get("GITHUB_GIST_ID", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    if not gist_id or not token:
        return {"items": []}

    try:
        resp = requests.get(
            _GIST_API.format(gist_id=gist_id),
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "ai-news-bot",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        files = data.get("files", {})
        if _FAV_FILENAME not in files:
            return {"items": []}
        content = files[_FAV_FILENAME].get("content", "") or ""
        return json.loads(content) if content else {"items": []}
    except Exception as e:
        logger.warning("读取收藏列表失败，将忽略收藏保留逻辑：%s", e)
        return {"items": []}
