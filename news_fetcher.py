"""
从多个游戏新闻 RSS 源抓取最近 N 小时内的新闻。
返回与原 slack_reader.NewsItem 完全相同的数据结构，下游模块无需修改。

特殊源：
- IT之家：全站 RSS，仅保留游戏关键词相关文章
- 游民星空：无 RSS，直接抓 HTML 新闻列表页
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import feedparser
import requests as _requests

import config

logger = logging.getLogger(__name__)

# feedparser 内部使用 socket，通过此方式设置全局超时
import socket
_ORIG_TIMEOUT = socket.getdefaulttimeout()


@dataclass
class NewsItem:
    text: str
    timestamp: str
    permalink: str = ""
    source: str = ""


# 游戏相关关键词（用于过滤非专业游戏媒体的全站 RSS）
# 注意：只用 ≥3 字符的词，避免 "SE"/"NS" 等短缩写误匹配
_GAME_KEYWORDS: tuple[str, ...] = (
    "游戏", "手游", "端游", "网游", "主机游戏", "电竞", "电子竞技",
    "Steam", "Epic", "Xbox", "PlayStation", "PS5", "PS4", "Switch",
    "任天堂", "Nintendo", "索尼互娱", "索尼游戏", "微软游戏", "Microsoft",
    "原神", "崩坏", "绝区零", "星穹铁道", "鸣潮", "王者荣耀", "和平精英",
    "英雄联盟", "明日方舟", "蔚蓝档案", "黑神话", "幻兽帕鲁",
    "GTA", "Minecraft", "暗黑破坏神", "魔兽世界", "暴雪", "动视暴雪",
    "卡普空", "Capcom", "育碧", "Ubisoft", "Rockstar",
    "Square Enix", "史克威尔", "万代南梦宫", "Bandai Namco",
    "独立游戏", "开放世界", "MMORPG", "MOBA", "电子游戏",
    "开服", "公测", "内测", "游戏发售", "游戏上线",
)


def _is_game_related(title: str) -> bool:
    """粗略判断标题是否与游戏相关（用于过滤全站 RSS）。"""
    tl = title.lower()
    return any(kw.lower() in tl for kw in _GAME_KEYWORDS)


def fetch_news(lookback_hours: int = config.LOOKBACK_HOURS) -> list[NewsItem]:
    """
    遍历 config.RSS_FEEDS 中所有源，合并、去重、过滤时间窗口内的新闻。
    单个源失败不影响其他源。
    同时抓取游民星空 HTML（无 RSS 支持）。
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    all_items: list[NewsItem] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()

    for feed_url in config.RSS_FEEDS:
        source_name = _source_name(feed_url)
        game_only = feed_url in config.GAME_FILTER_FEEDS
        try:
            items = _fetch_one_feed(
                feed_url, source_name, cutoff,
                seen_urls, seen_titles,
                game_only=game_only,
            )
            all_items.extend(items)
            logger.info("[%s] 抓取到 %d 条新闻", source_name, len(items))
        except Exception as e:
            logger.warning("[%s] 抓取失败，已跳过：%s", source_name, e)

    # 游民星空 HTML 抓取
    try:
        gs_items = _fetch_gamersky_html(cutoff, seen_urls, seen_titles)
        all_items.extend(gs_items)
        logger.info("[游民星空] 抓取到 %d 条新闻", len(gs_items))
    except Exception as e:
        logger.warning("[游民星空] 抓取失败，已跳过：%s", e)

    # 按发布时间正序排列
    all_items.sort(key=lambda x: x.timestamp)
    logger.info("共抓取到 %d 条新闻（最近 %d 小时，%d 个源）",
                len(all_items), lookback_hours, len(config.RSS_FEEDS) + 1)
    return all_items


def _fetch_one_feed(
    feed_url: str,
    source_name: str,
    cutoff: datetime,
    seen_urls: set[str],
    seen_titles: set[str],
    game_only: bool = False,
) -> list[NewsItem]:
    """解析单个 RSS 源，返回符合时间窗口且未重复的条目。"""
    socket.setdefaulttimeout(12)
    try:
        feed = feedparser.parse(feed_url)
    finally:
        socket.setdefaulttimeout(_ORIG_TIMEOUT)

    if feed.bozo and not feed.entries:
        raise ValueError(f"RSS 解析失败：{feed.bozo_exception}")

    items: list[NewsItem] = []
    for entry in feed.entries:
        # 获取发布时间
        pub_time = _entry_time(entry)
        if pub_time and pub_time < cutoff:
            continue

        # 获取链接和标题
        link = entry.get("link", "")
        title = entry.get("title", "").strip()

        # URL 去重
        if link and link in seen_urls:
            continue
        # 标题去重（应对同一新闻多源收录）
        title_key = title.lower()[:60]
        if title_key and title_key in seen_titles:
            continue

        if link:
            seen_urls.add(link)
        if title_key:
            seen_titles.add(title_key)

        # 构造正文：标题 + 摘要
        summary = _clean_text(entry.get("summary", "") or entry.get("description", ""))
        text = title
        if summary and summary.lower() != title.lower():
            # 摘要截取前 300 字符，避免内容过长
            text = f"{title}\n{summary[:300]}"

        if not text or len(text) < 5:
            continue

        # 全站 RSS 过滤：只保留游戏相关文章
        if game_only and not _is_game_related(title):
            continue

        ts = str(pub_time.timestamp()) if pub_time else str(time.time())
        items.append(NewsItem(
            text=text,
            timestamp=ts,
            permalink=link,
            source=source_name,
        ))

    return items


def _fetch_gamersky_html(
    cutoff: datetime,
    seen_urls: set[str],
    seen_titles: set[str],
    max_items: int = 50,
) -> list[NewsItem]:
    """
    直接抓取游民星空新闻列表页（该站无公开 RSS）。
    取最新 max_items 条，按 URL 中的年月过滤超出 lookback 的旧文章。
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.gamersky.com/",
    }
    resp = _requests.get(config.GAMERSKY_URL, headers=headers, timeout=12)
    resp.raise_for_status()
    resp.encoding = "utf-8"   # 游民星空响应头可能声明错误编码，强制 UTF-8
    html = resp.text

    # 提取新闻条目：<a class="tt" href="URL" ... title="TITLE">
    pattern = re.compile(
        r'<a\s+class="tt"\s+href="'
        r'(https://www\.gamersky\.com/news/(\d{4})(\d{2})/(\d+)\.shtml)"'
        r'[^>]*title="([^"]+)"',
        re.IGNORECASE,
    )

    now = datetime.now(timezone.utc)
    # 允许当月和上月的文章（避免月初第一天漏掉昨天的文章）
    cutoff_month = datetime(cutoff.year, cutoff.month, 1, tzinfo=timezone.utc)

    items: list[NewsItem] = []
    for m in pattern.finditer(html):
        if len(items) >= max_items:
            break
        link, year, month, _art_id, title = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
        title = title.strip()
        if not title or len(title) < 3:
            continue

        # 月份粒度过滤（精确日期需进入文章页，此处不做）
        art_month = datetime(int(year), int(month), 1, tzinfo=timezone.utc)
        if art_month < cutoff_month:
            continue

        if link in seen_urls:
            continue
        title_key = title.lower()[:60]
        if title_key in seen_titles:
            continue

        seen_urls.add(link)
        seen_titles.add(title_key)

        items.append(NewsItem(
            text=title,
            timestamp=str(now.timestamp()),
            permalink=link,
            source="游民星空",
        ))

    return items


def _entry_time(entry: dict) -> datetime | None:
    """尝试从 RSS 条目中解析发布时间，返回带时区的 datetime。"""
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        t = entry.get(field)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def _clean_text(html_or_text: str) -> str:
    """简单去除 HTML 标签。"""
    import re
    text = re.sub(r"<[^>]+>", " ", html_or_text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


_URL_SOURCE_MAP = {
    "feedburner.com/ign":       "IGN",
    "feedburner.com/Kotaku":    "Kotaku",
    "gamespot.com":             "GameSpot",
    "rockpapershotgun.com":     "RPS",
    "pcgamer.com":              "PCGamer",
    "eurogamer.net":            "Eurogamer",
    "polygon.com":              "Polygon",
    "feedx.net":                "3DM",
    "nadianshi.com":            "手游那点事",
    "yystv.cn":                 "游研社",
    "ithome.com":               "IT之家",
    "4gamer.net":               "4Gamer",
}


def _source_name(url: str) -> str:
    """从 URL 提取简短的来源名称。"""
    for key, name in _URL_SOURCE_MAP.items():
        if key in url:
            return name
    try:
        host = urlparse(url).netloc
        host = host.replace("www.", "").replace("feeds.", "")
        return host.split(".")[0]
    except Exception:
        return url


def format_for_llm(items: list[NewsItem]) -> str:
    """将新闻列表格式化为发给 LLM 的纯文本（与原 slack_reader 接口完全相同）。"""
    if not items:
        return ""
    lines = []
    for i, item in enumerate(items, 1):
        source_tag = f"[{item.source}] " if item.source else ""
        lines.append(f"[{i}] {source_tag}{item.text}")
        if item.permalink:
            lines.append(f"    链接：{item.permalink}")
        lines.append("")
    return "\n".join(lines)
