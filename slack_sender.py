from __future__ import annotations

import logging

import requests

import config

logger = logging.getLogger(__name__)


def send_report(
    results: list,
    date_str: str,
    news_count: int,
    html_path: str = "",
) -> bool:
    webhook_url = config.SLACK_WEBHOOK_URL
    if not webhook_url:
        logger.info("Slack webhook not configured, skipping Slack push")
        return False

    payload = {
        "text": _build_fallback_text(results, date_str, news_count, html_path),
        "blocks": _build_blocks(results, date_str, news_count, html_path),
    }

    try:
        resp = requests.post(webhook_url, json=payload, timeout=15)
        resp.raise_for_status()
        logger.info("Slack message sent successfully")
        return True
    except requests.RequestException as e:
        logger.error("Slack message send failed: %s", e)
        return False


def _build_fallback_text(
    results: list,
    date_str: str,
    news_count: int,
    html_path: str = "",
) -> str:
    parts = [f"游戏选题日报 {date_str}", f"今日新增 {news_count} 条新闻"]
    if results:
        parts.append(f"AI 精选 Top {len(results)}")
        parts.extend(f"{r.rank}. {r.title}" for r in results[:5])
    if html_path:
        parts.append(html_path)
    return " | ".join(parts)


def _build_blocks(
    results: list,
    date_str: str,
    news_count: int,
    html_path: str = "",
) -> list[dict]:
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"游戏选题日报 | {date_str}"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*今日新增新闻：* {news_count} 条",
            },
        },
    ]

    if results:
        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*AI 精选 Top {len(results)}*",
                },
            }
        )
        for result in results[:5]:
            title = result.title.strip() if getattr(result, "title", "") else "未命名选题"
            score = getattr(result, "score", "")
            source_link = getattr(result, "source_link", "") or html_path

            if source_link:
                line = f"*{result.rank}. <{source_link}|{title}>*"
            else:
                line = f"*{result.rank}. {title}*"
            if score != "":
                line += f"  `评分 {score}/10`"

            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": line},
                }
            )

    if html_path:
        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"<{html_path}|查看完整新闻列表>",
                },
            }
        )

    return blocks
