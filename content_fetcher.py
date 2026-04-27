from __future__ import annotations

import html
import re
import urllib.request

import requests


def _normalize_whitespace(text: str) -> str:
    text = text.replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _strip_tags(fragment: str) -> str:
    fragment = re.sub(r"(?is)<br\s*/?>", "\n", fragment)
    fragment = re.sub(r"(?is)</p\s*>", "\n", fragment)
    fragment = re.sub(r"(?is)<[^>]+>", "", fragment)
    fragment = html.unescape(fragment)
    return _normalize_whitespace(fragment)


def _extract_meta(html_text: str, names: tuple[str, ...]) -> str:
    for name in names:
        pattern = (
            rf'(?is)<meta[^>]+(?:property|name)=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']+)["\']'
        )
        m = re.search(pattern, html_text)
        if m:
            return _strip_tags(m.group(1))
    return ""


def _extract_text_from_html(html_text: str) -> str:
    clean = re.sub(r"(?is)<(script|style|noscript|svg|iframe|form)[^>]*>.*?</\\1>", " ", html_text)
    article_match = re.search(r"(?is)<article[^>]*>(.*?)</article>", clean)
    body_match = re.search(r"(?is)<body[^>]*>(.*?)</body>", clean)
    main = article_match.group(1) if article_match else (body_match.group(1) if body_match else clean)

    paragraphs = []
    for frag in re.findall(r"(?is)<p[^>]*>(.*?)</p>", main):
        text = _strip_tags(frag)
        if len(text) >= 20:
            paragraphs.append(text)
    paragraph_text = "\n\n".join(paragraphs[:40]).strip()

    title = _extract_meta(clean, ("og:title", "twitter:title"))
    if not title:
        m = re.search(r"(?is)<title[^>]*>(.*?)</title>", clean)
        title = _strip_tags(m.group(1)) if m else ""
    description = _extract_meta(clean, ("description", "og:description", "twitter:description"))

    parts = [x for x in (title, description, paragraph_text) if x]
    text = "\n\n".join(parts).strip()
    if len(text) >= 120:
        return text

    fallback = _strip_tags(main)
    parts = [x for x in (title, description, fallback[:4000]) if x]
    return "\n\n".join(parts).strip()


def _fetch_via_reader(url: str, timeout: int) -> tuple[str, str]:
    req = urllib.request.Request(
        f"https://r.jina.ai/{url}",
        headers={
            "User-Agent": "Mozilla/5.0 ai-news-bot/1.0",
            "Accept": "text/markdown,text/plain",
            "X-Timeout": str(timeout),
        },
    )
    with urllib.request.urlopen(req, timeout=timeout + 5) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return _normalize_whitespace(raw), ""


def _fetch_direct_html(url: str, timeout: int) -> tuple[str, str]:
    resp = requests.get(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    text = _extract_text_from_html(resp.text)
    return text, ""


def fetch_article_text(url: str, timeout: int = 25) -> dict:
    if not url:
        return {"text": "", "method": "none", "notice": "缺少链接"}

    reader_errors = []
    for t in (timeout, timeout + 5):
        try:
            text, _ = _fetch_via_reader(url, t)
            if len(text) >= 120:
                return {"text": text, "method": "jina_reader", "notice": ""}
        except Exception as e:
            reader_errors.append(f"{type(e).__name__}")

    try:
        text, _ = _fetch_direct_html(url, timeout)
        if text:
            notice = ""
            if reader_errors:
                notice = f"Jina 抓取失败（{'/'.join(reader_errors)}），已回退到原网页正文提取。"
            return {"text": text, "method": "direct_html", "notice": notice}
    except Exception as e:
        direct_error = f"{type(e).__name__}"
        if reader_errors:
            return {
                "text": "",
                "method": "failed",
                "notice": f"Jina 抓取失败（{'/'.join(reader_errors)}），原网页提取也失败（{direct_error}）。",
            }
        return {"text": "", "method": "failed", "notice": f"正文抓取失败（{direct_error}）。"}

    if reader_errors:
        return {
            "text": "",
            "method": "failed",
            "notice": f"Jina 抓取失败（{'/'.join(reader_errors)}），且未能提取到足够正文。",
        }
    return {"text": "", "method": "failed", "notice": "未能提取到足够正文。"}
