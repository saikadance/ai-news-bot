"""
从 Slack 指定频道拉取最近 N 小时内的游戏新闻消息。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

import config

logger = logging.getLogger(__name__)


@dataclass
class NewsItem:
    text: str
    timestamp: str
    permalink: str = ""


def fetch_news(
    lookback_hours: int = config.LOOKBACK_HOURS,
) -> list[NewsItem]:
    """
    拉取频道最近 lookback_hours 小时内的消息，返回 NewsItem 列表。
    自动处理 Slack API 分页（cursor）和速率限制重试。
    """
    client = WebClient(token=config.SLACK_BOT_TOKEN)
    oldest = _hours_ago_ts(lookback_hours)

    messages: list[dict] = []
    cursor: str | None = None

    while True:
        try:
            kwargs: dict = {
                "channel": config.SLACK_CHANNEL_ID,
                "oldest": oldest,
                "limit": 200,
            }
            if cursor:
                kwargs["cursor"] = cursor

            resp = client.conversations_history(**kwargs)
        except SlackApiError as e:
            if e.response["error"] == "ratelimited":
                retry_after = int(e.response.headers.get("Retry-After", 10))
                logger.warning("Slack 速率限制，等待 %d 秒后重试…", retry_after)
                time.sleep(retry_after)
                continue
            logger.error("Slack API 错误：%s", e.response["error"])
            raise

        messages.extend(resp.get("messages", []))

        if resp.get("has_more") and resp.get("response_metadata", {}).get("next_cursor"):
            cursor = resp["response_metadata"]["next_cursor"]
        else:
            break

    news_items = _parse_messages(client, messages)
    logger.info("共拉取到 %d 条新闻（最近 %d 小时）", len(news_items), lookback_hours)
    return news_items


def _parse_messages(client: WebClient, messages: list[dict]) -> list[NewsItem]:
    """过滤无效消息，构造 NewsItem 列表，并尝试获取 permalink。"""
    items: list[NewsItem] = []

    for msg in messages:
        subtype = msg.get("subtype", "")
        # 跳过频道加入/离开等系统消息，但保留 bot_message（RSS/爬虫频道来源）
        if subtype and subtype not in ("bot_message", ""):
            continue
        text: str = msg.get("text", "").strip()
        # RSS 消息有时正文在 attachments 里
        if not text:
            attachments = msg.get("attachments", [])
            for att in attachments:
                candidate = att.get("text") or att.get("fallback") or att.get("title") or ""
                if candidate:
                    text = candidate.strip()
                    break
        if not text or len(text) < 10:
            continue

        ts = msg.get("ts", "")
        permalink = _get_permalink(client, ts)

        items.append(NewsItem(text=text, timestamp=ts, permalink=permalink))

    # 按时间正序排列（Slack 返回的是倒序）
    items.reverse()
    return items


def _get_permalink(client: WebClient, ts: str) -> str:
    try:
        resp = client.chat_getPermalink(
            channel=config.SLACK_CHANNEL_ID, message_ts=ts
        )
        return resp.get("permalink", "")
    except SlackApiError:
        return ""


def _hours_ago_ts(hours: int) -> str:
    """返回 N 小时前的 Unix 时间戳字符串（Slack oldest 参数格式）。"""
    dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    return str(dt.timestamp())


def format_for_llm(items: list[NewsItem]) -> str:
    """将新闻列表格式化为发给 LLM 的纯文本。"""
    if not items:
        return ""
    lines = []
    for i, item in enumerate(items, 1):
        lines.append(f"[{i}] {item.text}")
        if item.permalink:
            lines.append(f"    链接：{item.permalink}")
        lines.append("")
    return "\n".join(lines)
