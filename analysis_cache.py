"""
文章分析结果缓存模块。

每条记录同时保存 ArticleAnalysis（分析结果）和 NewsItem 元数据（标题、来源），
使得下次启动时能从缓存直接重建文章列表，不依赖 RSS 重新抓取。

缓存格式（analysis_cache.json）：
{
  "https://example.com/article": {
    "judgment": "适合",
    "score": 8,
    "reason": "...",
    "angles": ["角度1", "角度2"],
    "error": "",
    "_title": "文章标题",
    "_source": "3DM",
    "_timestamp": "1711789200.0",
    "_cached_at": "2026-03-30T15:00:00"
  }
}

保留策略：_cached_at 超过 KEEP_DAYS 天的记录会被自动清理。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "analysis_cache.json")
KEEP_DAYS = 90  # 保留最近 90 天的文章历史（按需调大；0 表示永久保留）


# ── 加载 / 保存 ────────────────────────────────────────────

def load() -> dict[str, dict]:
    """加载缓存，返回 {permalink: record_dict}。"""
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


def save_batch(
    news_items: list,
    index_map: "dict[int, object]",
    existing_cache: dict[str, dict],
) -> dict[str, dict]:
    """
    将本次分析结果（及文章元数据）合并写入缓存，并清理超期记录。
    返回更新后的缓存字典。
    """
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
                "score":    getattr(analysis, "score", 0),
                "reason":   getattr(analysis, "reason", ""),
                "angles":   getattr(analysis, "angles", []),
                "error":    getattr(analysis, "error", ""),
            }
        # 附加文章元数据（以 _ 前缀区分）
        record["_title"]     = item.text.split("\n")[0][:200]
        record["_source"]    = item.source or ""
        record["_timestamp"] = item.timestamp or ""
        record["_cached_at"] = now_iso
        updated[key] = record

    # 清理超期记录（KEEP_DAYS=0 表示永久保留，旧格式无 _cached_at 的条目直接淘汰）
    if KEEP_DAYS > 0:
        cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat()
        pruned = {url: rec for url, rec in updated.items()
                  if rec.get("_cached_at", "1970-01-01") >= cutoff_iso}
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


# ── 从缓存重建文章列表 ─────────────────────────────────────

def get_cached_news_items(cache: dict[str, dict]) -> list:
    """
    从缓存中重建 NewsItem 列表（仅包含有元数据的记录）。
    用于在 RSS 只返回少量新文章时，补全历史文章供 HTML 展示和热点聚类。
    """
    from news_fetcher import NewsItem

    items = []
    for url, rec in cache.items():
        title = rec.get("_title", "")
        if not title:
            continue
        items.append(NewsItem(
            text=title,
            timestamp=rec.get("_timestamp", "0"),
            permalink=url,
            source=rec.get("_source", ""),
        ))
    # 按时间戳排序（最新在后）
    items.sort(key=lambda x: x.timestamp)
    return items


# ── 过滤 / 合并 ────────────────────────────────────────────

def filter_uncached(news_items: list, cache: dict[str, dict]) -> set[int]:
    """
    返回尚未缓存的文章下标集合。
    以下情况需要重新分析：
      - permalink 为空
      - URL 不在缓存中
      - 缓存条目为旧格式（缺少 _cached_at 字段，无法用于恢复文章列表）
    """
    uncached = set()
    for i, item in enumerate(news_items):
        key = item.permalink or ""
        if not key or key not in cache:
            uncached.add(i)
            continue
        # 旧格式条目（无 _cached_at）需重新分析以填充元数据
        if not cache[key].get("_cached_at"):
            uncached.add(i)
    skipped = len(news_items) - len(uncached)
    if skipped:
        logger.info("分析缓存命中 %d 条（跳过）；需新分析 %d 条",
                    skipped, len(uncached))
    return uncached


def to_index_map(
    news_items: list,
    cache: dict[str, dict],
    fresh: "dict[int, object]",
) -> "dict[int, object]":
    """
    将缓存结果和新鲜分析结果合并，返回完整的 {文章下标: ArticleAnalysis} 字典。
    fresh 中的结果优先级高于缓存。
    """
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


# ── 工具 ───────────────────────────────────────────────────

def merge_items(cached_items: list, fresh_items: list) -> list:
    """
    合并缓存文章列表和新鲜 RSS 文章列表，去掉重复 URL，新鲜文章优先。
    返回合并后的列表（按时间戳升序）。
    """
    seen: set[str] = set()
    merged: list = []

    # 新鲜文章优先（覆盖缓存中同 URL 的旧记录）
    for item in fresh_items:
        key = item.permalink or ""
        if key:
            seen.add(key)
        merged.append(item)

    # 缓存文章补充（只加 URL 不在新鲜列表中的）
    for item in cached_items:
        key = item.permalink or ""
        if key and key in seen:
            continue
        seen.add(key)
        merged.append(item)

    merged.sort(key=lambda x: x.timestamp)
    return merged


def clear() -> None:
    """清除所有缓存（调试用）。"""
    if os.path.exists(_CACHE_FILE):
        os.remove(_CACHE_FILE)
        logger.info("分析缓存已清除")
