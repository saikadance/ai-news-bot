from __future__ import annotations

import json
import re
from pathlib import Path

from models import KOLAccount, KOLPost, SignalCandidate, SignalReport


REPO_ROOT = Path(__file__).resolve().parent.parent
ANALYSIS_CACHE_FILE = REPO_ROOT / "analysis_cache.json"


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


def _match_news(post: KOLPost, recent_news: list[dict]) -> list[dict]:
    keywords = _candidate_keywords(post)
    matches: list[dict] = []
    for news in recent_news:
        title = news.get("title", "")
        if any(keyword and keyword in title for keyword in keywords):
            matches.append(
                {
                    "title": title,
                    "link": news.get("link", ""),
                    "source": news.get("source", ""),
                    "matched_keywords": [k for k in keywords if k in title],
                }
            )
        if len(matches) >= 5:
            break
    return matches


def build_signal_report(accounts: list[KOLAccount], posts: list[KOLPost]) -> SignalReport:
    recent_news = load_recent_news_titles()
    notes = [
        "当前仍处于新功能早期阶段，报告以公开内容抓取和轻量交叉验证为主。",
        f"已读取账号 {len(accounts)} 个，新闻缓存样本 {len(recent_news)} 条。",
        "后续可继续增强事件聚类、图像识别和跨平台合并。",
    ]

    candidates: list[SignalCandidate] = []
    posts_preview: list[dict] = []
    for post in posts[:20]:
        matches = _match_news(post, recent_news)
        posts_preview.append(
            {
                "platform": post.platform,
                "account_name": post.account_name,
                "title": post.title,
                "text": post.text[:280],
                "url": post.url,
                "published_at": post.published_at,
                "score": post.normalized_score(account_priority=1),
                "keywords": _candidate_keywords(post),
                "needs_image_review": post.needs_image_review,
                "media_count": len(post.media_urls),
                "media_urls": post.media_urls[:4],
                "downloaded_media_paths": post.downloaded_media_paths[:4],
                "embedded_media_data_urls": post.embedded_media_data_urls[:4],
                "matched_news": matches,
            }
        )
    if posts_preview:
        notes.append(f"当前已生成 {len(posts_preview)} 条帖子预览，可先用来判断账号信号质量。")
    else:
        notes.append("当前尚未抓到有效帖子，可能需要 Cookie、浏览器会话或平台适配继续增强。")

    return SignalReport(
        generated_at=SignalReport.empty().generated_at,
        accounts_count=len(accounts),
        posts_count=len(posts),
        candidates=candidates,
        posts_preview=posts_preview,
        notes=notes,
    )
