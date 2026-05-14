from __future__ import annotations

import json
import re
from pathlib import Path

from models import KOLAccount, KOLPost, SignalCandidate, SignalReport


REPO_ROOT = Path(__file__).resolve().parent.parent
ANALYSIS_CACHE_FILE = REPO_ROOT / "analysis_cache.json"
MIN_RELEVANCE_SCORE = 4
NOISE_TERMS = {
    "自拍",
    "欢迎关注",
    "互fo",
    "互关",
    "抽奖",
    "转发抽奖",
    "福利视频",
    "日常",
    "plog",
    "美照",
    "约稿",
    "吃饭",
    "全校闻名",
    "狠人",
    "记录一下",
    "发个自拍",
}
EVENT_TERMS = {
    "游戏",
    "手游",
    "端游",
    "steam",
    "主机",
    "联动",
    "发售",
    "上线",
    "新作",
    "新游",
    "测试",
    "预约",
    "版本",
    "角色",
    "动画",
    "动漫",
    "二次元",
    "pv",
    "预告",
    "官宣",
    "switch",
    "ps5",
    "xbox",
    "dlc",
    "更新",
    "活动",
    "复刻",
}


def load_recent_news_titles(limit: int = 300) -> list[dict]:
    if not ANALYSIS_CACHE_FILE.exists():
        return []
    try:
        raw = json.loads(ANALYSIS_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(raw, dict):
        return []

    items: list[dict] = []
    for link, payload in raw.items():
        if not isinstance(payload, dict):
            continue
        title = str(payload.get("_title", "")).strip()
        source = str(payload.get("_source", "")).strip()
        if not title:
            continue
        items.append({"title": title, "link": str(link), "source": source})
    return items[:limit]


def _candidate_keywords(post: KOLPost) -> list[str]:
    found: list[str] = []
    text = f"{post.title}\n{post.text}"
    for item in re.findall(r"《([^》]{2,20})》", text):
        if item not in found:
            found.append(item)
    for item in post.extracted_keywords:
        if item not in found:
            found.append(item)
    for item in re.findall(r"#([^#]{2,20})#", text):
        if item not in found:
            found.append(item)
    return found[:8]


def _event_keywords_in_text(text: str) -> list[str]:
    found: list[str] = []
    lowered = text.lower()
    for keyword in EVENT_TERMS:
        if keyword in lowered and keyword not in found:
            found.append(keyword)
    return found[:8]


def _noise_hits(text: str) -> list[str]:
    found: list[str] = []
    lowered = text.lower()
    for keyword in NOISE_TERMS:
        if keyword.lower() in lowered and keyword not in found:
            found.append(keyword)
    return found[:6]


def _match_news(post: KOLPost, recent_news: list[dict]) -> list[dict]:
    keywords = _candidate_keywords(post)
    matches: list[dict] = []
    for news in recent_news:
        title = news.get("title", "")
        matched = [k for k in keywords if k and k in title]
        if matched:
            matches.append(
                {
                    "title": title,
                    "link": news.get("link", ""),
                    "source": news.get("source", ""),
                    "matched_keywords": matched,
                }
            )
        if len(matches) >= 5:
            break
    return matches


def _relevance_result(post: KOLPost, matches: list[dict]) -> tuple[int, list[str], list[str], bool]:
    text = f"{post.title}\n{post.text}".strip()
    event_keywords = _event_keywords_in_text(text)
    noise_hits = _noise_hits(text)
    score = 0
    reasons: list[str] = []

    if matches:
        score += 6
        reasons.append(f"命中 {len(matches)} 条关联报道")
    if post.matched_focus_hashtags:
        score += min(len(post.matched_focus_hashtags), 2) * 3
        reasons.append(f"命中 {len(post.matched_focus_hashtags)} 个话题")
    if post.matched_focus_keywords:
        score += min(len(post.matched_focus_keywords), 3) * 2
        reasons.append(f"命中 {len(post.matched_focus_keywords)} 个关注词")
    if _candidate_keywords(post):
        score += 2
        reasons.append("提取到作品名或事件名")
    if event_keywords:
        score += min(len(event_keywords), 2)
        reasons.append("正文带有游戏事件词")
    if len(text) >= 60:
        score += 1
    if noise_hits:
        score -= min(len(noise_hits), 2) * 3
        reasons.append(f"含低相关日常词：{'、'.join(noise_hits[:2])}")
    if len(text) <= 12 and not matches and not post.matched_focus_keywords and not post.matched_focus_hashtags:
        score -= 2

    keep = score >= MIN_RELEVANCE_SCORE
    return score, reasons[:4], event_keywords[:6], keep


def build_signal_report(accounts: list[KOLAccount], posts: list[KOLPost]) -> SignalReport:
    recent_news = load_recent_news_titles()
    filtered_accounts = sum(1 for a in accounts if a.focus_keywords)
    notes = [
        "当前仍处于新功能早期阶段，报告以公开内容抓取和轻量交叉验证为主。",
        f"已读取账号 {len(accounts)} 个，新闻缓存样本 {len(recent_news)} 条。",
        (
            f"当前有 {filtered_accounts} 个账号启用了关键词 / #话题# 筛选。"
            if filtered_accounts
            else "当前未启用关键词筛选，将展示账号最近公开内容。"
        ),
        "后续可继续增强事件聚类、图像识别和跨平台合并。",
    ]

    candidates: list[SignalCandidate] = []
    ranked_items: list[dict] = []
    filtered_out = 0

    for post in posts[:30]:
        matches = _match_news(post, recent_news)
        relevance_score, relevance_reasons, event_keywords, keep = _relevance_result(post, matches)
        if not keep:
            filtered_out += 1
            continue
        ranked_items.append(
            {
                "platform": post.platform,
                "account_name": post.account_name,
                "title": post.title,
                "text": post.text[:280],
                "url": post.url,
                "published_at": post.published_at,
                "score": post.normalized_score(account_priority=1),
                "keywords": _candidate_keywords(post),
                "matched_focus_keywords": post.matched_focus_keywords[:6],
                "matched_focus_hashtags": post.matched_focus_hashtags[:6],
                "event_keywords": event_keywords,
                "relevance_score": relevance_score,
                "relevance_reasons": relevance_reasons,
                "needs_image_review": post.needs_image_review,
                "media_count": len(post.media_urls),
                "media_urls": post.media_urls[:4],
                "downloaded_media_paths": post.downloaded_media_paths[:4],
                "embedded_media_data_urls": post.embedded_media_data_urls[:4],
                "matched_news": matches,
            }
        )

    ranked_items.sort(
        key=lambda item: (item.get("relevance_score", 0), item.get("score", 0)),
        reverse=True,
    )
    posts_preview = ranked_items[:20]

    if posts_preview:
        notes.append(f"当前保留 {len(posts_preview)} 条高相关预览，过滤掉 {filtered_out} 条低相关内容。")
    else:
        notes.append("当前没有通过相关度阈值的内容，后续可增加账号、细化关键词或补充图像识别。")

    return SignalReport(
        generated_at=SignalReport.empty().generated_at,
        accounts_count=len(accounts),
        posts_count=len(posts_preview),
        candidates=candidates,
        posts_preview=posts_preview,
        notes=notes,
    )
