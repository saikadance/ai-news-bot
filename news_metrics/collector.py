from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import news_fetcher  # noqa: E402

logger = logging.getLogger(__name__)

OUTPUT_FILE = Path(__file__).resolve().parent / "latest_metrics.json"
DEFAULT_LIMIT = 30
REQUEST_TIMEOUT = 12
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,ja;q=0.7",
    "Cache-Control": "no-cache",
}


@dataclass
class NewsMetric:
    url: str
    title: str
    source: str
    host: str
    metrics: dict[str, int | None]
    confidence: str
    method: str
    evidence: list[str] = field(default_factory=list)
    error: str = ""


def _parse_count(raw: str) -> int | None:
    text = (raw or "").strip().replace(",", "")
    if not text:
        return None
    multiplier = 1
    if "亿" in text:
        multiplier = 100000000
    elif "万" in text:
        multiplier = 10000
    elif text.lower().endswith("k"):
        multiplier = 1000
    elif text.lower().endswith("m"):
        multiplier = 1000000

    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return None
    return int(float(match.group(0)) * multiplier)


def _first_count(patterns: list[str], html: str) -> tuple[int | None, str]:
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.IGNORECASE | re.S)
        if not match:
            continue
        count = _parse_count(match.group(1))
        if count is not None:
            snippet = re.sub(r"\s+", " ", match.group(0)).strip()[:120]
            return count, snippet
    return None, ""


def _confidence(metrics: dict[str, int | None]) -> str:
    found = sum(1 for value in metrics.values() if value is not None)
    if found >= 2:
        return "good"
    if found == 1:
        return "partial"
    return "none"


def _extract_generic_metrics(html: str) -> tuple[dict[str, int | None], list[str]]:
    patterns = {
        "views": [
            r"(?:阅读量|阅读|浏览量|浏览|点击量|点击|views?|view_count|read_count)[^0-9万亿kKmM]{0,30}([\d,.]+(?:万|亿|k|K|m|M)?)",
            r"([\d,.]+(?:万|亿|k|K|m|M)?)\s*(?:次阅读|阅读|浏览|views?)",
        ],
        "comments": [
            r"(?:评论数|评论|comments?|comment_count)[^0-9万亿kKmM]{0,30}([\d,.]+(?:万|亿|k|K|m|M)?)",
            r"([\d,.]+(?:万|亿|k|K|m|M)?)\s*(?:条评论|评论|comments?)",
        ],
        "shares": [
            r"(?:转发数|转发|分享数|分享|shares?|share_count)[^0-9万亿kKmM]{0,30}([\d,.]+(?:万|亿|k|K|m|M)?)",
            r"([\d,.]+(?:万|亿|k|K|m|M)?)\s*(?:次分享|分享|转发|shares?)",
        ],
        "likes": [
            r"(?:点赞数|点赞|赞|likes?|like_count)[^0-9万亿kKmM]{0,30}([\d,.]+(?:万|亿|k|K|m|M)?)",
            r"([\d,.]+(?:万|亿|k|K|m|M)?)\s*(?:人点赞|点赞|likes?)",
        ],
        "favorites": [
            r"(?:收藏数|收藏|favorites?|favorite_count)[^0-9万亿kKmM]{0,30}([\d,.]+(?:万|亿|k|K|m|M)?)",
            r"([\d,.]+(?:万|亿|k|K|m|M)?)\s*(?:人收藏|收藏|favorites?)",
        ],
    }

    metrics: dict[str, int | None] = {}
    evidence: list[str] = []
    for key, key_patterns in patterns.items():
        value, snippet = _first_count(key_patterns, html)
        metrics[key] = value
        if snippet:
            evidence.append(f"{key}: {snippet}")
    return metrics, evidence[:5]


def _fetch_page(url: str) -> str:
    parsed = urlparse(url)
    headers = dict(HEADERS)
    if parsed.scheme and parsed.netloc:
        headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
    resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    resp.raise_for_status()
    if resp.apparent_encoding:
        resp.encoding = resp.apparent_encoding
    return resp.text


def collect_one(item: Any) -> NewsMetric:
    title = (getattr(item, "text", "") or "").split("\n")[0][:160]
    url = getattr(item, "permalink", "") or ""
    source = getattr(item, "source", "") or ""
    host = urlparse(url).netloc.lower().replace("www.", "")
    empty_metrics = {
        "views": None,
        "comments": None,
        "shares": None,
        "likes": None,
        "favorites": None,
    }

    if not url:
        return NewsMetric(url, title, source, host, empty_metrics, "none", "none", error="missing_url")

    try:
        html = _fetch_page(url)
        metrics, evidence = _extract_generic_metrics(html)
        return NewsMetric(
            url=url,
            title=title,
            source=source,
            host=host,
            metrics=metrics,
            confidence=_confidence(metrics),
            method="direct_html_generic",
            evidence=evidence,
        )
    except Exception as exc:
        return NewsMetric(
            url=url,
            title=title,
            source=source,
            host=host,
            metrics=empty_metrics,
            confidence="none",
            method="direct_html_generic",
            error=f"{type(exc).__name__}: {str(exc)[:160]}",
        )


def _load_recent_news(lookback_hours: int, limit: int) -> list:
    items = news_fetcher.fetch_news(lookback_hours)
    items.sort(key=lambda item: float(getattr(item, "timestamp", "0") or 0), reverse=True)
    return items[:limit]


def collect_metrics(lookback_hours: int, limit: int, workers: int) -> dict[str, Any]:
    items = _load_recent_news(lookback_hours, limit)
    results: list[NewsMetric] = []
    started = time.time()

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = [executor.submit(collect_one, item) for item in items]
        for future in as_completed(futures):
            results.append(future.result())

    results.sort(
        key=lambda item: (
            {"good": 2, "partial": 1, "none": 0}.get(item.confidence, 0),
            item.metrics.get("views") or 0,
            item.metrics.get("comments") or 0,
        ),
        reverse=True,
    )

    summary = {
        "total": len(results),
        "good": sum(1 for item in results if item.confidence == "good"),
        "partial": sum(1 for item in results if item.confidence == "partial"),
        "none": sum(1 for item in results if item.confidence == "none"),
        "elapsed_seconds": round(time.time() - started, 2),
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lookback_hours": lookback_hours,
        "limit": limit,
        "summary": summary,
        "items": [asdict(item) for item in results],
        "notes": [
            "新闻站点通常不稳定公开阅读量，confidence=none 不代表新闻不热门。",
            "这份数据仅作为选题传播参考，不参与当前 Top5 排序或推送。",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect optional public engagement metrics for recent news.")
    parser.add_argument("--lookback-hours", type=int, default=24)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--output", default=str(OUTPUT_FILE))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    report = collect_metrics(args.lookback_hours, args.limit, args.workers)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("news metrics written: %s (%s items)", output_path, report["summary"]["total"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
