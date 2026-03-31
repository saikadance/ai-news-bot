"""
通过飞书自定义机器人 Webhook 发送选题报告（飞书卡片格式）。
支持可选的签名校验。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time

import requests

import config
from llm_analyzer import TopicResult

logger = logging.getLogger(__name__)

SCORE_COLOR = {
    range(9, 11): "red",
    range(7, 9): "orange",
    range(5, 7): "yellow",
    range(0, 5): "grey",
}


def send_report(
    results: list[TopicResult],
    date_str: str,
    news_count: int,
    html_path: str = "",
) -> bool:
    """
    发送选题报告到飞书。
    返回 True 表示发送成功。
    """
    if not results:
        return _send_empty_report(date_str)

    card = _build_card(results, date_str, news_count, html_path)
    return _post({"msg_type": "interactive", "card": card})


# ── 卡片构建 ────────────────────────────────────────────────────────────────


def _build_card(results: list[TopicResult], date_str: str, news_count: int, html_path: str = "") -> dict:
    elements: list[dict] = []

    # 统计摘要
    elements.append({
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": f"📰 今日共采集 **{news_count}** 条游戏新闻，AI 精选 **Top {len(results)}** 选题",
        },
    })

    # 跳转链接
    if html_path and html_path.startswith("http"):
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"[📊 查看完整选题分析与全部新闻 →]({html_path})",
            },
        })

    return {
        "schema": "2.0",
        "header": {
            "template": "blue",
            "title": {
                "tag": "plain_text",
                "content": f"🎮 游戏选题日报 · {date_str}",
            },
        },
        "body": {"elements": elements},
    }


def _send_empty_report(date_str: str) -> bool:
    payload = {
        "msg_type": "text",
        "content": {"text": f"🎮 游戏选题日报 · {date_str}\n今日暂无新游戏新闻，请稍后再看。"},
    }
    return _post(payload)


# ── HTTP 请求 ────────────────────────────────────────────────────────────────


def _post(payload: dict) -> bool:
    webhook_url = config.FEISHU_WEBHOOK_URL
    headers = {"Content-Type": "application/json"}

    if config.FEISHU_WEBHOOK_SECRET:
        timestamp, sign = _sign(config.FEISHU_WEBHOOK_SECRET)
        payload["timestamp"] = timestamp
        payload["sign"] = sign

    try:
        resp = requests.post(webhook_url, json=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0 and data.get("StatusCode") != 0:
            logger.error("飞书 Webhook 返回错误：%s", data)
            return False
        logger.info("飞书消息发送成功")
        return True
    except requests.RequestException as e:
        logger.error("飞书消息发送失败：%s", e)
        return False


def _sign(secret: str) -> tuple[str, str]:
    """生成飞书 Webhook 签名（HMAC-SHA256）。"""
    timestamp = str(int(time.time()))
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    sign = base64.b64encode(hmac_code).decode("utf-8")
    return timestamp, sign


def _score_color(score: int) -> str:
    for score_range, color in SCORE_COLOR.items():
        if score in score_range:
            return color
    return "grey"
