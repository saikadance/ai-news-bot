"""
跨天持久化 URL 去重缓存。
维护 seen_urls.json 文件，记录已推送过的文章 URL，自动清理 7 天前的记录。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

CACHE_FILE = Path(__file__).parent / "seen_urls.json"
KEEP_DAYS = 7


def load() -> dict[str, str]:
    """加载缓存文件，返回 {url: iso_timestamp} 字典。"""
    if not CACHE_FILE.exists():
        return {}
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("读取 URL 缓存失败，将使用空缓存：%s", e)
        return {}


def filter_new(items: list, cache: dict[str, str]) -> list:
    """
    过滤掉 cache 中已存在的条目，返回真正的新条目列表。
    items 为 news_fetcher.NewsItem 列表。
    """
    new_items = []
    skipped = 0
    for item in items:
        url = item.permalink
        if url and url in cache:
            skipped += 1
            continue
        new_items.append(item)
    if skipped:
        logger.info("跨天去重：过滤掉 %d 条已推送过的新闻", skipped)
    return new_items


def save(items: list, cache: dict[str, str]) -> None:
    """
    将本次新闻的 URL 写入缓存，并清理超过 KEEP_DAYS 天的旧记录。
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    for item in items:
        if item.permalink:
            cache[item.permalink] = now_iso

    # 清理过期记录
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat()
    pruned = {url: ts for url, ts in cache.items() if ts >= cutoff}
    removed = len(cache) - len(pruned)
    if removed:
        logger.debug("清理过期 URL 缓存 %d 条", removed)

    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(pruned, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.error("写入 URL 缓存失败：%s", e)
