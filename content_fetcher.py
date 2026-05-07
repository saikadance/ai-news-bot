from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request

import requests


MIN_ACCEPTABLE_TEXT = 120

COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

SITE_HINTS: dict[str, tuple[str, ...]] = {
    "ithome.com": (
        "post-content",
        "news-content",
        "article-content",
        "content",
        "detail",
    ),
    "3dmgame.com": (
        "news_warp_center",
        "news_content",
        "article-content",
        "content",
    ),
    "gamersky.com": (
        "Mid2L_con",
        "Mid2Ltext",
        "article-content",
        "content",
    ),
    "bilibili.com": (
        "article-content",
        "opus-module-content",
        "content",
    ),
}

GENERIC_HINTS = (
    "article-content",
    "article_content",
    "post-content",
    "entry-content",
    "content-main",
    "content_detail",
    "detail-content",
    "news-content",
    "article-body",
    "richtext",
    "content",
)


def _normalize_whitespace(text: str) -> str:
    text = text.replace("\r", "\n")
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _strip_tags(fragment: str) -> str:
    fragment = re.sub(r"(?is)<br\s*/?>", "\n", fragment)
    fragment = re.sub(r"(?is)</p\s*>", "\n", fragment)
    fragment = re.sub(r"(?is)</div\s*>", "\n", fragment)
    fragment = re.sub(r"(?is)<[^>]+>", "", fragment)
    fragment = html.unescape(fragment)
    return _normalize_whitespace(fragment)


def _clean_html(html_text: str) -> str:
    html_text = re.sub(r"(?is)<!--.*?-->", " ", html_text)
    html_text = re.sub(r"(?is)<(script|style|noscript|svg|iframe|form)[^>]*>.*?</\1>", " ", html_text)
    return html_text


def _extract_meta(html_text: str, names: tuple[str, ...]) -> str:
    for name in names:
        pattern = (
            rf'(?is)<meta[^>]+(?:property|name)=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']+)["\']'
        )
        m = re.search(pattern, html_text)
        if m:
            return _strip_tags(m.group(1))
    return ""


def _host_hints(host: str) -> tuple[str, ...]:
    host = host.lower()
    for domain, hints in SITE_HINTS.items():
        if domain in host:
            return hints + GENERIC_HINTS
    return GENERIC_HINTS


def _extract_candidate_blocks(html_text: str, host: str) -> list[str]:
    clean = _clean_html(html_text)
    blocks: list[str] = []
    seen: set[str] = set()
    for hint in _host_hints(host):
        pattern = (
            r'(?is)<(?:article|section|div|main)[^>]+'
            r'(?:class|id)=["\'][^"\']*' + re.escape(hint) + r'[^"\']*["\'][^>]*>'
            r'(.*?)'
            r'</(?:article|section|div|main)>'
        )
        for match in re.finditer(pattern, clean):
            block = match.group(1)
            key = block[:300]
            if key in seen:
                continue
            seen.add(key)
            blocks.append(block)
    body_match = re.search(r"(?is)<body[^>]*>(.*?)</body>", clean)
    if body_match:
        blocks.append(body_match.group(1))
    blocks.append(clean)
    return blocks


def _extract_text_from_block(fragment: str) -> str:
    paragraphs: list[str] = []
    for frag in re.findall(r"(?is)<p[^>]*>(.*?)</p>", fragment):
        text = _strip_tags(frag)
        if len(text) >= 18:
            paragraphs.append(text)
    if len(paragraphs) >= 3:
        return "\n\n".join(paragraphs[:60]).strip()

    for frag in re.findall(r"(?is)<div[^>]*>(.*?)</div>", fragment):
        text = _strip_tags(frag)
        if len(text) >= 28:
            paragraphs.append(text)
        if len(paragraphs) >= 20:
            break
    if paragraphs:
        return "\n\n".join(paragraphs[:40]).strip()

    return _strip_tags(fragment)


def _extract_text_from_html(url: str, html_text: str) -> str:
    host = urllib.parse.urlparse(url).netloc.lower()
    clean = _clean_html(html_text)

    title = _extract_meta(clean, ("og:title", "twitter:title"))
    if not title:
        m = re.search(r"(?is)<title[^>]*>(.*?)</title>", clean)
        title = _strip_tags(m.group(1)) if m else ""
    description = _extract_meta(clean, ("description", "og:description", "twitter:description"))

    best_body = ""
    for block in _extract_candidate_blocks(clean, host):
        body = _extract_text_from_block(block)
        if len(body) > len(best_body):
            best_body = body
        if len(best_body) >= 600:
            break

    parts = [x for x in (title, description, best_body) if x]
    text = "\n\n".join(parts).strip()
    if len(text) >= MIN_ACCEPTABLE_TEXT:
        return text

    fallback = _strip_tags(clean)
    parts = [x for x in (title, description, fallback[:5000]) if x]
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
    headers = dict(COMMON_HEADERS)
    parsed = urllib.parse.urlparse(url)
    headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/" if parsed.scheme and parsed.netloc else url

    resp = requests.get(
        url,
        headers=headers,
        timeout=timeout,
        allow_redirects=True,
    )
    resp.raise_for_status()
    if resp.apparent_encoding:
        resp.encoding = resp.apparent_encoding
    html_text = resp.text
    text = _extract_text_from_html(resp.url or url, html_text)
    return text, ""


def fetch_article_text(url: str, timeout: int = 25) -> dict:
    if not url:
        return {"text": "", "method": "none", "notice": "缺少链接"}

    direct_errors: list[str] = []
    reader_errors: list[str] = []
    direct_result = ""
    reader_result = ""

    for t in (timeout, timeout + 5):
        try:
            text, _ = _fetch_direct_html(url, t)
            if len(text) >= 180:
                return {"text": text, "method": "direct_html", "notice": ""}
            if len(text) > len(direct_result):
                direct_result = text
        except Exception as e:
            direct_errors.append(type(e).__name__)

    for t in (timeout, timeout + 5):
        try:
            text, _ = _fetch_via_reader(url, t)
            if len(text) > len(reader_result):
                reader_result = text
            if len(text) >= MIN_ACCEPTABLE_TEXT and len(text) > len(direct_result):
                notice = ""
                if direct_errors:
                    notice = f"直连原文抓取失败（{'/'.join(direct_errors)}），已回退到 Reader。"
                return {"text": text, "method": "jina_reader", "notice": notice}
        except Exception as e:
            reader_errors.append(type(e).__name__)

    if len(direct_result) >= 80:
        notice = ""
        if reader_errors:
            notice = f"Reader 抓取失败（{'/'.join(reader_errors)}），已使用直连原文提取。"
        return {"text": direct_result, "method": "direct_html_partial", "notice": notice}

    if len(reader_result) >= 80:
        notice = ""
        if direct_errors:
            notice = f"直连原文抓取失败（{'/'.join(direct_errors)}），已使用 Reader 提取。"
        return {"text": reader_result, "method": "jina_reader_partial", "notice": notice}

    if direct_errors and reader_errors:
        return {
            "text": "",
            "method": "failed",
            "notice": f"直连原文失败（{'/'.join(direct_errors)}），Reader 也失败（{'/'.join(reader_errors)}）。",
        }
    if direct_errors:
        return {"text": "", "method": "failed", "notice": f"直连原文抓取失败（{'/'.join(direct_errors)}）。"}
    if reader_errors:
        return {"text": "", "method": "failed", "notice": f"Reader 抓取失败（{'/'.join(reader_errors)}）。"}
    return {"text": "", "method": "failed", "notice": "未能提取到足够正文。"}
